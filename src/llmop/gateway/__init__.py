"""閘道：唯一對外的程序。

驗證 API 金鑰與請求簽章、執行配額、把不透明的加密信封轉給以 WebSocket 連入的 worker，
並提供本機管理介面。閘道只讀得到信封的 protected 標頭，永遠看不到對話內容。

這裡只放各模組共用的 aiohttp 應用程式鍵，組裝在 app.py。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .admin import AdminState
    from .config import GatewayConfig
    from .hub import Hub
    from .store import Store

CONFIG: web.AppKey[GatewayConfig] = web.AppKey("config")
STORE: web.AppKey[Store] = web.AppKey("store")
HUB: web.AppKey[Hub] = web.AppKey("hub")
ADMIN: web.AppKey[AdminState] = web.AppKey("admin")
