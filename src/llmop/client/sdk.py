"""用戶端 SDK：封裝一筆請求、送到閘道、必要時輪詢，最後解開回覆。

路徑：<gateway_url>/e2e/v1/models、/e2e/v1/chat/completions、/e2e/v1/requests/{id}；gateway_url 可含 gate 路徑。

信任規則：只會加密給設定檔 pinned_workers 裡的 worker 公鑰。閘道在 /e2e/v1/models 給的公鑰只拿來比對，
從不直接採用；沒有任何已釘選的 worker 提供該模型時，直接報錯，不會改用未釘選的金鑰。

重試規則：
- 閘道回的錯誤一律不重試，直接丟 ClientError。
- 輪詢時的網路失敗會以短暫退避重試，因為輪詢是冪等的。
- 送出請求時的網路失敗，接著用同一個 request_id 輪詢一次；閘道說不認得這筆請求就報錯。
- 整體期限（預設 1800 秒）一到就停止輪詢。

排隊：閘道的 pending 回應可帶 "queue": {"position": 3, "eta": 40}；chat() 的 on_queue 會收到整理過的版本
（只留非負整數），代理用它產生給應用程式的保活註解。

紀錄只寫 request_id、模型、key_id、狀態與耗時；絕不寫提示、回覆、API 金鑰或簽章金鑰。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from typing import NamedTuple

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from llmop import crypto
from llmop.client.config import ClientConfig
from llmop.protocol import (
    HTTP_STATUS,
    MAX_MODEL_LEN,
    MAX_TOKENS_RANGE,
    REQUEST_TIMEOUT_SECONDS,
    SYNC_WAIT_SECONDS,
    ProtocolError,
    b64u_decode,
)

log = logging.getLogger("llmop.client")

DEADLINE_SECONDS = REQUEST_TIMEOUT_SECONDS
# 閘道最多同步等待 SYNC_WAIT_SECONDS，單一 HTTP 呼叫的逾時要比它長一些。
HTTP_TIMEOUT_SECONDS = SYNC_WAIT_SECONDS + 30
# 輪詢時網路失敗的退避秒數；用完後一直使用最後一個值。
POLL_BACKOFF = (0.5, 1.0, 2.0, 4.0, 5.0)
# 閘道應該長輪詢；如果它太快回 pending，至少隔這麼久再問，避免狂打閘道。
POLL_MIN_INTERVAL = 0.5
# 加密面的 API 路徑前綴（接在 gateway_url 之後；gateway_url 可含 gate 路徑）。
API_PREFIX = "/e2e/v1"
# pending 回應的 queue 裡會轉交給呼叫端的欄位。
QUEUE_FIELDS = ("position", "eta")


class ClientError(Exception):
    """一筆請求失敗。code 是協定錯誤碼（或用戶端自己的碼），message 是給人看的說明。

    status 是本機代理回給呼叫端的 HTTP 狀態：預設查 protocol.HTTP_STATUS，查不到用 502。
    sealed 為 True 表示錯誤來自 worker 封裝的回覆（例如 backend_error）。
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int | None = None,
        sealed: bool = False,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message
        self.status = status if status is not None else HTTP_STATUS.get(code, 502)
        self.sealed = sealed
        self.request_id = request_id

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


class WorkerModel(NamedTuple):
    """一筆「（模型，已釘選且 key_id 正確的 worker 公鑰）」。"""

    model: str
    key_id: str
    public_key: str


class _Transient(Exception):
    """網路層的失敗（連線中斷、逾時，或邊緣節點回的非 JSON 5xx）。只在本模組內使用。"""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _url(config: ClientConfig, path: str) -> str:
    """把 API 路徑（例如 /models）接到 <gateway_url>/e2e/v1 後面。"""
    return config.base_url + API_PREFIX + path


async def _call(
    session: aiohttp.ClientSession, config: ClientConfig, method: str, path: str, payload: dict | None = None
) -> tuple[int, dict]:
    """送出一個 HTTP 呼叫，回傳 (HTTP 狀態, JSON 物件)。網路失敗丟 _Transient。"""
    try:
        async with session.request(
            method,
            _url(config, path),
            json=payload,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
        ) as resp:
            status = resp.status
            raw = await resp.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise _Transient(type(exc).__name__) from exc
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # 閘道本身一律回 JSON；非 JSON 的 5xx 來自前面的邊緣節點或隧道，當成網路失敗。
        if status >= 500:
            raise _Transient(f"HTTP {status}")
        if status == 404:
            # R-19：秘密路徑（gate）不對時閘道回純文字 404，最常見的原因是 API 連結換了。
            raise ClientError(
                "bad_response",
                "閘道回了 404：API 連結可能已更換，請到帳號網站查看新的 API 連結，並更新 gateway_url"
                "（the API link may have changed; get the new one from the account website）",
                status=502,
            )
        raise ClientError("bad_response", f"閘道回了無法解析的回應（HTTP {status}）", status=502)
    return status, data


