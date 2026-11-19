"""用戶端 API（加密面）：POST /e2e/v1/chat/completions、GET /e2e/v1/requests/{id}、GET /e2e/v1/models。

移植自 B 的 ``llmop.gateway.api``。閘道只解析信封的 protected 標頭；sealed 與 sig 是不透明字串，
原樣轉給 worker，不寫進紀錄或磁碟。

與 B 的差異（DISPATCH T-5）：
- API 金鑰與帳號用 A 的 store／config；沒有登記 ``sig_public_key`` 的金鑰一律拒絕。
- 准入用 T-2 的 ``QuotaManager.acquire``（等級與兩個用量表）；不再預扣 max_tokens（D5：事後記帳）。
- 派工用 T-3 的排程器（取代 ``8 × max_concurrency`` 與 ``max_inflight``）；``pending`` 回應帶 ``queue``。
- worker 回報的用量寫進 ``usage_events``（``usage_source="worker"``）並結算到用量表。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from llmop.crypto import verify_request
from llmop.protocol import (
    HTTP_STATUS,
    MAX_BODY_BYTES,
    Envelope,
    Header,
    ProtocolError,
    b64u_decode,
    check_timestamp,
    loads_strict,
)

from .hub import Hub, Job

log = logging.getLogger(__name__)

# 轉送給 worker 時，信封會包進 request 訊框（多了 type、request_id、client_sig_pub 等欄位），
# 訊框本身一樣受 MAX_BODY_BYTES 限制。所以這裡收本文時先扣一點餘裕。
_MAX_ENVELOPE_BYTES = MAX_BODY_BYTES - 4096

_BEARER = "Bearer "
ZONE = "public"  # 加密面只掛在公開 API 入口（DISPATCH 第 3 節）
ENDPOINT = "chat.completions"

# 排隊相關的錯誤碼（TIERS 7.3／7.4）。協定表裡沒有，用戶端以 HTTP 狀態判斷。
STATUS = {**HTTP_STATUS, "queue_wait": 429}

_DEFAULT_MESSAGES = {
    "payload_too_large": f"請求本文超過 {MAX_BODY_BYTES} 位元組",
    "unauthorized": "缺少或無效的 API 金鑰",
    "duplicate_request": "這個 request_id 已經送出過",
    "unknown_request": "找不到這筆請求",
    "internal_error": "閘道內部錯誤",
}


class Rejected(ProtocolError):
    """帶 Retry-After 的拒絕（額度、排隊）。"""

    def __init__(self, code: str, message: str = "", retry_after: int | None = None) -> None:
        super().__init__(code, message)
        self.retry_after = retry_after


def _now() -> float:
    """目前時間（Unix 秒）。測試會替換它來模擬換日。"""
    return time.time()


# ---------------------------------------------------------------------------
# 從 AppState 取用元件（T-9 才會接線；沒有就自己建一個並快取）
# ---------------------------------------------------------------------------


class SecureContext:
    """加密面需要的元件。``state`` 至少要有 ``store``；其餘以 ``getattr`` 取用。"""

    def __init__(self, state: Any, hub: Hub, sync_wait_seconds: float) -> None:
        self.state = state
        self.hub = hub
        self.sync_wait_seconds = sync_wait_seconds
        self._quota = None
        self._scheduler = None

    @property
    def store(self):
        return self.state.store

    @property
    def config(self):
        return getattr(self.state, "config", None)

    @property
    def quota(self):
        quota = getattr(self.state, "quota", None)
        if quota is not None:
            return quota
        if self._quota is None:
            from llmop.quota import QuotaManager

            self._quota = QuotaManager(self.store, tables=getattr(self.state, "usage", None))
        return self._quota

    @property
    def scheduler(self):
        scheduler = getattr(self.state, "scheduler", None)
        if scheduler is not None:
            return scheduler
        if self._scheduler is None:
            from llmop.scheduler import TIERS, Scheduler

            cfg = self.config
            q = getattr(cfg, "queue", None)
            if q is None:
                self._scheduler = Scheduler()
            else:
                tiers = getattr(cfg, "tiers", None) or {}
                self._scheduler = Scheduler(
                    slots=q.slots, max_length=q.max_length, max_per_account=q.max_per_account,
                    max_wait_seconds=q.max_wait_seconds, fair_share_every=q.fair_share_every,
                    max_concurrent={t: s.max_concurrent for t, s in tiers.items() if t in TIERS},
                )
        return self._scheduler


# ---------------------------------------------------------------------------
# 回應
# ---------------------------------------------------------------------------


def error_response(code: str, message: str = "", request_id: str | None = None,
                   retry_after: int | None = None) -> JSONResponse:
    body: dict = {"status": "error", "error": {"code": code, "message": message or _DEFAULT_MESSAGES.get(code, code)}}
    if request_id is not None:
        body = {"request_id": request_id, **body}
    headers = {"Retry-After": str(int(retry_after))} if retry_after else None
    return JSONResponse(body, status_code=STATUS.get(code, 500), headers=headers)


def job_response(job: Job) -> JSONResponse:
    if not job.done.is_set():
        body: dict = {"request_id": job.request_id, "status": "pending"}
        queue = job.queue_info()
        if queue is not None:
            body["queue"] = queue
        return JSONResponse(body, status_code=202)
    if job.code is not None:
        return error_response(job.code, job.message, job.request_id, job.retry_after)
    return JSONResponse({"request_id": job.request_id, "status": "done", "sealed": job.sealed})


# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------


def _authenticate(ctx: SecureContext, request: Request):
    """回傳 (ApiKey, Account | None)。GUEST key 沒有帳號。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith(_BEARER):
        raise ProtocolError("unauthorized", "缺少 Authorization: Bearer 標頭")
    key = ctx.store.find_api_key(auth[len(_BEARER):].strip())
    if key is None:
        raise ProtocolError("unauthorized", "API 金鑰無效、已到期或已撤銷")
    account = None
    if key.username is not None:
        cfg = ctx.config
        account = cfg.account(key.username) if cfg is not None else None
        if account is None or account.disabled:
            raise ProtocolError("unauthorized", "這把 API 金鑰的帳號不存在或已停用")
    return key, account


