"""Web UI layer: login, dashboard (usage, quota, API keys, playground) and admin.

Everything here authenticates with the session cookie only — never with API keys
(the ``/v1`` API layer is the opposite). Pages are plain HTML files from
``web/static/`` driven by vanilla JS that talks to the JSON endpoints under
``/ui/api/``.

Security model:

* Session cookie ``HttpOnly; SameSite=Lax; Path=/`` (+ ``Secure`` on https or when
  ``server.cookie_secure: always``). Only ``sha256(session id)`` is stored.
* Every state-changing request (POST/DELETE, except login) needs the header
  ``X-CSRF-Token`` equal to the session's token, and a present ``Origin`` header
  must match the host the request was sent to.
* Login: JSON only, Origin-checked, throttled per IP and per username, constant
  work for unknown usernames, password check off the event loop.
* Strict CSP (no inline script/style), ``X-Frame-Options: DENY``, ``nosniff``,
  ``no-referrer`` and ``no-store`` on every UI response.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import __version__, auth, security
from ..config import Account, Config
from ..quota import month_start, utc_offset_seconds
from ..store import ApiKey
from .throttle import LoginThrottle

log = logging.getLogger("llmop.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# Explicit map: Windows' registry-driven ``mimetypes`` sometimes says text/plain for .js.
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
_STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
PAGES = {"login": "login.html", "dashboard": "dashboard.html", "admin": "admin.html"}

MAX_BODY = 1_000_000  # bytes, JSON bodies of the UI API (playground included)
MAX_KEY_NAME = 60
MAX_EXPIRES_DAYS = 3650
PLAYGROUND_ROLES = ("system", "user", "assistant")
PLAYGROUND_MAX_MESSAGES = 100
PLAYGROUND_MAX_CHARS = 200_000
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------------- errors
class WebError(Exception):
    """Turned into ``{"error": {"message", "code"}}`` (same shape as the OpenAI-style
    errors that the playground passes through, so the JS has one error parser)."""

    def __init__(self, status: int, message: str, code: str = "error",
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.headers = headers or {}


def _error_json(status: int, message: str, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": code}},
                        status_code=status, headers=headers)


def apply_security_headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    if "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


class _UIRoute(APIRoute):
    """Adds the security headers to every response of the UI router (errors included)."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                response = await handler(request)
            except WebError as exc:
                response = _error_json(exc.status, exc.message, exc.code, exc.headers)
            except HTTPException as exc:
                response = _error_json(exc.status_code, str(exc.detail), "http_error",
                                       dict(exc.headers or {}))
            return apply_security_headers(response)

        return wrapped


