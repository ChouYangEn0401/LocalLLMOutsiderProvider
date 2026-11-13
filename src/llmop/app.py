"""Application factory.

Layers (one process, separated by path prefix):

* ``/v1/*``      — the API layer (OpenAI-compatible), API key auth.   -> llmop.api
* ``/``, ``/ui/*`` — the web UI layer (login, dashboard, keys, admin). -> llmop.web
* ``/health``    — unauthenticated liveness check (no details).
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config, ConfigHolder, load_config
from .quota import QuotaManager
from .store import Store

log = logging.getLogger("llmop")


@dataclass
class AppState:
    holder: ConfigHolder
    store: Store
    quota: QuotaManager
    router: "object"  # llmop.backends.BackendRouter
    tunnel: "object | None" = None  # llmop.tunnel.TunnelManager
    started_at: float = field(default_factory=time.time)

    @property
    def config(self) -> Config:
        return self.holder.get()

    def public_url(self) -> str:
        """Fixed ``server.public_url`` if set, else the live tunnel URL, else ''."""
        cfg = self.config
        if cfg.server.public_url:
            return cfg.server.public_url.rstrip("/")
        if self.tunnel is not None and getattr(self.tunnel, "url", None):
            return self.tunnel.url
        got = self.store.kv_get("public_url")
        return got[0] if got else ""


def build_state(config: Config, *, store: Store | None = None, transport=None) -> AppState:
    """``transport`` is an optional httpx transport (tests use httpx.MockTransport)."""
    from .backends import BackendRouter

    holder = ConfigHolder(config)
    if store is None:
        store = Store(config.data_path / "llmop.sqlite3")
    quota = QuotaManager(store)
    router = BackendRouter(holder, transport=transport)
    return AppState(holder=holder, store=store, quota=quota, router=router)


def create_app(config: Config | str | Path | None = None, *, state: AppState | None = None) -> FastAPI:
    if state is None:
        if config is None:
            config = "config.yaml"
        if not isinstance(config, Config):
            config = load_config(config)
        state = build_state(config)

    from .api import build_api_router
    from .web import build_web_router

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.store.purge_expired_sessions()
        try:
            yield
        finally:
            await state.router.aclose()

    app = FastAPI(
        title="LocalLLMOutsiderProvider",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.llmop = state

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    app.include_router(build_api_router(state))
    app.include_router(build_web_router(state))
    return app
