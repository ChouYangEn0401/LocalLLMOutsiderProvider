"""用戶端 API：POST /v1/chat/completions、GET /v1/requests/{request_id}、GET /v1/models、GET /healthz。

閘道只解析信封的 protected 標頭；sealed 與 sig 是不透明字串，原樣轉給 worker，不寫進紀錄或磁碟。
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from llmop.crypto import verify_request
from llmop.protocol import (
    HTTP_STATUS,
    MAX_BODY_BYTES,
    Envelope,
    Header,
    ProtocolError,
    b64u_decode,
    check_timestamp,
    loads_strict,
)

from . import CONFIG, HUB, STORE
from .hub import Job
from .store import ApiKey, utc_day

log = logging.getLogger(__name__)

# 轉送給 worker 時，信封會包進 request 訊框（多了 type、request_id、client_sig_pub 等欄位），
# 訊框本身一樣受 MAX_BODY_BYTES 限制（worker 端 WebSocket 的 max_msg_size）。所以這裡收本文時
# 先扣一點餘裕，寧可在這裡乾淨地回 payload_too_large，也不要讓包裝後的訊框在 worker 那邊被悄悄丟棄。
_MAX_ENVELOPE_BYTES = MAX_BODY_BYTES - 4096

_BEARER = "Bearer "
_READ_CHUNK = 64 * 1024

_DEFAULT_MESSAGES = {
    "payload_too_large": f"請求本文超過 {MAX_BODY_BYTES} 位元組",
    "unauthorized": "缺少或無效的 API 金鑰",
    "duplicate_request": "這個 request_id 已經送出過",
    "unknown_request": "找不到這筆請求",
    "internal_error": "閘道內部錯誤",
}


def _now() -> float:
    """目前時間（Unix 秒）。測試會替換它來模擬換日。"""
    return time.time()


def error_response(code: str, message: str = "", request_id: str | None = None) -> web.Response:
    body: dict = {"status": "error", "error": {"code": code, "message": message or _DEFAULT_MESSAGES.get(code, code)}}
    if request_id is not None:
        body = {"request_id": request_id, **body}
    return web.json_response(body, status=HTTP_STATUS[code])


def job_response(job: Job) -> web.Response:
    if not job.done.is_set():
        return web.json_response({"request_id": job.request_id, "status": "pending"}, status=202)
    if job.code is not None:
        return error_response(job.code, job.message, job.request_id)
    return web.json_response({"request_id": job.request_id, "status": "done", "sealed": job.sealed})


def _authenticate(request: web.Request) -> ApiKey:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith(_BEARER):
        raise ProtocolError("unauthorized", "缺少 Authorization: Bearer 標頭")
    key = request.app[STORE].find_api_key(auth[len(_BEARER):].strip())
    if key is None:
        raise ProtocolError("unauthorized", "API 金鑰無效或已撤銷")
    return key


async def _read_body(request: web.Request) -> bytes:
    if request.content_length is not None and request.content_length > _MAX_ENVELOPE_BYTES:
        raise ProtocolError("payload_too_large")
    chunks, size = [], 0
    async for chunk in request.content.iter_chunked(_READ_CHUNK):
        size += len(chunk)
        if size > _MAX_ENVELOPE_BYTES:
            raise ProtocolError("payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _peek_request_id(obj: object) -> str | None:
    """信封不合法時，盡量從 protected 取出 request_id 放進錯誤回應。"""
    try:
        return Header.parse(obj["protected"]).request_id  # type: ignore[index]
    except (ProtocolError, KeyError, TypeError):
        return None


async def chat_completions(request: web.Request) -> web.Response:
    started = time.monotonic()
    # 紀錄用的欄位，處理過程中逐步填入；只放可以公開記錄的值。
    trace: dict = {"request_id": None, "api_key_id": None, "worker_id": None, "model": None, "bytes": 0}
    try:
        response = await _chat(request, trace)
    except ProtocolError as exc:
        response = error_response(exc.code, exc.message, trace["request_id"])
    except Exception as exc:
        # 只記例外類型，不記訊息：訊息裡可能夾帶輸入資料。
        log.error("chat 內部錯誤 request_id=%s error=%s", trace["request_id"], type(exc).__name__)
        response = error_response("internal_error", request_id=trace["request_id"])
    log.info(
        "chat request_id=%s api_key_id=%s worker_id=%s model=%s bytes=%d status=%d duration_ms=%d",
        trace["request_id"], trace["api_key_id"], trace["worker_id"], trace["model"], trace["bytes"],
        response.status, (time.monotonic() - started) * 1000,
    )
    return response


async def _chat(request: web.Request, trace: dict) -> web.Response:
    """依 PROTOCOL.md 的九個步驟處理一筆請求；任何一步失敗就丟 ProtocolError。"""
    config, store, hub = request.app[CONFIG], request.app[STORE], request.app[HUB]

    # 1. 本文大小、JSON 形狀、解析標頭。
    raw = await _read_body(request)
    trace["bytes"] = len(raw)
    obj = loads_strict(raw)
    try:
        envelope = Envelope.from_json(obj)
    except ProtocolError:
        trace["request_id"] = _peek_request_id(obj)
        raise
    header = envelope.header
    trace["request_id"], trace["model"] = header.request_id, header.model

    # 2. API 金鑰（存在且未撤銷）。
    key = _authenticate(request)
    trace["api_key_id"] = key.id

    # 3. sig_key_id 與簽章（verify_request 兩項都檢查）。
    if key.sig_public_key is None:
        raise ProtocolError("bad_signature", "這把 API 金鑰還沒有登記簽章公鑰")
    verify_request(envelope, b64u_decode(key.sig_public_key))

    # 4. 時間窗。
    now = _now()
    check_timestamp(header.ts, int(now))

    # 5. 重複的 request_id。
    if hub.is_duplicate(key.id, header.request_id):
        raise ProtocolError("duplicate_request")

    # 6–8. 配額檢查與計數在同一個交易；路由與容量檢查夾在中間，都是同步的記憶體操作，
    # 任何一項失敗都會回滾，不計數。
    day = utc_day(now)
    with store.admit(key.id, header.max_tokens, day):
        conn = hub.route(header.key_id, header.model)
        hub.check_capacity(conn)
    trace["worker_id"] = conn.worker_id

    # 9. 已計入配額；轉送給 worker，最多等 sync_wait_seconds。
    job = hub.submit(conn, key.id, header.request_id, header.model, day)
    await hub.relay(job, envelope, key.sig_public_key)
    await hub.wait(job, config.sync_wait_seconds)
    return job_response(job)


async def get_request(request: web.Request) -> web.Response:
    started = time.monotonic()
    request_id = request.match_info["request_id"]
    try:
        key = _authenticate(request)
    except ProtocolError as exc:
        return error_response(exc.code, exc.message, request_id)
    hub = request.app[HUB]
    job = hub.get(key.id, request_id)
    if job is None:
        response = error_response("unknown_request", request_id=request_id)
    else:
        await hub.wait(job, request.app[CONFIG].sync_wait_seconds)
        response = job_response(job)
    log.info(
        "poll request_id=%s api_key_id=%s status=%d duration_ms=%d",
        request_id, key.id, response.status, (time.monotonic() - started) * 1000,
    )
    return response


async def list_models(request: web.Request) -> web.Response:
    try:
        _authenticate(request)
    except ProtocolError as exc:
        return error_response(exc.code, exc.message)
    data = [
        {
            "id": model,
            "object": "model",
            "owned_by": conn.name,
            "llmop": {"key_id": key_id, "public_key": public_key},
        }
        for conn in request.app[HUB].workers.values()
        for model in conn.models
        for key_id, public_key in conn.keys.items()
    ]
    return web.json_response({"object": "list", "data": data})


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def setup(app: web.Application) -> None:
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/requests/{request_id}", get_request)
    app.router.add_get("/v1/models", list_models)
    app.router.add_get("/healthz", healthz)