# --------------------------------------------------------------------------- helpers
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_port(value: str) -> tuple[str, int | None] | None:
    value = (value or "").split(",")[0].strip().lower()
    if not value:
        return None
    try:
        parts = urlsplit("//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    return (host, port) if host else None


def origin_allowed(request: Request, public_url: str = "") -> bool:
    """A missing ``Origin`` is allowed (non-browser clients; the CSRF token still
    applies). A present one must name the host this request was sent to: the
    ``X-Forwarded-Host`` / ``Host`` header, or the configured / tunnel public URL."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        o = urlsplit(origin.strip())
        o_host, o_port = o.hostname, o.port
    except ValueError:
        return False
    scheme = (o.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS or not o_host:
        return False  # includes "null" (sandboxed iframes, file://, ...)
    o_default = o_port is None or o_port == _DEFAULT_PORTS[scheme]
    o_effective = o_port or _DEFAULT_PORTS[scheme]

    candidates = [request.headers.get("x-forwarded-host", ""), request.headers.get("host", "")]
    if public_url:
        try:
            candidates.append(urlsplit(public_url).netloc)
        except ValueError:
            pass
    for cand in candidates:
        hp = _host_port(cand)
        if hp is None or hp[0] != o_host:
            continue
        port = hp[1]
        if port is None and o_default:
            return True
        if port is not None and port == o_effective:
            return True
    return False


def _check_origin(request: Request, public_url: str) -> None:
    if not origin_allowed(request, public_url):
        raise WebError(403, "來源不符，已拒絕這個請求 (cross-origin request blocked).",
                       "origin_mismatch")


def _check_csrf(request: Request, user: auth.WebUser, public_url: str) -> None:
    _check_origin(request, public_url)
    token = request.headers.get("x-csrf-token", "")
    if not token or not hmac.compare_digest(token.encode("utf-8"),
                                            user.session.csrf.encode("utf-8")):
        raise WebError(403, "CSRF 驗證失敗，請重新整理頁面後再試 (CSRF check failed).",
                       "csrf_failed")


async def _read_json(request: Request) -> dict:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise WebError(415, "請用 JSON 傳送 (Content-Type: application/json).",
                       "unsupported_media_type")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise WebError(413, "內容太大 (request body too large).", "body_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise WebError(413, "內容太大 (request body too large).", "body_too_large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise WebError(400, "JSON 格式錯誤 (invalid JSON).", "invalid_json") from None
    if not isinstance(data, dict):
        raise WebError(400, "JSON 必須是物件 (expected a JSON object).", "invalid_json")
    return data


def _opt_int(value: Any, name: str, lo: int, hi: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise WebError(400, f"{name} 必須是整數 (integer).", "invalid_value")
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise WebError(400, f"{name} 必須是整數 (integer).", "invalid_value") from None
    if isinstance(value, float) and value != num:
        raise WebError(400, f"{name} 必須是整數 (integer).", "invalid_value")
    if not lo <= num <= hi:
        raise WebError(400, f"{name} 必須介於 {lo} 和 {hi} 之間.", "invalid_value")
    return num


def _local_midnight(d: date) -> float:
    return datetime(d.year, d.month, d.day).timestamp()


def _key_json(k: ApiKey, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "id": k.id,
        "name": k.name,
        "display": k.display,
        "created_at": k.created_at,
        "last_used_at": k.last_used_at,
        "expires_at": k.expires_at,
        "revoked_at": k.revoked_at,
        "active": k.active,
        "expired": k.expires_at is not None and k.expires_at <= now,
    }


def _usage_rows(rows: list[dict], key_labels: dict[str, str], *, with_user: bool) -> list[dict]:
    out = []
    for r in rows:
        key_id = r.get("key_id")
        item = {
            "id": r.get("id"),
            "ts": r.get("ts"),
            "endpoint": r.get("endpoint"),
            "model": r.get("model"),
            "backend": r.get("backend"),
            "prompt_tokens": r.get("prompt_tokens", 0),
            "completion_tokens": r.get("completion_tokens", 0),
            "total_tokens": r.get("total_tokens", 0),
            "usage_source": r.get("usage_source"),
            "status": r.get("status"),
            "stream": bool(r.get("stream")),
            "duration_ms": r.get("duration_ms", 0),
            "error": r.get("error", ""),
            "client_ip": r.get("client_ip", ""),
            "key_id": key_id,
            "via": "key" if key_id else "playground",
            "via_label": (key_labels.get(key_id) or key_id) if key_id else "playground",
        }
        if with_user:
            item["username"] = r.get("username")
        out.append(item)
    return out


def _key_labels(keys: list[ApiKey]) -> dict[str, str]:
    return {k.id: (f"{k.name} ({k.display})" if k.name else k.display) for k in keys}


def _plain(value: Any, depth: int = 0) -> Any:
    """JSON-safe copy of a value from another module (health rows, tunnel attrs)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth < 3 and isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    if depth < 3 and isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    return str(value)


def _health_row(row: Any) -> dict:
    if not isinstance(row, dict):
        row = dict(getattr(row, "__dict__", None) or {"name": str(row)})
    return {str(k): _plain(v) for k, v in row.items()}


def _tunnel_info(state, cfg: Config) -> dict:
    info: dict[str, Any] = {
        "enabled": cfg.tunnel.enabled,
        "mode": cfg.tunnel.mode,
        "running": state.tunnel is not None,
    }
    tunnel = state.tunnel
    if tunnel is not None:
        for attr in ("url", "status", "last_error", "restarts", "started_at", "mode"):
            try:
                value = getattr(tunnel, attr, None)
                if callable(value):
                    value = value()
            except Exception:  # a broken tunnel object must not break the admin page
                value = None
            if value is not None or attr not in info:
                info[attr] = _plain(value)
    return info


def _public_url_info(state, cfg: Config) -> dict:
    url = state.public_url()
    updated = None
    if not cfg.server.public_url:
        got = state.store.kv_get("public_url")
        if got and got[0] == url:
            updated = got[1]
    return {
        "public_url": url,
        "public_url_updated_at": updated,
        "public_url_fixed": bool(cfg.server.public_url),
        # A quick tunnel gets a new random URL on every start.
        "url_may_change": not cfg.server.public_url and cfg.tunnel.mode != "named",
    }


def _message_page(status: int, title: str, message: str, link: str, link_label: str) -> HTMLResponse:
    t, m = html.escape(title), html.escape(message)
    body = (
        "<!doctype html>\n<html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{t}</title>"
        f"<link rel=\"stylesheet\" href=\"/ui/static/app.css?v={__version__}\">"
        "<link rel=\"icon\" href=\"/ui/static/favicon.svg\" type=\"image/svg+xml\">"
        "</head><body class=\"page-center\"><main class=\"card narrow message-card\">"
        f"<p class=\"status-code\">{status}</p><h1>{t}</h1><p class=\"muted\">{m}</p>"
        f"<p><a class=\"btn primary\" href=\"{html.escape(link)}\">{html.escape(link_label)}</a></p>"
        "</main></body></html>"
    )
    return HTMLResponse(body, status_code=status)


# --------------------------------------------------------------------------- static
_static_cache: dict[str, tuple[float, int, bytes, str]] = {}


def _read_static(name: str) -> tuple[bytes, str] | None:
    """Bytes + ETag of a file directly inside STATIC_DIR, or None."""
    if not _STATIC_NAME.match(name) or ".." in name:
        return None
    path = (STATIC_DIR / name).resolve()
    if path.parent != STATIC_DIR or not path.is_file():
        return None
    st = path.stat()
    cached = _static_cache.get(name)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2], cached[3]
    data = path.read_bytes()
    etag = '"' + hashlib.sha256(data).hexdigest()[:20] + '"'
    _static_cache[name] = (st.st_mtime, st.st_size, data, etag)
    return data, etag


