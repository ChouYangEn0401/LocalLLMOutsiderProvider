"""管理介面：/admin/api/* 的 JSON API、登入與 CSRF，以及 /admin/ 的靜態頁面。

安全規則：
- admin_local_only 為 true 時，帶 Cf-Connecting-Ip 標頭或對端不是回送位址的請求，所有 /admin 路徑一律 404。
- 工作階段權杖是 32 個隨機位元組，放在 HttpOnly、SameSite=Strict 的 cookie；只存在記憶體，12 小時到期。
- 會改變狀態的呼叫都要帶 X-LLMOP-CSRF（等於登入時拿到的值），而且本文必須是 JSON。
- 同一個來源位址在 15 分鐘內失敗 5 次就暫時鎖住登入。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from llmop.crypto import signing_key_id
from llmop.protocol import ProtocolError, b64u_decode, b64u_encode

from . import ADMIN, CONFIG, HUB, STORE
from .config import is_loopback
from .store import ApiKey, Worker, first_day_of_window, hash_password, utc_day

STATIC_DIR = Path(__file__).parent / "static"

SESSION_COOKIE = "llmop_admin"
CSRF_HEADER = "X-LLMOP-CSRF"
SESSION_SECONDS = 12 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_NAME_LEN = 100
MAX_USAGE_DAYS = 366

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass
class Session:
    csrf: str
    expires_at: float


@dataclass
class AdminState:
    """記憶體中的管理狀態：工作階段（以權杖的 SHA-256 為鍵）與登入失敗紀錄。"""

    sessions: dict[str, Session] = field(default_factory=dict)
    failures: dict[str, list[float]] = field(default_factory=dict)

    def recent_failures(self, addr: str, now: float) -> list[float]:
        recent = [t for t in self.failures.get(addr, []) if now - t < LOGIN_WINDOW_SECONDS]
        if recent:
            self.failures[addr] = recent
        else:
            self.failures.pop(addr, None)
        return recent

    def find_session(self, token: str | None, now: float) -> Session | None:
        if not token:
            return None
        key = _token_key(token)
        session = self.sessions.get(key)
        if session is not None and session.expires_at <= now:
            del self.sessions[key]
            return None
        return session


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 中介層與共用工具
# ---------------------------------------------------------------------------


def is_local_request(request: web.Request) -> bool:
    """請求是否直接來自本機（不是經過 Cloudflare 通道）。"""
    return "Cf-Connecting-Ip" not in request.headers and is_loopback(request.remote or "")


def client_address(request: web.Request) -> str:
    """登入限流用的來源位址：對端是本機時採用 Cf-Connecting-Ip，否則用對端位址。"""
    peer = request.remote or ""
    if is_loopback(peer):
        return request.headers.get("Cf-Connecting-Ip", peer)
    return peer


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


@web.middleware
async def admin_guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    if _is_admin_path(request.path) and request.app[CONFIG].admin_local_only and not is_local_request(request):
        raise web.HTTPNotFound()
    return await handler(request)


async def add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)


_ERROR_CLASSES: dict[int, type[web.HTTPException]] = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
    415: web.HTTPUnsupportedMediaType,
    429: web.HTTPTooManyRequests,
    503: web.HTTPServiceUnavailable,
}


def _error(status: int, code: str, message: str) -> web.HTTPException:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
    return _ERROR_CLASSES[status](text=body, content_type="application/json")


def _require_session(request: web.Request) -> Session:
    session = request.app[ADMIN].find_session(request.cookies.get(SESSION_COOKIE), time.monotonic())
    if session is None:
        raise _error(401, "unauthorized", "請先登入")
    return session


def _check_csrf(request: web.Request, session: Session) -> None:
    if request.content_type != "application/json":
        raise _error(415, "unsupported_media_type", "本文必須是 application/json")
    token = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(token.encode("utf-8"), session.csrf.encode("utf-8")):
        raise _error(403, "csrf", "缺少或錯誤的 CSRF 權杖")


def _protected(handler: Handler, *, mutating: bool) -> Handler:
    """包一層：需要登入；會改變狀態的呼叫另外要求 CSRF 標頭與 JSON 本文。"""

    async def wrapper(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        if mutating:
            _check_csrf(request, session)
        return await handler(request)

    return wrapper


async def _json_body(request: web.Request) -> dict:
    text = await request.text()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise _error(400, "bad_request", "本文不是合法的 JSON") from None
    if not isinstance(data, dict):
        raise _error(400, "bad_request", "本文必須是 JSON 物件")
    return data


def _check_fields(data: dict, allowed: set[str]) -> None:
    unknown = data.keys() - allowed
    if unknown:
        raise _error(400, "bad_request", f"不認得的欄位：{', '.join(sorted(unknown))}")


def _valid_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NAME_LEN:
        raise _error(400, "bad_request", f"name 必須是 1 到 {MAX_NAME_LEN} 個字元")
    return value.strip()


def _valid_limit(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _error(400, "bad_request", f"{name} 必須是非負整數或 null")
    return value


def _valid_sig_public_key(value: object) -> str | None:
    """sig_public_key 必須是 32 位元組的 b64u（Ed25519 公鑰），或 null。"""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(b64u_decode(value)) != 32:
            raise ProtocolError("bad_envelope")
    except ProtocolError:
        raise _error(400, "bad_request", "sig_public_key 必須是 32 位元組 Ed25519 公鑰的 b64u") from None
    return value


def _key_json(key: ApiKey, today: dict | None = None) -> dict:
    sig_key_id = signing_key_id(b64u_decode(key.sig_public_key)) if key.sig_public_key else None
    data = {
        "id": key.id,
        "name": key.name,
        "sig_public_key": key.sig_public_key,
        "sig_key_id": sig_key_id,
        "daily_request_limit": key.daily_request_limit,
        "daily_token_limit": key.daily_token_limit,
        "created_at": key.created_at,
        "revoked_at": key.revoked_at,
    }
    if today is not None:
        data["today"] = today
    return data


def _worker_json(request: web.Request, worker: Worker) -> dict:
    store, hub = request.app[STORE], request.app[HUB]
    conn = hub.workers.get(worker.id)
    return {
        "id": worker.id,
        "name": worker.name,
        "online": conn is not None,
        "current_key_id": next(iter(conn.keys), None) if conn else None,
        "pending": len(conn.pending) if conn else 0,
        "max_concurrency": conn.max_concurrency if conn else None,
        "models": store.worker_models(worker.id),
        "keys": store.worker_keys(worker.id),
        "created_at": worker.created_at,
        "last_seen_at": worker.last_seen_at,
        "revoked_at": worker.revoked_at,
        "reg_expires_at": worker.reg_expires_at,
    }


def _today() -> str:
    return utc_day(time.time())


# ---------------------------------------------------------------------------
# 登入與工作階段
# ---------------------------------------------------------------------------


async def login(request: web.Request) -> web.Response:
    if request.content_type != "application/json":
        raise _error(415, "unsupported_media_type", "本文必須是 application/json")
    state = request.app[ADMIN]
    addr = client_address(request)
    now = time.monotonic()
    if len(state.recent_failures(addr, now)) >= LOGIN_MAX_FAILURES:
        raise _error(429, "too_many_attempts", "登入失敗次數過多，請 15 分鐘後再試")
    data = await _json_body(request)
    password = data.get("password")
    record = request.app[STORE].admin_password_record()
    if record is None:
        raise _error(503, "not_initialized", "尚未設定管理員密碼，請先執行 llmop gateway init")
    ok = False
    if isinstance(password, str) and 0 < len(password) <= 1024:
        salt, expected = record
        digest = await asyncio.get_running_loop().run_in_executor(None, hash_password, password, salt)
        ok = hmac.compare_digest(digest, expected)
    if not ok:
        state.failures.setdefault(addr, []).append(now)
        raise _error(401, "unauthorized", "密碼錯誤")

    token = b64u_encode(secrets.token_bytes(32))
    csrf = b64u_encode(secrets.token_bytes(32))
    state.sessions[_token_key(token)] = Session(csrf=csrf, expires_at=now + SESSION_SECONDS)
    response = web.json_response({"csrf": csrf})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_SECONDS,
        path="/admin",
        httponly=True,
        samesite="Strict",
        secure=True if request.headers.get("X-Forwarded-Proto") == "https" else None,
    )
    return response


async def logout(request: web.Request) -> web.Response:
    request.app[ADMIN].sessions.pop(_token_key(request.cookies.get(SESSION_COOKIE, "")), None)
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/admin")
    return response


async def session_info(request: web.Request) -> web.Response:
    session = request.app[ADMIN].find_session(request.cookies.get(SESSION_COOKIE), time.monotonic())
    if session is None:
        return web.json_response({"authenticated": False})
    return web.json_response({"authenticated": True, "csrf": session.csrf})


# ---------------------------------------------------------------------------
# API 金鑰
# ---------------------------------------------------------------------------


async def list_keys(request: web.Request) -> web.Response:
    rows = request.app[STORE].list_api_keys(_today())
    return web.json_response({"keys": [_key_json(key, today) for key, today in rows]})


async def create_key(request: web.Request) -> web.Response:
    data = await _json_body(request)
    _check_fields(data, {"name", "sig_public_key", "daily_request_limit", "daily_token_limit"})
    key, secret = request.app[STORE].create_api_key(
        name=_valid_name(data.get("name")),
        sig_public_key=_valid_sig_public_key(data.get("sig_public_key")),
        daily_request_limit=_valid_limit(data.get("daily_request_limit"), "daily_request_limit"),
        daily_token_limit=_valid_limit(data.get("daily_token_limit"), "daily_token_limit"),
    )
    # 明文金鑰只在這個回應出現一次。
    return web.json_response({"key": _key_json(key), "api_key": secret}, status=201)


async def update_key(request: web.Request) -> web.Response:
    data = await _json_body(request)
    _check_fields(data, {"sig_public_key", "daily_request_limit", "daily_token_limit"})
    changes: dict = {}
    if "sig_public_key" in data:
        changes["sig_public_key"] = _valid_sig_public_key(data["sig_public_key"])
    for name in ("daily_request_limit", "daily_token_limit"):
        if name in data:
            changes[name] = _valid_limit(data[name], name)
    key = request.app[STORE].update_api_key(request.match_info["key_id"], changes)
    if key is None:
        raise _error(404, "not_found", "找不到這把 API 金鑰")
    return web.json_response({"key": _key_json(key)})


async def revoke_key(request: web.Request) -> web.Response:
    key = request.app[STORE].revoke_api_key(request.match_info["key_id"])
    if key is None:
        raise _error(404, "not_found", "找不到這把 API 金鑰")
    return web.json_response({"key": _key_json(key)})


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


async def list_workers(request: web.Request) -> web.Response:
    workers = request.app[STORE].list_workers()
    return web.json_response({"workers": [_worker_json(request, w) for w in workers]})


async def create_worker(request: web.Request) -> web.Response:
    data = await _json_body(request)
    _check_fields(data, {"name"})
    worker, reg_token = request.app[STORE].create_worker(_valid_name(data.get("name")))
    # 明文註冊權杖只在這個回應出現一次。
    return web.json_response({"worker": _worker_json(request, worker), "reg_token": reg_token}, status=201)


async def revoke_worker(request: web.Request) -> web.Response:
    worker_id = request.match_info["worker_id"]
    worker = request.app[STORE].revoke_worker(worker_id)
    if worker is None:
        raise _error(404, "not_found", "找不到這台 worker")
    request.app[HUB].disconnect(worker_id)
    return web.json_response({"worker": _worker_json(request, worker)})


async def reissue_reg_token(request: web.Request) -> web.Response:
    worker_id = request.match_info["worker_id"]
    result = request.app[STORE].reissue_reg_token(worker_id)
    if result is None:
        if request.app[STORE].get_worker(worker_id) is None:
            raise _error(404, "not_found", "找不到這台 worker")
        raise _error(409, "revoked", "已撤銷的 worker 不能重新註冊")
    worker, reg_token = result
    return web.json_response({"worker": _worker_json(request, worker), "reg_token": reg_token})


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------


async def usage(request: web.Request) -> web.Response:
    try:
        days = int(request.query.get("days", "7"))
    except ValueError:
        days = 0
    if not 1 <= days <= MAX_USAGE_DAYS:
        raise _error(400, "bad_request", f"days 必須是 1 到 {MAX_USAGE_DAYS} 的整數")
    today = _today()
    first = first_day_of_window(today, days)
    return web.json_response({"from": first, "to": today, "usage": request.app[STORE].usage_since(first)})


# ---------------------------------------------------------------------------
# 靜態頁面與路由
# ---------------------------------------------------------------------------


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def redirect_index(request: web.Request) -> web.Response:
    raise web.HTTPFound("/admin/")


def setup(app: web.Application) -> None:
    app[ADMIN] = AdminState()
    app.middlewares.append(admin_guard)
    app.on_response_prepare.append(add_security_headers)

    r = app.router
    r.add_get("/admin", redirect_index)
    r.add_get("/admin/", index)
    r.add_static("/admin/static", STATIC_DIR)
    r.add_post("/admin/api/login", login)
    r.add_get("/admin/api/session", session_info)
    r.add_post("/admin/api/logout", _protected(logout, mutating=True))
    r.add_get("/admin/api/keys", _protected(list_keys, mutating=False))
    r.add_post("/admin/api/keys", _protected(create_key, mutating=True))
    r.add_patch("/admin/api/keys/{key_id}", _protected(update_key, mutating=True))
    r.add_post("/admin/api/keys/{key_id}/revoke", _protected(revoke_key, mutating=True))
    r.add_get("/admin/api/workers", _protected(list_workers, mutating=False))
    r.add_post("/admin/api/workers", _protected(create_worker, mutating=True))
    r.add_post("/admin/api/workers/{worker_id}/revoke", _protected(revoke_worker, mutating=True))
    r.add_post("/admin/api/workers/{worker_id}/reg-token", _protected(reissue_reg_token, mutating=True))
    r.add_get("/admin/api/usage", _protected(usage, mutating=False))