async def _read_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > _MAX_ENVELOPE_BYTES:
        raise ProtocolError("payload_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_ENVELOPE_BYTES:
            raise ProtocolError("payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _peek_request_id(obj: object) -> str | None:
    """信封不合法時，盡量從 protected 取出 request_id 放進錯誤回應。"""
    try:
        return Header.parse(obj["protected"]).request_id  # type: ignore[index]
    except (ProtocolError, KeyError, TypeError):
        return None


def _queue_tier(lease: Any, key: Any, account: Any) -> str:
    from llmop.scheduler import TIERS

    tier = getattr(lease, "queue_tier", None)
    if tier in TIERS:
        return tier
    if key.is_guest:
        return "guest"
    tier = getattr(account, "tier", None)
    return tier if tier in TIERS else "normal"


def _settle(ctx: SecureContext, key: Any, account: Any, lease: Any):
    """工作結束時呼叫：釋放租約；有轉送給 worker 的才記用量並結算到用量表。"""
    from llmop.store import UsageEvent

    def settle(job: Job) -> None:
        lease.release()
        if not job.relayed:
            return  # 排隊時就結束：不佔名額、不記用量（TIERS 11）
        prompt, completion = job.usage or (0, 0)
        if job.code is None:
            ctx.quota.settle(prompt + completion, account, key)
        ctx.store.record_usage(UsageEvent(
            username=account.username if account is not None else "",
            endpoint=ENDPOINT,
            model=job.model,
            backend=job.worker_name or job.worker_id or "",
            prompt_tokens=prompt,
            completion_tokens=completion,
            usage_source="worker" if job.usage is not None else "none",
            status="ok" if job.code is None else "error",
            duration_ms=int(((job.finished_at or job.started_at) - job.started_at) * 1000),
            key_id=key.id,
            error=job.code or "",
            ts=_now(),
        ))

    return settle


# ---------------------------------------------------------------------------
# 端點
# ---------------------------------------------------------------------------


async def chat_completions(ctx: SecureContext, request: Request) -> JSONResponse:
    started = time.monotonic()
    # 紀錄用的欄位，處理過程中逐步填入；只放可以公開記錄的值。
    trace: dict = {"request_id": None, "api_key_id": None, "worker_id": None, "model": None, "bytes": 0}
    ctx.hub.purge()
    try:
        response = await _chat(ctx, request, trace)
    except Rejected as exc:
        response = error_response(exc.code, exc.message, trace["request_id"], exc.retry_after)
    except ProtocolError as exc:
        response = error_response(exc.code, exc.message, trace["request_id"])
    except Exception as exc:
        # 只記例外類型，不記訊息：訊息裡可能夾帶輸入資料。
        log.error("chat 內部錯誤 request_id=%s error=%s", trace["request_id"], type(exc).__name__)
        response = error_response("internal_error", request_id=trace["request_id"])
    log.info(
        "chat request_id=%s api_key_id=%s worker_id=%s model=%s bytes=%d status=%d duration_ms=%d",
        trace["request_id"], trace["api_key_id"], trace["worker_id"], trace["model"], trace["bytes"],
        response.status_code, (time.monotonic() - started) * 1000,
    )
    return response


async def _chat(ctx: SecureContext, request: Request, trace: dict) -> JSONResponse:
    """依 PROTOCOL.md 3.1 的步驟處理一筆請求；任何一步失敗就丟 ProtocolError。"""
    from llmop.quota import QuotaDenied

    hub = ctx.hub

    # 1. 本文大小、JSON 形狀、解析標頭。
    raw = await _read_body(request)
    trace["bytes"] = len(raw)
    obj = loads_strict(raw)
    try:
        envelope = Envelope.from_json(obj)
    except ProtocolError:
        trace["request_id"] = _peek_request_id(obj)
        raise
    header = envelope.header
    trace["request_id"], trace["model"] = header.request_id, header.model

    # 2. API 金鑰（存在、未撤銷、未到期）與帳號。
    key, account = _authenticate(ctx, request)
    trace["api_key_id"] = key.id

    # 3. sig_key_id 與簽章。沒有登記簽章公鑰的金鑰一律拒絕。
    if not key.sig_public_key:
        raise ProtocolError("bad_signature", "這把 API 金鑰還沒有登記簽章公鑰")
    verify_request(envelope, b64u_decode(key.sig_public_key))

    # 4. 時間窗。
    check_timestamp(header.ts, int(_now()))

    # 5. 重複的 request_id。
    if hub.is_duplicate(key.id, header.request_id):
        raise ProtocolError("duplicate_request")

    # 6. 准入：等級與兩個用量表（T-2）。
    try:
        lease = ctx.quota.acquire(account, key)
    except QuotaDenied as exc:
        code = "unauthorized" if exc.status == 401 else "quota_exceeded"
        raise Rejected(code, exc.message, exc.retry_after) from None

    # 7. 有一台線上 worker 公告了 key_id 且提供 model（排到名額時會再路由一次）。
    try:
        conn = hub.route(header.key_id, header.model)
        scheduler = ctx.scheduler
        tier = _queue_tier(lease, key, account)
        who = account.username if account is not None else (getattr(lease, "subject", None) or f"key:{key.id}")
        slot = scheduler.slot(who, tier, ZONE)
    except BaseException:
        lease.release()
        raise
    trace["worker_id"] = conn.worker_id

    # 8–9. 排隊（T-3），排到後轉送給 worker；最多等 sync_wait_seconds。
    job = Job(
        api_key_id=key.id, request_id=header.request_id, key_id=header.key_id, model=header.model,
        started_at=asyncio.get_running_loop().time(), settle=_settle(ctx, key, account, lease),
    )
    hub.submit(job, envelope, key.sig_public_key, slot)
    await hub.wait(job, ctx.sync_wait_seconds)
    return job_response(job)


async def get_request(ctx: SecureContext, request: Request) -> JSONResponse:
    started = time.monotonic()
    request_id = request.path_params["request_id"]
    ctx.hub.purge()
    try:
        key, _ = _authenticate(ctx, request)
    except ProtocolError as exc:
        return error_response(exc.code, exc.message, request_id)
    job = ctx.hub.get(key.id, request_id)
    if job is None:
        response = error_response("unknown_request", request_id=request_id)
    else:
        await ctx.hub.wait(job, ctx.sync_wait_seconds)
        response = job_response(job)
    log.info(
        "poll request_id=%s api_key_id=%s status=%d duration_ms=%d",
        request_id, key.id, response.status_code, (time.monotonic() - started) * 1000,
    )
    return response


async def list_models(ctx: SecureContext, request: Request) -> JSONResponse:
    try:
        _authenticate(ctx, request)
    except ProtocolError as exc:
        return error_response(exc.code, exc.message)
    data = [
        {
            "id": model,
            "object": "model",
            "owned_by": conn.name,
            "llmop": {"key_id": key_id, "public_key": public_key},
        }
        for conn in ctx.hub.workers.values()
        for model in conn.models
        for key_id, public_key in conn.keys.items()
    ]
    return JSONResponse({"object": "list", "data": data})
