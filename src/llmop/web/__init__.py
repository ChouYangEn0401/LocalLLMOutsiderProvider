"""Web UI layer (``/`` and ``/ui/*``): login page, usage dashboard, API key
management, playground and the admin page. Session-cookie auth only."""

from .routes import build_web_router
from .throttle import LoginThrottle

__all__ = ["build_web_router", "LoginThrottle"]