def _gateway_error(status: int, data: dict, request_id: str | None = None) -> ClientError:
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        code = error["code"]
        message = error.get("message")
        return ClientError(
            code,
            message if isinstance(message, str) else "",
            status=HTTP_STATUS.get(code, status if status >= 400 else 502),
            request_id=request_id,
        )
    return ClientError("bad_response", f"閘道的回應格式不正確（HTTP {status}）", status=502, request_id=request_id)


# ---------------------------------------------------------------------------
# 模型清單
# ---------------------------------------------------------------------------


def _parse_model_entry(entry: object) -> WorkerModel | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("llmop"), dict):
        return None
    model, meta = entry.get("id"), entry["llmop"]
    key_id, public_key = meta.get("key_id"), meta.get("public_key")
    if not isinstance(model, str) or not 0 < len(model) <= MAX_MODEL_LEN:
        return None
    if not isinstance(key_id, str) or not isinstance(public_key, str):
        return None
    return WorkerModel(model, key_id, public_key)


async def list_models(session: aiohttp.ClientSession, config: ClientConfig) -> list[WorkerModel]:
    """GET /e2e/v1/models，只留下公鑰已釘選、且 key_id 由公鑰重新計算後相符的項目。"""
    try:
        status, data = await _call(session, config, "GET", "/models")
    except _Transient as exc:
        raise ClientError("network_error", f"無法連到閘道（{exc}）") from exc
    if status != 200:
        raise _gateway_error(status, data)
    entries = data.get("data")
    if not isinstance(entries, list):
        raise ClientError("bad_response", "閘道的模型清單格式不正確", status=502)
    pinned = set(config.pinned_workers)
    result = []
    for entry in entries:
        item = _parse_model_entry(entry)
        if item is None:
            log.debug("略過格式不正確的模型項目")
            continue
        if item.public_key not in pinned:
            log.debug("略過未釘選的 worker 金鑰 model=%s key_id=%.32s", item.model, item.key_id)
            continue
        if crypto.worker_key_id(b64u_decode(item.public_key)) != item.key_id:
            log.warning("閘道給的 key_id 與公鑰不符，已略過 model=%s key_id=%.32s", item.model, item.key_id)
            continue
        result.append(item)
    return result


async def _pick_worker(session: aiohttp.ClientSession, config: ClientConfig, model: str) -> WorkerModel:
    """依 pinned_workers 的順序，挑第一個提供這個模型的已釘選 worker。"""
    serving = {item.public_key: item for item in await list_models(session, config) if item.model == model}
    for public_key in config.pinned_workers:
        if public_key in serving:
            return serving[public_key]
    raise ClientError("model_unavailable", f"沒有任何已釘選的 worker 提供模型 {model!r}（不會改用未釘選的金鑰）")


# ---------------------------------------------------------------------------
# 送出、輪詢、解開
# ---------------------------------------------------------------------------


def _check_params(body: object, model: object, max_tokens: object) -> None:
    if not isinstance(body, dict):
        raise ClientError("bad_request", "body 必須是 JSON 物件", status=400)
    if not isinstance(model, str) or not 0 < len(model) <= MAX_MODEL_LEN:
        raise ClientError("bad_request", f"model 必須是 1 到 {MAX_MODEL_LEN} 個字元的字串", status=400)
    low, high = MAX_TOKENS_RANGE
    if type(max_tokens) is not int or not low <= max_tokens <= high:
        raise ClientError("bad_request", f"max_tokens 必須是 {low} 到 {high} 的整數", status=400)


def queue_info(data: dict) -> dict[str, int] | None:
    """從 pending 回應取出 queue 資訊，只留 QUEUE_FIELDS 裡的非負整數（不接受布林）。沒有就回 None。"""
    queue = data.get("queue")
    if not isinstance(queue, dict):
        return None
    info: dict[str, int] = {}
    for name in QUEUE_FIELDS:
        value = queue.get(name)
        if isinstance(value, float) and math.isfinite(value) and 0 <= value < 1e9:
            value = round(value)
        if type(value) is int and 0 <= value < 10**9:
            info[name] = value
    return info or None


def _interpret(status: int, data: dict, request_id: str) -> str | None:
    """解讀閘道對 POST 或輪詢的回應：完成回傳 sealed，還在跑回傳 None，失敗丟 ClientError。"""
    if data.get("request_id", request_id) != request_id:
        raise ClientError("bad_response", "閘道回應的 request_id 與請求不符", status=502, request_id=request_id)
    state = data.get("status")
    if status == 200 and state == "done" and isinstance(data.get("sealed"), str):
        return data["sealed"]
    if status == 202 and state == "pending":
        return None
    raise _gateway_error(status, data, request_id)


