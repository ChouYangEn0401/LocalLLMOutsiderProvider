"""組裝閘道的 aiohttp Application：用戶端 API、worker WebSocket 與管理介面共用同一個埠。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from aiohttp import web

from . import CONFIG, HUB, STORE, admin, api, worker_ws
from .config import GatewayConfig
from .hub import Hub
from .store import Store

log = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 60


def build_app(config: GatewayConfig, store: Store) -> web.Application:
    """建立 Application。store 由呼叫端開啟與關閉。"""
    app = web.Application()
    app[CONFIG] = config
    app[STORE] = store
    app[HUB] = Hub(store, config.max_inflight, config.request_timeout_seconds)
    api.setup(app)
    app.router.add_get("/worker/ws", worker_ws.handle)
    admin.setup(app)
    app.cleanup_ctx.append(_purge_results)
    app.on_shutdown.append(_close_workers)
    return app


async def _purge_results(app: web.Application) -> AsyncIterator[None]:
    """定期丟掉過期的結果。"""

    async def loop() -> None:
        while True:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            dropped = app[HUB].purge()
            if dropped:
                log.debug("丟棄過期結果 count=%d", dropped)

    task = asyncio.create_task(loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _close_workers(app: web.Application) -> None:
    await app[HUB].close_all()
