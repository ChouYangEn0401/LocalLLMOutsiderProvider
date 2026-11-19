"""Admin write routes of the local console (``surface="local"``, and the v0.1
combined UI). **Never registered on the public surface.**

Every handler starts with ``mutating_admin(request)``: admin session + CSRF +
Origin, and on the local surface a session created there. One-time secrets (a new
GUEST key, a worker enrolment token) are returned once in the response that created
them and never logged.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Callable

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import data as wdata

log = logging.getLogger("llmop.web")

MAX_LABEL = 60
MAX_WORKER_NAME = 100
MAX_REASON = 200
MAX_CONFIG_TEXT = 512_000
MAX_GUEST_BUDGET = 10**12
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean_text(value: Any, name: str, max_len: int, *, required: bool = False) -> str:
    from .routes import WebError

    if value is None:
        value = ""
    if not isinstance(value, str):
        raise WebError(400, f"{name} 必須是文字 (must be a string).", "invalid_value")
    value = _CONTROL_CHARS.sub("", value).strip()
    if len(value) > max_len:
        raise WebError(400, f"{name} 最多 {max_len} 個字 (too long).", "invalid_value")
    if required and not value:
        raise WebError(400, f"請輸入 {name} ({name} required).", "invalid_value")
    return value


def _bool(value: Any, name: str) -> bool:
    from .routes import WebError

    if value is None:
        return False
    if not isinstance(value, bool):
        raise WebError(400, f"{name} 必須是 true 或 false.", "invalid_value")
    return value


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def register_console_routes(
    router: APIRouter, state, *, mutating_admin: Callable, require_admin: Callable,
    read_json: Callable, opt_int: Callable, sig_value: Callable, encryption_value: Callable,
) -> None:
    from .routes import WebError

    # ------------------------------------------------------------------ v0.1 actions
    @router.post("/ui/api/admin/revoke-key")
    async def api_admin_revoke_key(request: Request):
        user = mutating_admin(request)
        data = await read_json(request)
        key_id = data.get("id")
        if not isinstance(key_id, str) or not key_id:
            raise WebError(400, "缺少金鑰 id (id required).", "invalid_value")
        if not state.store.revoke_api_key(key_id):
            raise WebError(404, "找不到這把金鑰，或已經撤銷 (key not found).", "not_found")
        log.info("api key revoked by admin=%s id=%s", user.account.username, key_id)
        return JSONResponse({"ok": True, "id": key_id})

    @router.post("/ui/api/admin/logout-user")
    async def api_admin_logout_user(request: Request):
        user = mutating_admin(request)
        data = await read_json(request)
        username = data.get("username")
        if not isinstance(username, str) or not username:
            raise WebError(400, "缺少帳號 (username required).", "invalid_value")
        count = state.store.delete_user_sessions(username)
        log.info("sessions of %s deleted by admin=%s (%d)", username[:64],
                 user.account.username, count)
        return JSONResponse({"ok": True, "username": username, "sessions_deleted": count})

    # ------------------------------------------------------------------ GUEST keys
    @router.post("/ui/api/admin/guest-keys")
    async def api_admin_guest_key_create(request: Request):
        """Issue a GUEST key (TIERS 8): no account, own budget, optional expiry."""
        user = mutating_admin(request)
        data = await read_json(request)
        label = _clean_text(data.get("label"), "label", MAX_LABEL)
        if not label:
            label = time.strftime("guest-%Y%m%d-%H%M")
        budget = opt_int(data.get("budget_tokens"), "budget_tokens", 1, MAX_GUEST_BUDGET)
        expires_days = opt_int(data.get("expires_days"), "expires_days", 1, 3650)
        encryption = encryption_value(data.get("encryption"), "optional")
        sig = sig_value(data.get("sig_public_key"))
        if encryption == "required" and not sig:
            raise WebError(400, "加密的金鑰需要簽章公鑰 (an encrypted key needs a signing "
                                "public key).", "sig_public_key_required")
        reset = _bool(data.get("reset"), "reset")
        expires_at = time.time() + expires_days * 86400 if expires_days else None
        key, rec = state.store.create_guest_key(
            label, budget_tokens=budget, expires_at=expires_at, encryption=encryption,
            reset=reset, sig_public_key=sig)
        log.info("guest key created by admin=%s id=%s", user.account.username, rec.id)
        return JSONResponse({
            "ok": True,
            "key": key,
            "record": wdata.guest_key_json(state, rec, time.time(), masked=False),
            "warning": "完整金鑰只會顯示這一次，請立即複製保存 (shown only once).",
        }, status_code=201)

    # ------------------------------------------------------------------ bucket refill
    @router.post("/ui/api/admin/refill")
    async def api_admin_refill(request: Request):
        """Fill an account's (or a GUEST key's) 1.5-hour bucket and leave LOW."""
        user = mutating_admin(request)
        data = await read_json(request)
        username, key_id = data.get("username"), data.get("key_id")
        mgr = wdata.usage_manager(state)
        if isinstance(username, str) and username:
            acc = state.config.account(username)
            if acc is None:
                raise WebError(404, "找不到這個帳號 (account not found).", "not_found")
            status = mgr.fill(acc)
            subject = username
        elif isinstance(key_id, str) and key_id:
            rec = state.store.get_api_key(key_id)
            if rec is None or rec.username is not None:
                raise WebError(404, "找不到這把 GUEST 金鑰 (guest key not found).", "not_found")
            status = mgr.fill(None, rec)
            subject = key_id
        else:
            raise WebError(400, "需要 username 或 key_id.", "invalid_value")
        log.info("bucket refilled by admin=%s subject=%s", user.account.username, subject[:64])
        return JSONResponse({"ok": True, "status": wdata.plain(status)})

    # ------------------------------------------------------------------ workers
    def _worker_or_404(worker_id: str):
        w = state.store.get_worker(worker_id)
        if w is None:
            raise WebError(404, "找不到這台 worker (worker not found).", "not_found")
        return w

    @router.post("/ui/api/admin/workers")
    async def api_admin_worker_create(request: Request):
        user = mutating_admin(request)
        data = await read_json(request)
        name = _clean_text(data.get("name"), "name", MAX_WORKER_NAME, required=True)
        worker, reg_token = state.store.create_worker(name)
        log.info("worker created by admin=%s id=%s", user.account.username, worker.id)
        return JSONResponse({
            "ok": True,
            "worker": wdata.worker_json(state, worker, masked=False),
            "reg_token": reg_token,
            "warning": "註冊碼只會顯示這一次，1 小時內有效 (shown only once).",
        }, status_code=201)

    @router.post("/ui/api/admin/workers/{worker_id}/revoke")
    async def api_admin_worker_revoke(request: Request, worker_id: str):
        user = mutating_admin(request)
        _worker_or_404(worker_id)
        worker = state.store.revoke_worker(worker_id)
        hub = getattr(state, "hub", None)
        disconnect = getattr(hub, "disconnect", None)
        if callable(disconnect):
            try:
                await wdata.maybe_await(disconnect(worker_id))
            except Exception as exc:  # the revocation itself already happened
                log.warning("worker disconnect failed: %s", type(exc).__name__)
        log.info("worker revoked by admin=%s id=%s", user.account.username, worker_id)
        return JSONResponse({"ok": True, "worker": wdata.worker_json(state, worker, masked=False)})

    @router.post("/ui/api/admin/workers/{worker_id}/reg-token")
    async def api_admin_worker_reg_token(request: Request, worker_id: str):
        user = mutating_admin(request)
        _worker_or_404(worker_id)
        result = state.store.reissue_reg_token(worker_id)
        if result is None:
            raise WebError(409, "已撤銷的 worker 不能重新註冊 (worker revoked).", "revoked")
        worker, reg_token = result
        log.info("worker reg token reissued by admin=%s id=%s", user.account.username, worker_id)
        return JSONResponse({
            "ok": True,
            "worker": wdata.worker_json(state, worker, masked=False),
            "reg_token": reg_token,
            "warning": "註冊碼只會顯示這一次，1 小時內有效 (shown only once).",
        })

    # ------------------------------------------------------------------ API link
    @router.post("/ui/api/admin/api-link/rotate")
    async def api_admin_rotate_api_link(request: Request):
        """"Rotate API link" (MERGE_PLAN 9.4): new secret path, optionally a new host."""
        user = mutating_admin(request)
        data = await read_json(request)
        change_host = _bool(data.get("change_host"), "change_host")
        reason = _clean_text(data.get("reason"), "reason", MAX_REASON)
        link = getattr(state, "api_link", None)
        rotate = getattr(link, "rotate", None)
        if not callable(rotate):
            raise WebError(503, "API 連結功能尚未啟用 (API link not available).", "unavailable")
        try:
            await wdata.maybe_await(rotate(change_host=change_host, reason=reason))
        except WebError:
            raise
        except Exception as exc:
            log.warning("api link rotation failed: %s", type(exc).__name__)
            raise WebError(502, "更換 API 連結失敗，請看伺服器紀錄 (rotation failed).",
                           "rotate_failed") from None
        log.info("api link rotated by admin=%s change_host=%s", user.account.username,
                 change_host)
        return JSONResponse({"ok": True, "api_link": wdata.api_link_info(state)})

    # ------------------------------------------------------------------ raw config
    def _config_path():
        path = state.config.path
        if path is None:
            raise WebError(404, "這個伺服器沒有設定檔 (no config file).", "no_config_file")
        return path

    @router.get("/ui/api/admin/config")
    async def api_admin_config_get(request: Request):
        require_admin(request)
        from .. import configedit

        path = _config_path()
        try:
            text = configedit._read_text(path)
        except OSError:
            raise WebError(404, "讀不到設定檔 (config file unreadable).", "no_config_file") from None
        return JSONResponse({"name": path.name, "text": text, "sha256": _sha(text),
                             "config_error": state.holder.last_error})

    @router.put("/ui/api/admin/config")
    async def api_admin_config_put(request: Request):
        """Replace config.yaml with the edited text: validated first (a broken file is
        never written), refused if the file changed since it was loaded."""
        user = mutating_admin(request)
        from .. import configedit
        from ..config import ConfigError

        data = await read_json(request)
        text, base = data.get("text"), data.get("base_sha256")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CONFIG_TEXT:
            raise WebError(400, "設定內容不可空白或過長 (text required).", "invalid_value")
        if not isinstance(base, str) or not base:
            raise WebError(400, "缺少 base_sha256 (base_sha256 required).", "invalid_value")
        path = _config_path()
        try:
            current = configedit._read_text(path)
        except OSError:
            raise WebError(404, "讀不到設定檔 (config file unreadable).", "no_config_file") from None
        if _sha(current) != base:
            raise WebError(409, "設定檔在你編輯期間被改過，請重新載入 (config changed on disk).",
                           "config_changed")
        try:
            cfg = configedit._validate_text(text, path)
        except ConfigError as exc:
            raise WebError(400, f"設定檔有誤，沒有存檔：{exc}", "invalid_config") from None
        configedit._write_text(path, text)
        state.holder.set(cfg)
        log.info("config file edited by admin=%s", user.account.username)
        return JSONResponse({"ok": True, "sha256": _sha(text)})