async def _poll(session: aiohttp.ClientSession, config: ClientConfig, request_id: str) -> tuple[int, dict]:
    """輪詢一次；網路失敗就退避後重試，直到成功拿到閘道的回應（或被整體期限取消）。"""
    attempt = 0
    while True:
        try:
            status, data = await _call(session, config, "GET", f"/requests/{request_id}")
        except _Transient as exc:
            delay = POLL_BACKOFF[min(attempt, len(POLL_BACKOFF) - 1)]
            attempt += 1
            log.warning("輪詢時網路失敗（%s），%.1f 秒後重試 request_id=%s", exc, delay, request_id)
            await asyncio.sleep(delay)
            continue
        log.debug("輪詢 request_id=%s http_status=%s", request_id, status)
        return status, data


async def _send(
    session: aiohttp.ClientSession,
    config: ClientConfig,
    signing_key: Ed25519PrivateKey,
    body: dict,
    model: str,
    max_tokens: int,
    on_queue: Callable[[dict[str, int]], None] | None = None,
) -> dict:
    worker = await _pick_worker(session, config, model)
    try:
        envelope, opener = crypto.seal_request(
            body,
            model=model,
            max_tokens=max_tokens,
            worker_public=b64u_decode(worker.public_key),
            signing_key=signing_key,
        )
    except (TypeError, ValueError) as exc:
        raise ClientError("bad_request", "body 無法序列化成 JSON", status=400) from exc
    request_id = envelope.header.request_id
    log.info("送出請求 request_id=%s model=%s key_id=%s", request_id, model, worker.key_id)
    sent = time.monotonic()
    try:
        try:
            status, data = await _call(session, config, "POST", "/chat/completions", envelope.to_json())
        except _Transient as exc:
            log.warning("送出請求時網路失敗（%s），以輪詢確認閘道是否收到 request_id=%s", exc, request_id)
            status, data = await _poll(session, config, request_id)
            error = data.get("error")
            if status == 404 and isinstance(error, dict) and error.get("code") == "unknown_request":
                raise ClientError(
                    "network_error", "送出請求時網路中斷，閘道沒有收到這筆請求", request_id=request_id
                ) from exc
        log.debug("閘道回應 request_id=%s http_status=%s", request_id, status)
        while (sealed := _interpret(status, data, request_id)) is None:
            if on_queue is not None and (queue := queue_info(data)) is not None:
                on_queue(queue)
            asked = time.monotonic()
            status, data = await _poll(session, config, request_id)
            if status == 202:
                await asyncio.sleep(max(0.0, POLL_MIN_INTERVAL - (time.monotonic() - asked)))
        try:
            reply = opener.open(sealed)
        except ProtocolError as exc:
            raise ClientError(exc.code, exc.message, status=502, request_id=request_id) from None
    finally:
        # 一次性回覆私鑰只留在記憶體，用完立刻放掉參照。
        opener = None
    if not reply["ok"]:
        error = reply["error"]
        raise ClientError(error["code"], error["message"], status=502, sealed=True, request_id=request_id)
    log.info(
        "請求完成 request_id=%s model=%s key_id=%s status=done duration=%.2fs",
        request_id,
        model,
        worker.key_id,
        time.monotonic() - sent,
    )
    return reply["body"]


async def chat(
    session: aiohttp.ClientSession,
    config: ClientConfig,
    signing_key: Ed25519PrivateKey,
    body: dict,
    *,
    model: str,
    max_tokens: int,
    deadline: float = DEADLINE_SECONDS,
    on_queue: Callable[[dict[str, int]], None] | None = None,
) -> dict:
    """送出一筆 chat-completions 請求並回傳 chat.completion 本文。失敗丟 ClientError。

    body 是 OpenAI 的請求本文，不含 model、max_tokens 與 stream（這些由參數決定，worker 會忽略本文裡的值）。
    on_queue：閘道回 pending 且帶 queue 資訊時呼叫，參數見 queue_info()。
    """
    _check_params(body, model, max_tokens)
    started = time.monotonic()
    try:
        async with asyncio.timeout(deadline):
            result = await _send(session, config, signing_key, body, model, max_tokens, on_queue)
    except TimeoutError:
        log.warning("請求逾時 model=%s duration=%.2fs", model, time.monotonic() - started)
        raise ClientError("timeout", f"超過 {deadline:g} 秒仍未完成") from None
    except ClientError as exc:
        log.warning(
            "請求失敗 request_id=%s model=%s status=%s duration=%.2fs",
            exc.request_id,
            model,
            exc.code,
            time.monotonic() - started,
        )
        raise
    return result
