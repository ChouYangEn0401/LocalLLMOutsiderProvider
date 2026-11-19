"""Who is calling? Shared by the API layer (API keys) and the web UI (session cookie).

The API never accepts the session cookie and the UI never accepts API keys: the
two layers are separate on purpose (no CSRF exposure on the API, no key leakage
into the browser).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from starlette.requests import Request

from .config import Account, Config
from .store import ApiKey, Session, Store

SESSION_COOKIE = "llmop_session"


class AuthError(Exception):
    def __init__(self, message: str, code: str = "invalid_api_key", status: int = 401):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass
class ApiCaller:
    account: Account
    key: ApiKey


@dataclass
class WebUser:
    account: Account
    session: Session
    session_id: str


def _peer_is_trusted(peer: str, cfg: Config) -> bool:
    if not peer:
        return False
    for item in cfg.server.trusted_proxies:
        try:
            if "/" in item:
                if ipaddress.ip_address(peer) in ipaddress.ip_network(item, strict=False):
                    return True
            elif peer == item:
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request, cfg: Config) -> str:
    """Real client IP. Forwarded headers are trusted only from a trusted proxy peer
    (cloudflared connects from 127.0.0.1, or from the compose network)."""
    peer = request.client.host if request.client else ""
    if _peer_is_trusted(peer, cfg):
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return peer


def is_forwarded(request: Request) -> bool:
    """True when the request came through a tunnel / reverse proxy."""
    h = request.headers
    return any(h.get(k) for k in ("cf-connecting-ip", "x-forwarded-for", "x-forwarded-host"))


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return '"https"' in request.headers.get("cf-visitor", "")


def extract_api_key(request: Request) -> str | None:
    """``Authorization: Bearer <key>`` (OpenAI) or ``x-api-key: <key>`` (Anthropic)."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
        if token:
            return token
    token = request.headers.get("x-api-key", "").strip()
    return token or None


def authenticate_api(request: Request, cfg: Config, store: Store) -> ApiCaller:
    key = extract_api_key(request)
    if not key:
        raise AuthError(
            "Missing API key. Send 'Authorization: Bearer sk-llop-...' "
            "(create a key on the web dashboard)."
        )
    rec = store.find_api_key(key)
    if rec is None:
        raise AuthError("Invalid or revoked API key.")
    account = cfg.account(rec.username)
    if account is None:
        raise AuthError("The account for this API key no longer exists.")
    if account.disabled:
        raise AuthError("This account is disabled.", code="account_disabled", status=403)
    return ApiCaller(account=account, key=rec)


def key_allows_plaintext(key: ApiKey, account: Account | None = None) -> bool:
    """May this key be used on the plaintext ``/v1`` path? Keys marked
    ``encryption: required`` only work through the encrypted relay (``/e2e/v1``).
    ``account`` is accepted for future per-account policy; a disabled account never
    gets plaintext access."""
    if account is not None and account.disabled:
        return False
    return getattr(key, "encryption", "optional") != "required"


def web_user(request: Request, cfg: Config, store: Store) -> WebUser | None:
    sid = request.cookies.get(SESSION_COOKIE, "")
    if not sid:
        return None
    sess = store.get_session(sid)
    if sess is None:
        return None
    account = cfg.account(sess.username)
    if account is None or account.disabled:
        store.delete_session(sid)
        return None
    return WebUser(account=account, session=sess, session_id=sid)
