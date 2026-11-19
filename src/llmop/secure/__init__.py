"""加密中繼（B 的閘道功能，移植到 A 的 FastAPI app）。

``build_secure_router(state)`` 回傳可掛載的 router：

* ``POST /e2e/v1/chat/completions`` — 收信封、驗簽、准入、排隊、轉給 worker（同步最多等
  ``sync_wait_seconds``，之後回 202 ``pending``，帶 ``queue``）。
* ``GET /e2e/v1/requests/{request_id}`` — 長輪詢。
* ``GET /e2e/v1/models`` — 線上 worker 的（模型，公鑰）。
* ``WS /worker/ws`` — worker 連線。

閘道只讀得到信封的 protected 標頭，永遠看不到對話內容。

從 ``state`` 讀取：``store``（必要）、``config``（帳號、``queue``、``tiers``、``server.trusted_proxies``）、
``quota``、``usage``、``scheduler``。T-9 接線前沒有的元件用 ``getattr`` 取用並自行建立。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter
from starlette.requests import Request
from starlette.websockets import WebSocket

from llmop.protocol import REQUEST_TIMEOUT_SECONDS, SYNC_WAIT_SECONDS

from . import api, worker_ws
from .api import SecureContext
from .hub import Hub

__all__ = ["build_secure_router"]


def build_secure_router(
    state: Any,
    *,
    sync_wait_seconds: float = SYNC_WAIT_SECONDS,
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> APIRouter:
    """建立加密面的 router。``router.secure`` 是 :class:`SecureContext`（含 ``hub``）。

    router 的 lifespan 在關閉時切斷所有 worker 連線並結束進行中的工作。"""
    hub = Hub(request_timeout_seconds)
    ctx = SecureContext(state, hub, sync_wait_seconds)

    @asynccontextmanager
    async def lifespan(_app: Any):
        try:
            yield
        finally:
            await hub.close_all()

    router = APIRouter(lifespan=lifespan)

    async def chat_completions(request: Request):
        return await api.chat_completions(ctx, request)

    async def get_request(request: Request):
        return await api.get_request(ctx, request)

    async def list_models(request: Request):
        return await api.list_models(ctx, request)

    async def worker_socket(websocket: WebSocket):
        await worker_ws.handle(ctx, websocket)

    router.add_api_route("/e2e/v1/chat/completions", chat_completions, methods=["POST"],
                         include_in_schema=False)
    router.add_api_route("/e2e/v1/requests/{request_id}", get_request, methods=["GET"],
                         include_in_schema=False)
    router.add_api_route("/e2e/v1/models", list_models, methods=["GET"], include_in_schema=False)
    router.add_api_websocket_route("/worker/ws", worker_socket)
    router.secure = ctx  # type: ignore[attr-defined]
    return router
