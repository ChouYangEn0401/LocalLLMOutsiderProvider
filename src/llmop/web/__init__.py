"""Web UI layer (``/`` and ``/ui/*``): login page, the account portal (tier, usage
tables, own API keys, API link, queue) and the admin pages. Session-cookie auth only.

``build_web_router(state, surface)`` with ``surface`` ``"public"`` (account website,
no admin writes) or ``"local"`` (console, ``netguard.is_local`` only); ``surface``
omitted = the v0.1 combined UI (see :mod:`llmop.web.routes`)."""

from .routes import build_web_router
from .throttle import LoginThrottle

__all__ = ["build_web_router", "LoginThrottle"]