# --------------------------------------------------------------------------- router
def build_web_router(state, *, throttle: LoginThrottle | None = None) -> APIRouter:
    """``state`` is a :class:`llmop.app.AppState`. ``throttle`` is injectable for tests."""
    router = APIRouter(route_class=_UIRoute, include_in_schema=False)
    throttle = throttle or LoginThrottle()

    def current_user(request: Request) -> auth.WebUser | None:
        return auth.web_user(request, state.config, state.store)

    def require_user(request: Request) -> auth.WebUser:
        user = current_user(request)
        if user is None:
            raise WebError(401, "請先登入 (not logged in or session expired).", "not_logged_in")
        return user

    def require_admin(request: Request) -> auth.WebUser:
        user = require_user(request)
        if not user.account.is_admin:
            raise WebError(403, "需要管理員權限 (admin only).", "forbidden")
        return user

    def mutating_user(request: Request, *, admin: bool = False) -> auth.WebUser:
        user = require_admin(request) if admin else require_user(request)
        _check_csrf(request, user, state.public_url())
        return user

    def cookie_secure(request: Request, cfg: Config) -> bool:
        mode = cfg.server.cookie_secure
        return mode == "always" or (mode == "auto" and auth.is_https(request))

    def page(name: str) -> HTMLResponse:
        got = _read_static(PAGES[name])
        if got is None:  # pragma: no cover - packaging error
            return _message_page(500, "頁面遺失", f"{PAGES[name]} not found", "/", "首頁")
        text = got[0].decode("utf-8")
        text = text.replace("{{v}}", __version__).replace(
            "{{title}}", html.escape(state.config.server.title))
        return HTMLResponse(text)

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------------ pages
    @router.get("/")
    @router.get("/ui")
    @router.get("/ui/")
    async def home(request: Request):
        return redirect("/ui/dashboard" if current_user(request) else "/ui/login")

    @router.get("/ui/login")
    async def login_page(request: Request):
        if current_user(request):
            return redirect("/ui/dashboard")
        return page("login")

    @router.get("/ui/dashboard")
    async def dashboard_page(request: Request):
        if not current_user(request):
            return redirect("/ui/login")
        return page("dashboard")

    @router.get("/ui/admin")
    async def admin_page(request: Request):
        user = current_user(request)
        if user is None:
            return redirect("/ui/login?next=/ui/admin")
        if not user.account.is_admin:
            return _message_page(403, "沒有權限 (Forbidden)",
                                 "這一頁只有管理員 (admin) 可以看。", "/ui/dashboard", "回到儀表板")
        return page("admin")

    @router.get("/ui/static/{name:path}")
    async def static_file(request: Request, name: str):
        ext = Path(name).suffix.lower() if "/" not in name and "\\" not in name else ""
        got = _read_static(name) if ext in STATIC_TYPES else None
        if got is None:
            return _error_json(404, "Not found", "not_found")
        data, etag = got
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        return Response(data, media_type=STATIC_TYPES[ext], headers=headers)

    # ------------------------------------------------------------------ auth API
    @router.post("/ui/api/login")
    async def api_login(request: Request):
        cfg = state.config
        _check_origin(request, state.public_url())
        data = await _read_json(request)
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str) \
                or not username.strip() or not password:
            raise WebError(400, "請輸入帳號與密碼 (username and password required).",
                           "missing_credentials")
        username = username.strip()
        ip = auth.client_ip(request, cfg)

        wait = throttle.retry_after(ip, username)
        if wait:
            minutes = max(1, (wait + 59) // 60)
            log.warning("login throttled user=%r ip=%s", username[:64], ip)
            raise WebError(
                429,
                f"登入失敗次數過多，請約 {minutes} 分鐘後再試 (too many failed logins).",
                "too_many_attempts",
                headers={"Retry-After": str(wait)},
            )
        # Count the attempt up front so concurrent guesses cannot all slip past the
        # check above while scrypt runs; forgiven on success.
        throttle.failure(ip, username)

        account = cfg.account(username) if len(username) <= 64 else None
        if account is None or len(password) > 1024:
            await run_in_threadpool(security.dummy_verify, password[:1024])
            ok = False
        else:
            ok = await run_in_threadpool(security.verify_password, password,
                                         account.password_hash)
        if not ok or account is None:
            log.warning("login failed user=%r ip=%s", username[:64], ip)
            raise WebError(401, "帳號或密碼錯誤 (wrong username or password).",
                           "invalid_credentials")
        throttle.forgive(ip, username)
        if account.disabled:
            raise WebError(403, "此帳號已停用，請聯絡管理員 (account disabled).",
                           "account_disabled")

        old_sid = request.cookies.get(auth.SESSION_COOKIE)
        if old_sid:  # session rotation: never reuse a pre-login session id
            state.store.delete_session(old_sid)
        state.store.purge_expired_sessions()
        ttl = max(1, int(cfg.server.session_minutes)) * 60
        sid, sess = state.store.create_session(
            account.username, ttl, ip=ip, user_agent=request.headers.get("user-agent", ""))
        log.info("login ok user=%s ip=%s", account.username, ip)
        resp = JSONResponse({
            "ok": True,
            "username": account.username,
            "display_name": account.display_name or account.username,
            "role": account.role,
            "csrf": sess.csrf,
            "session_expires_at": sess.expires_at,
        })
        resp.set_cookie(auth.SESSION_COOKIE, sid, max_age=ttl, path="/", httponly=True,
                        samesite="lax", secure=cookie_secure(request, cfg))
        return resp

    @router.post("/ui/api/logout")
    async def api_logout(request: Request):
        cfg = state.config
        user = current_user(request)
        if user is not None:
            _check_csrf(request, user, state.public_url())
            state.store.delete_session(user.session_id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=cookie_secure(request, cfg))
        return resp

    @router.get("/ui/api/me")
    async def api_me(request: Request):
        user = require_user(request)
        cfg = state.config
        acc = user.account
        return JSONResponse({
            "username": acc.username,
            "display_name": acc.display_name or acc.username,
            "role": acc.role,
            "is_admin": acc.is_admin,
            "csrf": user.session.csrf,
            "session_expires_at": user.session.expires_at,
            "models": list(acc.models),
            "max_api_keys": acc.max_api_keys,
            "title": cfg.server.title,
            "version": __version__,
            "server_time": time.time(),
            **_public_url_info(state, cfg),
        })

    # ------------------------------------------------------------------ usage
    @router.get("/ui/api/usage")
    async def api_usage(request: Request):
        user = require_user(request)
        acc = user.account
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            raise WebError(400, "days 必須是整數 (integer).", "invalid_value") from None
        days = min(max(days, 1), 366)
        first_day = date.today() - timedelta(days=days - 1)
        since = _local_midnight(first_day)

        rows = {r["day"]: r for r in state.store.usage_by_day(
            acc.username, since, utc_offset_seconds())}
        by_day = []
        for i in range(days):
            d = (first_day + timedelta(days=i)).isoformat()
            r = rows.get(d) or {}
            by_day.append({
                "day": d,
                "requests": int(r.get("requests") or 0),
                "prompt": int(r.get("prompt") or 0),
                "completion": int(r.get("completion") or 0),
                "total": int(r.get("total") or 0),
            })
        labels = _key_labels(state.store.list_api_keys(acc.username, include_revoked=True))
        return JSONResponse({
            "days": days,
            "since": since,
            "quota": state.quota.usage(acc),
            "summary": state.store.usage_summary(acc.username, since),
            "by_day": by_day,
            "by_model": state.store.usage_by_model(acc.username, since),
            "recent": _usage_rows(state.store.recent_usage(acc.username, 50), labels,
                                  with_user=False),
        })

    # ------------------------------------------------------------------ API keys
    @router.get("/ui/api/keys")
    async def api_keys_list(request: Request):
        user = require_user(request)
        acc = user.account
        keys = state.store.list_api_keys(acc.username)
        now = time.time()
        return JSONResponse({
            "keys": [_key_json(k, now) for k in keys],
            "active_count": sum(1 for k in keys if k.active),
            "max_api_keys": acc.max_api_keys,
        })

    @router.post("/ui/api/keys")
    async def api_keys_create(request: Request):
        user = mutating_user(request)
        acc: Account = user.account
        data = await _read_json(request)
        name = data.get("name", "")
        if name is None:
            name = ""
        if not isinstance(name, str):
            raise WebError(400, "名稱必須是文字 (name must be a string).", "invalid_value")
        name = _CONTROL_CHARS.sub("", name).strip()
        if len(name) > MAX_KEY_NAME:
            raise WebError(400, f"名稱最多 {MAX_KEY_NAME} 個字 (name too long).", "invalid_value")
        if not name:
            name = datetime.now().strftime("key-%Y%m%d-%H%M")
        expires_days = _opt_int(data.get("expires_days"), "expires_days", 1, MAX_EXPIRES_DAYS)

        limit = int(acc.max_api_keys or 0)
        if limit <= 0:
            raise WebError(403, "這個帳號不能建立 API 金鑰 (API keys disabled for this account).",
                           "api_keys_disabled")
        if state.store.count_active_keys(acc.username) >= limit:
            raise WebError(
                409,
                f"已達 API 金鑰上限（{limit} 把），請先撤銷不用的金鑰 (key limit reached).",
                "max_api_keys",
            )
        expires_at = time.time() + expires_days * 86400 if expires_days else None
        key, rec = state.store.create_api_key(acc.username, name, expires_at)
        log.info("api key created user=%s id=%s", acc.username, rec.id)
        return JSONResponse({
            "ok": True,
            "key": key,
            "record": _key_json(rec),
            "warning": "完整金鑰只會顯示這一次，請立即複製保存 (shown only once).",
        }, status_code=201)

    @router.delete("/ui/api/keys/{key_id}")
    async def api_keys_revoke(request: Request, key_id: str):
        user = mutating_user(request)
        if not state.store.revoke_api_key(key_id, username=user.account.username):
            raise WebError(404, "找不到這把金鑰，或已經撤銷 (key not found).", "not_found")
        log.info("api key revoked user=%s id=%s", user.account.username, key_id)
        return JSONResponse({"ok": True, "id": key_id})

    # ------------------------------------------------------------------ models / playground
    @router.get("/ui/api/models")
    async def api_models(request: Request):
        user = require_user(request)
        acc = user.account
        # Same list as GET /v1/models (dedup, aliases, backend/model permissions).
        from llmop.api import visible_models

        try:
            objs = await visible_models(state, acc)
        except Exception as exc:
            log.warning("list_models failed: %s", exc)
            return JSONResponse({"data": [], "error": f"無法取得模型列表 (model list failed): {exc}"})
        data = [
            {
                "id": o["id"],
                "owned_by": o.get("owned_by", ""),
                "context_length": o.get("context_length"),
            }
            for o in objs
        ]
        return JSONResponse({"data": data, "error": None})

    @router.post("/ui/api/playground/chat")
    async def api_playground_chat(request: Request):
        user = mutating_user(request)
        cfg = state.config
        data = await _read_json(request)

        model = data.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise WebError(400, "請選擇模型 (model required).", "invalid_value")
        messages = data.get("messages")
        if messages is None:
            prompt = data.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise WebError(400, "請輸入訊息 (prompt or messages required).", "invalid_value")
            messages = [{"role": "user", "content": prompt}]
        if not isinstance(messages, list) or not messages \
                or len(messages) > PLAYGROUND_MAX_MESSAGES:
            raise WebError(400, f"messages 必須是 1–{PLAYGROUND_MAX_MESSAGES} 則訊息的陣列.",
                           "invalid_value")
        clean, chars = [], 0
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in PLAYGROUND_ROLES \
                    or not isinstance(m.get("content"), str):
                raise WebError(400, "每則訊息需要 role (system/user/assistant) 與文字 content.",
                               "invalid_value")
            chars += len(m["content"])
            clean.append({"role": m["role"], "content": m["content"]})
        if chars > PLAYGROUND_MAX_CHARS:
            raise WebError(400, "訊息太長 (messages too long).", "invalid_value")

        body: dict[str, Any] = {"model": model.strip(), "messages": clean, "stream": False}
        max_tokens = _opt_int(data.get("max_tokens"), "max_tokens", 1, 10_000_000)
        if max_tokens:
            body["max_tokens"] = max_tokens
        temperature = data.get("temperature")
        if temperature is not None and temperature != "":
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) \
                    or not 0 <= temperature <= 2:
                raise WebError(400, "temperature 必須介於 0 和 2 之間.", "invalid_value")
            body["temperature"] = float(temperature)

        try:
            from ..api.proxy import forward
        except ImportError as exc:  # pragma: no cover - API layer missing
            raise WebError(503, f"試玩功能暫時無法使用 (playground unavailable): {exc}",
                           "unavailable") from None
        return await forward(state, "chat.completions", body, account=user.account,
                             key_id=None, client_ip=auth.client_ip(request, cfg))

    # ------------------------------------------------------------------ admin
    @router.get("/ui/api/admin/overview")
    async def api_admin_overview(request: Request):
        require_admin(request)
        cfg = state.config
        store = state.store
        now = time.time()

        all_keys = store.list_api_keys(None, include_revoked=True)
        labels = _key_labels(all_keys)
        active_by_user: dict[str, int] = {}
        for k in all_keys:
            if k.active:
                active_by_user[k.username] = active_by_user.get(k.username, 0) + 1

        users = []
        for username, acc in cfg.accounts.items():
            users.append({
                "username": username,
                "display_name": acc.display_name or username,
                "role": acc.role,
                "disabled": acc.disabled,
                "models": list(acc.models),
                "max_api_keys": acc.max_api_keys,
                "active_keys": active_by_user.get(username, 0),
                "usage": state.quota.usage(acc),
            })
        # Usage recorded for usernames no longer in the config (removed accounts).
        known = set(cfg.accounts)
        orphans = [r for r in store.usage_by_user(month_start(now)) if r["username"] not in known]

        try:
            backends = await state.router.health()
        except Exception as exc:
            log.warning("backend health failed: %s", exc)
            backends = [{"name": "?", "type": "", "base_url": "", "reachable": False,
                         "model_count": 0, "error": str(exc)}]

        return JSONResponse({
            **_public_url_info(state, cfg),
            "tunnel": _tunnel_info(state, cfg),
            "config_error": state.holder.last_error,
            "config_path": str(cfg.path) if cfg.path else None,
            "version": __version__,
            "started_at": getattr(state, "started_at", None),
            "server_time": now,
            "backends": [_health_row(b) for b in backends],
            "users": users,
            "orphan_usage_month": orphans,
            "keys": [dict(_key_json(k, now), username=k.username)
                     for k in all_keys if k.revoked_at is None],
            "recent": _usage_rows(store.recent_usage(None, 100), labels, with_user=True),
        })

    @router.post("/ui/api/admin/revoke-key")
    async def api_admin_revoke_key(request: Request):
        user = mutating_user(request, admin=True)
        data = await _read_json(request)
        key_id = data.get("id")
        if not isinstance(key_id, str) or not key_id:
            raise WebError(400, "缺少金鑰 id (id required).", "invalid_value")
        if not state.store.revoke_api_key(key_id):
            raise WebError(404, "找不到這把金鑰，或已經撤銷 (key not found).", "not_found")
        log.info("api key revoked by admin=%s id=%s", user.account.username, key_id)
        return JSONResponse({"ok": True, "id": key_id})

    @router.post("/ui/api/admin/logout-user")
    async def api_admin_logout_user(request: Request):
        user = mutating_user(request, admin=True)
        data = await _read_json(request)
        username = data.get("username")
        if not isinstance(username, str) or not username:
            raise WebError(400, "缺少帳號 (username required).", "invalid_value")
        count = state.store.delete_user_sessions(username)
        log.info("sessions of %s deleted by admin=%s (%d)", username, user.account.username, count)
        return JSONResponse({"ok": True, "username": username, "sessions_deleted": count})

    # ------------------------------------------------------------------ fallbacks
    @router.get("/ui/{rest:path}")
    async def ui_not_found(request: Request, rest: str):
        if rest.startswith("api/"):
            return _error_json(404, "Not found", "not_found")
        return _message_page(404, "找不到頁面 (Not found)", "這個網址不存在。", "/", "回到首頁")

    return router
