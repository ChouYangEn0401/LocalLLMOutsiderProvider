"""Application factory.

v0.2 node (DISPATCH section 3): one :class:`AppState`, three apps, one per listener:

* ``local``      (127.0.0.1:8781) — local console (``web`` surface ``local``) + key-less
  ``/v1`` (``api`` zone ``local``) + ``/worker/ws`` for a worker on this machine.
  Every request must pass :func:`llmop.netguard.is_local`; uvicorn runs it with
  ``proxy_headers`` off.
* ``public-ui``  (127.0.0.1:8780, tunnel "ui") — the account website (``web`` surface
  ``public``) only: no ``/v1``, no ``/e2e``, no ``/worker/ws``.
* ``public-api`` (127.0.0.1:8782, tunnel "api") — ``/<gate>/v1/*`` (``api`` zone
  ``public``), ``/<gate>/e2e/v1/*`` and ``/<gate>/worker/ws`` (``secure``). No login
  page, no cookies. The gate comes from ``state.api_link``.

Both public apps check ``Host`` (421 for a foreign host name) and limit request bodies.

``create_app(config)`` / ``create_app(state=state)`` without ``surface`` is the v0.1
single-listener app (``/v1`` + the combined web UI), kept for ``llmop serve --port``.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocket

from . import __version__, netguard
from .config import Config, ConfigHolder, load_config
from .quota import QuotaManager
from .store import Store

log = logging.getLogger("llmop")
access_log = logging.getLogger("llmop.access")

SURFACES = ("local", "public-ui", "public-api")
PUBLIC_UI_BODY_LIMIT = 1 * 1024 * 1024
PUBLIC_API_BODY_LIMIT = 16 * 1024 * 1024  # = protocol.MAX_BODY_BYTES (encrypted envelopes)


@dataclass
class AppState:
    holder: ConfigHolder
    store: Store
    quota: QuotaManager
    router: "object"  # llmop.backends.BackendRouter
    tunnel: "object | None" = None  # llmop.tunnel.TunnelManager (v0.1 single tunnel)
    started_at: float = field(default_factory=time.time)
    # v0.2 (DISPATCH section 3); every consumer reads them with getattr.
    usage: "object | None" = None  # llmop.usage.UsageManager
    scheduler: "object | None" = None  # llmop.scheduler.Scheduler
    policy: "object | None" = None  # reserved (entrance rules live in the routers)
    api_link: "object | None" = None  # llmop.apilink.ApiLink
    hub: "object | None" = None  # llmop.secure.hub.Hub (set when the secure router is built)
    tunnels: dict = field(default_factory=dict)  # {"ui": TunnelManager, "api": TunnelManager}
    secure_router: "object | None" = None  # the one secure router (shared hub)

    @property
    def config(self) -> Config:
        return self.holder.get()

    def public_url(self) -> str:
        """Fixed ``server.public_url`` if set, else the live tunnel URL, else ''."""
        cfg = self.config
        if cfg.server.public_url:
            return cfg.server.public_url.rstrip("/")
        tunnel = self.tunnels.get("ui") if isinstance(self.tunnels, dict) else None
        tunnel = tunnel or self.tunnel
        if tunnel is not None and getattr(tunnel, "url", None):
            return tunnel.url
        got = self.store.kv_get("public_url")
        return got[0] if got else ""


def scheduler_settings(cfg: Any) -> dict:
    """Scheduler keyword arguments from ``queue``, the tiers' ``max_concurrent`` and
    each account's effective ``max_concurrent`` (tier value or the account's own
    override, R-03)."""
    from .scheduler import TIERS

    q = getattr(cfg, "queue", None)
    out: dict = {}
    if q is not None:
        out.update(slots=q.slots, max_length=q.max_length, max_per_account=q.max_per_account,
                   max_wait_seconds=dict(q.max_wait_seconds), fair_share_every=q.fair_share_every)
    tiers = getattr(cfg, "tiers", None) or {}
    out["max_concurrent"] = {t: s.max_concurrent for t, s in tiers.items() if t in TIERS}
    accounts = getattr(cfg, "accounts", None) or {}
    out["account_max_concurrent"] = {
        name: acc.limits.max_concurrent for name, acc in accounts.items()
        if getattr(acc, "limits", None) is not None and acc.limits.max_concurrent}
    return out


def make_scheduler(cfg: Config):
    """The global slot scheduler from ``queue``, the tiers' and the accounts'
    ``max_concurrent``. :func:`sync_scheduler` keeps it in step with config reloads."""
    from .scheduler import Scheduler

    sched = Scheduler(**scheduler_settings(cfg))
    sched.config_ref = cfg  # type: ignore[attr-defined]
    return sched


def sync_scheduler(scheduler: Any, cfg: Any) -> bool:
    """R-13: when the config object changed (hot reload), give the scheduler the new
    slots / limits. Running requests are untouched. Returns True if it changed."""
    if scheduler is None or cfg is None or not hasattr(scheduler, "config_ref"):
        return False  # not built from a config (tests pass their own): leave it alone
    if scheduler.config_ref is cfg:
        return False
    reconfigure = getattr(scheduler, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(**scheduler_settings(cfg))
    except (ValueError, TypeError) as exc:  # keep the old numbers
        log.warning("scheduler: new queue settings not applied (%s)", exc)
    scheduler.config_ref = cfg
    return True


def build_state(config: Config, *, store: Store | None = None, transport=None,
                scheduler: bool | None = None, api_link: bool = True) -> AppState:
    """``transport`` is an optional httpx transport (tests use httpx.MockTransport).

    Always wired: ``usage`` (both usage tables) and ``quota`` on top of it.
    ``scheduler``: the global slot queue; None = only when this call opens the
    database itself (the node). A caller that passes its own ``store`` (v0.1 tests)
    keeps the per-backend semaphore unless it asks for the scheduler.
    When the caller passes its own ``store`` the API gate is kept in memory only
    (no file in ``data_dir``); otherwise the database and the gate file live in
    ``config.data_path``."""
    from .backends import BackendRouter
    from .usage import UsageManager

    holder = ConfigHolder(config)
    own_store = store is None
    if store is None:
        store = Store(config.data_path / "llmop.sqlite3")
    usage = UsageManager(store, holder)
    quota = QuotaManager(store, tables=usage)
    router = BackendRouter(holder, transport=transport)
    state = AppState(holder=holder, store=store, quota=quota, router=router)
    state.usage = usage
    if scheduler if scheduler is not None else own_store:
        state.scheduler = make_scheduler(config)
    if api_link:
        from .apilink import ApiLink, api_base_url

        state.api_link = ApiLink(
            store, config.data_path if own_store else None,
            base_url=lambda: api_base_url(state.config, state.tunnels.get("api")),
        )
    return state


# --------------------------------------------------------------------------- ASGI middleware
async def _plain(send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class LocalOnlyMiddleware:
    """Whole-app guard of the local listener: :func:`netguard.is_local` or 403."""

    def __init__(self, app, port: int | None = None) -> None:
        self.app, self.port = app, port

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and not netguard.is_local(scope, port=self.port):
            if scope["type"] == "http":
                await _plain(send, 403, "local console only")
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


class PublicHostMiddleware:
    """``Host`` must be loopback or one of ``allowed()`` (public URLs), else 421."""

    def __init__(self, app, allowed: Callable[[], list[str]]) -> None:
        self.app, self.allowed = app, allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            try:
                names = [n for n in self.allowed() if n]
            except Exception:
                names = []
            if not netguard.check_public_host(scope, names):
                if scope["type"] == "http":
                    await _plain(send, 421, "Misdirected Request")
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """413 when ``Content-Length`` or the streamed body exceeds ``limit`` bytes."""

    def __init__(self, app, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for k, v in scope.get("headers") or ():
            if k.lower() == b"content-length":
                try:
                    too_big = int(v) > self.limit
                except ValueError:
                    too_big = True
                if too_big:
                    await _plain(send, 413, "Request body too large")
                    return
        seen = 0
        exceeded = False
        started = False

        async def limited_receive():
            nonlocal seen, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message):
            nonlocal started
            if exceeded and not started:
                return  # the app answered a body we cut off; 413 is sent below
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await _plain(send, 413, "Request body too large")


class RedactedAccessLog:
    """Access log for the public API listener: the gate (first path segment) and the
    query string are masked (:func:`netguard.redact_path`)."""

    def __init__(self, app, enabled: Callable[[], bool]) -> None:
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        status = {"code": 0}

        async def capture(message):
            if message["type"] in ("http.response.start", "websocket.http.response.start"):
                status["code"] = message.get("status", 0)
            elif message["type"] == "websocket.accept":
                status["code"] = 101
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            if self.enabled():
                path = scope.get("path") or ""
                qs = scope.get("query_string") or b""
                access_log.info("%s %s %s", scope.get("method", "WS"),
                                netguard.redact_path(path + ("?x" if qs else "")),
                                status["code"] or "-")


# --------------------------------------------------------------------------- apps
def _new_app(state: AppState, lifespan_extra: Callable | None = None, *,
             public: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.store.purge_expired_sessions()
        try:
            yield
        finally:
            if lifespan_extra is not None:
                await lifespan_extra()

    app = FastAPI(title="LocalLLMOutsiderProvider", version=__version__, docs_url=None,
                  redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.llmop = state

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        if public:  # R-17: public entrances do not tell the version
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "ok", "version": __version__})

    return app


def secure_router(state: AppState):
    """The one encrypted-relay router of this process (built on first use), or None
    when the ``secure`` extra is not installed. Sets ``state.hub``."""
    if state.secure_router is not None:
        return state.secure_router
    try:
        from .secure import build_secure_router
    except ImportError as exc:  # cryptography / aiohttp missing
        log.warning("encrypted relay disabled: %s (pip install \"localllm-outsider-provider[secure]\")",
                    exc.name or type(exc).__name__)
        return None
    router = build_secure_router(state)
    state.secure_router = router
    state.hub = router.secure.hub  # type: ignore[attr-defined]
    return router


def ui_hosts(state: AppState) -> list[str]:
    cfg = state.config
    tun = state.tunnels.get("ui") if isinstance(state.tunnels, dict) else None
    tun = tun or state.tunnel
    out = [cfg.server.public_url, getattr(cfg.tunnels.ui, "public_url", "")]
    if tun is not None:
        out.append(getattr(tun, "url", None) or "")
    return out


def api_hosts(state: AppState) -> list[str]:
    cfg = state.config
    tun = state.tunnels.get("api") if isinstance(state.tunnels, dict) else None
    out = [getattr(cfg.tunnels.api, "public_url", "")]
    if tun is not None:
        out.append(getattr(tun, "url", None) or "")
    link = state.api_link
    if link is not None:
        try:
            out.append(link.base_url())
        except Exception:
            pass
    return out


def _create_local(state: AppState, port: int | None):
    from .api import build_api_router
    from .web import build_web_router

    # The backend router is shared by the three apps; the node closes it at the end.
    app = _new_app(state)
    sec = secure_router(state)
    if sec is not None:
        from .secure import worker_ws

        ctx = sec.secure  # type: ignore[attr-defined]

        async def local_worker_socket(websocket: WebSocket):
            await worker_ws.handle(ctx, websocket)

        # Same handler and hub as /<gate>/worker/ws: a worker on this machine (the
        # in-process one of the node) connects here without the secret path.
        app.add_api_websocket_route("/worker/ws", local_worker_socket)
    app.include_router(build_api_router(state, "local"))
    app.include_router(build_web_router(state, "local"))
    return LocalOnlyMiddleware(app, port)


def _create_public_ui(state: AppState):
    from .web import build_web_router

    app = _new_app(state, public=True)
    app.include_router(build_web_router(state, "public"))
    inner = BodyLimitMiddleware(app, PUBLIC_UI_BODY_LIMIT)
    return PublicHostMiddleware(inner, lambda: ui_hosts(state))


def _create_public_api(state: AppState):
    from .api import build_api_router

    if state.api_link is None:
        raise ValueError("the public API app needs state.api_link (the secret path)")
    app = _new_app(state, public=True)
    app.include_router(build_api_router(state, "public"))
    sec = secure_router(state)
    if sec is not None:
        app.include_router(sec)
    link = state.api_link
    inner = BodyLimitMiddleware(app, PUBLIC_API_BODY_LIMIT)
    gated = netguard.GateMiddleware(inner, link.gate_state)
    hosted = PublicHostMiddleware(gated, lambda: api_hosts(state))
    return RedactedAccessLog(hosted, lambda: bool(state.config.server.access_log))


def create_app(config: Config | str | Path | None = None, *, state: AppState | None = None,
               surface: str | None = None, port: int | None = None) -> Any:
    """``surface`` ``None`` = the v0.1 single app; ``"local"`` / ``"public-ui"`` /
    ``"public-api"`` = one listener of the v0.2 node (an ASGI app). ``port`` is the
    local listener's port for the ``is_local`` check (default: the server's port)."""
    if state is None:
        if config is None:
            config = "config.yaml"
        if not isinstance(config, Config):
            config = load_config(config)
        state = build_state(config)

    if surface is not None:
        if surface not in SURFACES:
            raise ValueError(f"surface must be one of {SURFACES}, got {surface!r}")
        if surface == "local":
            return _create_local(state, port)
        if surface == "public-ui":
            return _create_public_ui(state)
        return _create_public_api(state)

    from .api import build_api_router
    from .web import build_web_router

    async def close() -> None:
        await state.router.aclose()

    app = _new_app(state, close)
    app.include_router(build_api_router(state))
    app.include_router(build_web_router(state))
    return app
