"""本機 LLM 後端：一個後端就是兩個非同步函式（list_models、chat）加上名稱。

chat(body, model, max_tokens) 一律以非串流方式呼叫後端，回傳 OpenAI chat.completion 物件；
失敗時丟 BackendError，code 是封裝回覆裡的錯誤碼（backend_error、backend_timeout、bad_request）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import aiohttp

from llmop.protocol import MAX_MODEL_LEN
from llmop.worker.config import BackendConfig

log = logging.getLogger(__name__)

MODEL_LIST_TIMEOUT = 10.0  # 列模型是很輕的請求，不必等到 request_timeout
_DETAIL_LIMIT = 300  # 後端錯誤訊息最多帶幾個字元進封裝回覆


class BackendError(Exception):
    """後端呼叫失敗。message 只會放進封裝回覆，絕不寫進紀錄或明文訊框。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Backend:
    name: str  # 例如 "ollama http://127.0.0.1:11434"，用在紀錄裡
    list_models: Callable[[], Awaitable[list[str]]]
    chat: Callable[[dict, str, int], Awaitable[dict]]


def make_backend(cfg: BackendConfig, session: aiohttp.ClientSession, request_timeout: float) -> Backend:
    """依設定建立後端。session 必須以 trust_env=False 建立，迴圈位址的呼叫絕不走系統代理。"""
    from llmop.worker.backends import ollama, openai_compat

    module = ollama if cfg.kind == "ollama" else openai_compat
    return module.make(cfg, session, request_timeout)


def _error_detail(text: str) -> str:
    """從後端的錯誤回應取出簡短說明（Ollama：{"error": "…"}；OpenAI：{"error": {"message": "…"}}）。"""
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        if isinstance(err, str):
            text = err
    return text.strip()[:_DETAIL_LIMIT]


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: float,
    payload: dict | None = None,
    headers: dict | None = None,
) -> object:
    """呼叫後端並解析 JSON 回應；逾時變成 backend_timeout，其他失敗變成 backend_error。"""
    try:
        async with session.request(
            method, url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                detail = _error_detail(text)
                raise BackendError("backend_error", f"後端回應 HTTP {resp.status}" + (f"：{detail}" if detail else ""))
    except asyncio.TimeoutError:
        raise BackendError("backend_timeout", f"後端在 {timeout:g} 秒內沒有完成") from None
    except aiohttp.ClientError as exc:
        raise BackendError("backend_error", f"無法連線到後端（{type(exc).__name__}）") from None
    try:
        return json.loads(text)
    except ValueError:
        raise BackendError("backend_error", "後端的回應不是 JSON") from None


async def collect_models(
    backends: Iterable[Backend], expose: frozenset[str], warned: set[str]
) -> dict[str, Backend]:
    """向每個後端查詢模型，回傳「模型名稱 → 後端」。

    同名模型以設定中較前面的後端為準，並記錄警告（每個名稱只警告一次，warned 由呼叫端保存）。
    expose 不是空的時，只保留其中列出的模型。查不到的後端會被略過並記錄警告。
    """
    routes: dict[str, Backend] = {}
    for backend in backends:
        try:
            names = await backend.list_models()
        except BackendError as exc:
            log.warning("無法取得 %s 的模型清單（%s）", backend.name, exc.code)
            continue
        for name in names:
            if expose and name not in expose:
                continue
            if len(name) > MAX_MODEL_LEN:
                log.warning("略過名稱超過 %d 字元的模型（%s）", MAX_MODEL_LEN, backend.name)
                continue
            if name in routes:
                if name not in warned:
                    warned.add(name)
                    log.warning("模型 %s 同時由 %s 與 %s 提供，使用前者", name, routes[name].name, backend.name)
                continue
            routes[name] = backend
    return routes
