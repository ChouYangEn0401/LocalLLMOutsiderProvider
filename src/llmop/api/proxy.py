"""``forward``: send one OpenAI-style request to a backend, enforcing the account's
model permissions and quota, and record its token usage.

Shared by the API layer (``/v1/*``) and the web playground (``key_id=None``), so it
does not depend on the incoming Request. It always returns a Response; errors are
OpenAI-shaped. A returned streaming Response must be sent (its cleanup - usage
record, quota lease, backend slot - runs while / after it is sent).

v0.2 (DISPATCH T-6, TIERS 7.3-7.5): the configured single ``model`` replaces the
request's ``model``; when ``state.scheduler`` exists its priority queue replaces the
per-backend semaphore (``: queue ...`` SSE comments while a stream waits, 429 +
``Retry-After`` when the wait is too long); after each request the usage tables are
charged (``QuotaManager.settle`` / ``UsageManager.charge``); with ``sanitize`` the
client never sees backend error details (they go to the log); a backend context
overflow becomes 400 ``context_length_exceeded``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import anyio
import httpx
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from ..backends import BackendAdapter, BackendError, ModelNotFound
from ..auth import usage_username
from ..backends.base import context_error, error_fields, is_context_overflow, parse_upstream_error
from ..config import Account, Config
from ..quota import Lease, QuotaDenied
from ..scheduler import TIERS as QUEUE_TIERS
from ..scheduler import QueueFull, QueueTimeout
from ..store import ApiKey, UsageEvent
from .errors import ApiError, openai_error
from .sse import SSEParser
from .tokens import chars_to_tokens, choice_chars, estimate_prompt_tokens, usage_from

if TYPE_CHECKING:
    from ..app import AppState

log = logging.getLogger("llmop.api")

ENDPOINTS = ("chat.completions", "completions", "embeddings")
MAX_TOKEN_KEYS = ("max_tokens", "max_completion_tokens")
KEEPALIVE = b": keepalive\n\n"
DONE = b"data: [DONE]\n\n"
_EOF = object()
QUEUE_NOTICE_SECONDS = 5.0  # ": queue ..." comment interval while a stream waits (TIERS 7.4)

# Client-facing messages for backend failures when details must stay in the log.
_GENERIC_ERRORS = {
    404: "模型目前無法使用 (the model is not available right now).",
    429: "模型伺服器忙碌中，請稍後再試 (the model server is busy; try again later).",
    503: "模型伺服器目前無法連線 (the model server is not reachable right now).",
    504: "模型伺服器回應逾時 (the model server timed out).",
}
_GENERIC_REJECTED = ("模型伺服器拒絕了這個請求，請檢查參數 "
                     "(the model server rejected the request; check the parameters).")
_GENERIC_FAILED = "模型伺服器發生錯誤 (the model server returned an error)."


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    # json.dumps directly (not JSONResponse): tolerates NaN from upstream embeddings.
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(body, status_code=status, headers=headers, media_type="application/json")


def sse_event(obj: Any) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


def model_permitted(account: Account, cfg: Config, requested: str, backend: str, upstream: str) -> bool:
    """May ``account`` use ``requested`` (routed to ``upstream`` on ``backend``)?

    An alias is allowed when the account lists the alias itself or its target;
    any name is allowed when the account lists the upstream model (``:latest``
    ignored) or ``"<backend>/<model>"``.
    """
    if requested in cfg.model_aliases and account.may_use_model(requested):
        return True
    return account.may_use_model(upstream) or account.may_use_model(f"{backend}/{upstream}")


# ---------------------------------------------------------------------- errors
def client_error(exc: BackendError, sanitize: bool, request_id: str = "") -> ApiError:
    """``exc`` as the client sees it. With ``sanitize`` the backend's own wording
    (and its name) only goes to the log; status, code and param are kept."""
    if not sanitize or exc.code == "context_length_exceeded":
        return ApiError.from_backend(exc)
    log.warning("backend error (request %s): HTTP %s %s: %s",
                request_id, exc.status, exc.code, exc.message)
    if exc.status < 500 and exc.status not in _GENERIC_ERRORS:
        message = _GENERIC_REJECTED
    else:
        message = _GENERIC_ERRORS.get(exc.status, _GENERIC_FAILED)
    return ApiError(exc.status, message, exc.type, exc.code, exc.param)


def queue_error(exc: QueueFull | QueueTimeout) -> ApiError:
    """TIERS 7.3/7.4: waited too long -> 429 ``queue_wait``; queue full -> 503,
    too many waiting requests of this account -> 429. Always with ``Retry-After``."""
    wait = max(1, int(exc.retry_after))
    headers = {"Retry-After": str(wait)}
    if isinstance(exc, QueueTimeout):
        pos = exc.position
        return ApiError(429, f"目前排第 {pos} 位，約 {wait} 秒後再試 "
                        f"(queue position {pos}, retry in ~{wait}s)",
                        code="queue_wait", headers=headers)
    if exc.reason == "account":
        return ApiError(429, f"你已有太多請求在排隊，約 {wait} 秒後再試 "
                        f"(too many of your requests are waiting, retry in ~{wait}s)",
                        code="queue_full", headers=headers)
    return ApiError(503, f"排隊人數已滿，約 {wait} 秒後再試 "
                    f"(the queue is full, retry in ~{wait}s)", code="queue_full", headers=headers)


def _body_error(adapter: BackendAdapter, obj: Any) -> BackendError:
    """An error object the backend sent inside a 200 response / stream event."""
    message, code, _ = error_fields(obj) or ("unknown error", None, None)
    if is_context_overflow(message, code):
        log.warning("backend %s: context overflow: %s", adapter.name, message)
        return context_error(adapter.name)
    return BackendError(502, f"Backend '{adapter.name}' error: {message}",
                        code=code or "backend_error", backend=adapter.name)


# ---------------------------------------------------------------------- validation
def _validate(endpoint: str, body: Any, need_model: bool = True) -> None:
    if not isinstance(body, dict):
        raise ApiError(400, "The request body must be a JSON object.", code="invalid_json")
    model = body.get("model")
    if need_model and (not isinstance(model, str) or not model.strip()):
        raise ApiError(400, "You must provide a model parameter.", param="model")
    if endpoint == "chat.completions" and not isinstance(body.get("messages"), list):
        raise ApiError(400, "'messages' must be a list of messages.", param="messages")
    if body.get("stream") is not None and not isinstance(body["stream"], bool):
        raise ApiError(400, "'stream' must be a boolean.", param="stream")
    for key in MAX_TOKEN_KEYS:
        v = body.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (
            isinstance(v, float) and not v.is_integer()
        ):
            raise ApiError(400, f"'{key}' must be an integer.", param=key)


def _clamp_max_tokens(body: dict, cap: int | None) -> None:
    """Enforce the lease's token cap on max_tokens / max_completion_tokens."""
    if cap is None:
        return
    present = [k for k in MAX_TOKEN_KEYS if body.get(k) is not None]
    for key in present:
        body[key] = min(int(body[key]), cap)
    if not present:
        body["max_tokens"] = cap


def _acquire(state: AppState, account: Account | None, key: ApiKey | None = None) -> Lease:
    try:
        if key is not None:
            return state.quota.acquire(account, key)
        return state.quota.acquire(account)
    except QuotaDenied as exc:
        if exc.code == "account_disabled":
            raise ApiError(403, exc.message, code="account_disabled") from None
        if getattr(exc, "status", 429) == 401:  # dead GUEST key
            raise ApiError(401, exc.message, code=exc.code) from None
        headers = dict(state.quota.headers(account)) if account is not None else {}
        if exc.retry_after:
            headers["Retry-After"] = str(int(exc.retry_after))
        # OpenAI uses type "insufficient_quota" / "requests" for these two codes.
        etype = "insufficient_quota" if exc.code == "insufficient_quota" else "requests"
        raise ApiError(429, exc.message, type=etype, code=exc.code, headers=headers) from None


def _queue_tier(lease: Lease, account: Account | None) -> str:
    tier = lease.queue_tier or (getattr(account, "tier", None) if account is not None else "guest")
    return tier if tier in QUEUE_TIERS else "normal"


# ---------------------------------------------------------------------- bookkeeping
class _Call:
    """One request that holds a quota lease: owns the lease and the backend slot
    (or scheduler slot), and writes its usage record exactly once (:meth:`finish`)."""

    def __init__(
        self, state: AppState, endpoint: str, account: Account | None, key_id: str | None,
        client_ip: str, client_model: str, request_id: str, stream: bool,
        adapter: BackendAdapter, lease: Lease, started: float, *,
        key: ApiKey | None = None, zone: str | None = None, sanitize: bool = False,
    ):
        self.state = state
        self.key = key
        self.usage_name = usage_username(account, key)
        self.zone = zone or "local"  # scheduler zone ("local" for the playground)
        self.sanitize = sanitize
        self.scheduler = getattr(state, "scheduler", None)
        self.tier = _queue_tier(lease, account)
        self.ticket: Any = None
        self._slot_cm: Any = None
        self.detail = ""  # backend error detail (for the usage record / log only)
        self.endpoint = endpoint
        self.account = account
        self.key_id = key_id
        self.client_ip = client_ip
        self.client_model = client_model
        self.request_id = request_id
        self.stream = stream
        self.adapter = adapter
        self.lease = lease
        self.started = started
        self.prompt_estimate = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usage_source = "none"
        self.finished = False
        self._slot = False

    async def acquire_slot(self) -> None:
        """Wait for a slot: the global scheduler when there is one (may raise
        QueueFull / QueueTimeout), else the backend's own semaphore."""
        if self.scheduler is not None:
            cm = self.scheduler.slot(self.usage_name, self.tier, self.zone)
            self.ticket = cm.ticket
            await cm.__aenter__()
            self._slot_cm = cm
            await self.adapter.acquire(gate=False)
        else:
            await self.adapter.acquire()
        self._slot = True

    async def release_slot(self) -> None:
        if self._slot:
            self._slot = False
            cm, self._slot_cm = self._slot_cm, None
            self.adapter.release(gate=cm is None)
            if cm is not None:
                await cm.__aexit__(None, None, None)

    def queued(self) -> bool:
        return self.ticket is not None and self.ticket.state == "waiting"

    def queue_comment(self) -> bytes:
        """``: queue position=3 eta=40 tier=normal`` (TIERS 7.4), or a keepalive."""
        t = self.ticket
        pos = t.position() if t is not None else None
        if not pos:
            return KEEPALIVE
        eta = math.ceil(t.eta() or 0)
        return f": queue position={pos} eta={eta} tier={self.tier}\n\n".encode()

    def api_error(self, exc: BaseException) -> ApiError | None:
        """ApiError for a backend / queue failure (None for anything else)."""
        if isinstance(exc, ApiError):
            return exc
        if isinstance(exc, (QueueFull, QueueTimeout)):
            return queue_error(exc)
        if isinstance(exc, BackendError):
            self.detail = exc.message
            return client_error(exc, self.sanitize, self.request_id)
        return None

    def set_usage(self, prompt: int, completion: int, source: str) -> None:
        self.prompt_tokens, self.completion_tokens, self.usage_source = prompt, completion, source

    def headers(self) -> dict[str, str]:
        h = {"x-request-id": self.request_id}
        if self.ticket is not None and self.ticket.started_at is not None:
            h["X-LLMOP-Queue-Waited"] = f"{self.ticket.waited:.1f}"
        try:
            if self.account is not None:
                h.update(self.state.quota.headers(self.account))
        except Exception:  # headers are a courtesy; never fail the request for them
            log.debug("x-ratelimit headers failed", exc_info=True)
        return h

    def _charge(self) -> None:
        """Charge the usage tables after the request (TIERS 2: always afterwards)."""
        tokens = self.prompt_tokens + self.completion_tokens
        quota = self.state.quota
        if getattr(quota, "tables", None) is not None:
            quota.settle(tokens, self.account, self.key)
            return
        usage = getattr(self.state, "usage", None)
        if usage is not None:
            usage.charge(tokens, self.account, self.key)

    async def finish(self, status: str, error: str = "") -> None:
        if self.finished:
            return
        self.finished = True
        await self.release_slot()
        if self.ticket is not None and self.ticket.started_at is None:
            # Never served (left the queue, timed out, rejected): no usage (TIERS 11).
            self.lease.release()
            return
        try:
            self._charge()
        except Exception:
            log.exception("could not charge usage tables for request %s", self.request_id)
        try:
            self.state.store.record_usage(UsageEvent(
                username=self.usage_name,
                endpoint=self.endpoint,
                model=self.client_model,
                backend=self.adapter.name,
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                usage_source=self.usage_source,
                status=status,
                stream=self.stream,
                duration_ms=int((time.monotonic() - self.started) * 1000),
                key_id=self.key_id,
                client_ip=self.client_ip,
                error=self.detail or error,
            ))
        except Exception:
            log.exception("could not record usage for request %s", self.request_id)
        finally:
            self.lease.release()


# ---------------------------------------------------------------------- entry point
async def forward(
    state: AppState,
    endpoint: str,
    body: Any,
    *,
    account: Account | None,
    key_id: str | None,
    client_ip: str = "",
    key: ApiKey | None = None,
    zone: str | None = None,
    stream_ok: bool = True,
    sanitize: bool | None = None,
) -> Response:
    """Forward an OpenAI request body to the right backend.

    ``endpoint`` is ``"chat.completions"``, ``"completions"`` or ``"embeddings"``.
    ``key_id`` is None for the web playground. ``account`` is None only for a GUEST
    ``key``. ``zone`` (``"local"`` / ``"public"``) picks the scheduler's wait limit
    (None = legacy call, queued as ``"local"``). ``stream_ok=False`` answers
    ``stream: true`` with the complete result as one SSE chunk (TIERS 7.5).
    ``sanitize`` (default: ``zone`` is set) keeps backend error details out of the
    response. Never raises for request/backend problems: returns an OpenAI-style
    error response instead.
    """
    request_id = "req_" + secrets.token_hex(12)
    if sanitize is None:
        sanitize = zone is not None
    try:
        return await _forward(state, endpoint, body, account, key_id, client_ip, request_id,
                              key=key, zone=zone, stream_ok=stream_ok, sanitize=sanitize)
    except ApiError as exc:
        return exc.response({"x-request-id": request_id})
    except Exception:
        log.exception("unexpected error forwarding %s (request %s)", endpoint, request_id)
        return openai_error(500, "Internal server error.", "server_error", "internal_error",
                            headers={"x-request-id": request_id})


async def _forward(
    state: AppState, endpoint: str, body: Any, account: Account | None, key_id: str | None,
    client_ip: str, request_id: str, *, key: ApiKey | None = None, zone: str | None = None,
    stream_ok: bool = True, sanitize: bool = False,
) -> Response:
    if endpoint not in ENDPOINTS:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    if account is None and key is None:
        raise ValueError("an account or a GUEST key is required")
    started = time.monotonic()
    cfg = state.config
    single = (getattr(cfg, "model", "") or "").strip()
    _validate(endpoint, body, need_model=not single)
    body = dict(body)  # we modify it; the caller's dict stays untouched
    if single:
        body["model"] = single  # one model for everybody (DISPATCH 3)
    model: str = body["model"]
    wants_stream = endpoint != "embeddings" and body.get("stream") is True
    stream = wants_stream and stream_ok

    try:
        adapter, upstream = await state.router.resolve(model)
    except ModelNotFound as exc:
        raise ApiError(404, exc.message, code="model_not_found", param="model") from None
    except BackendError as exc:
        raise client_error(exc, sanitize, request_id) from None
    if account is not None and not model_permitted(account, cfg, model, adapter.name, upstream):
        raise ApiError(403, f"Your account is not allowed to use the model '{model}'.",
                       code="model_not_allowed", param="model")

    lease = _acquire(state, account, key)
    call = _Call(state, endpoint, account, key_id, client_ip, model, request_id, wants_stream,
                 adapter, lease, started, key=key, zone=zone, sanitize=sanitize)
    try:
        if endpoint != "embeddings":
            _clamp_max_tokens(body, lease.max_tokens)
        body["model"] = upstream
        want_usage = False
        if wants_stream:
            opts = body.get("stream_options")
            opts = dict(opts) if isinstance(opts, dict) else {}
            want_usage = opts.get("include_usage") is True
            if stream:
                opts["include_usage"] = True  # we need the usage chunk for accounting
                body["stream_options"] = opts
            else:  # one complete answer, sent as a single SSE chunk
                body["stream"] = False
                body.pop("stream_options", None)
        call.prompt_estimate = estimate_prompt_tokens(endpoint, body)
        payload = adapter.prepare_body(endpoint, body)
        if stream:
            keepalive = cfg.server.sse_keepalive
            relay = _StreamRelay(call, payload, want_usage, keepalive if keepalive > 0 else None)
            return await relay.start()
        return await _complete(call, payload, as_sse=wants_stream, want_usage=want_usage)
    except BaseException as exc:
        # Not handed off to a streaming response: record and release now (idempotent).
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        await call.finish(status, getattr(exc, "message", "") or type(exc).__name__)
        raise


# ---------------------------------------------------------------------- non-streaming
async def _complete(call: _Call, payload: dict, *, as_sse: bool = False,
                    want_usage: bool = False) -> Response:
    adapter = call.adapter
    try:
        await call.acquire_slot()
    except (QueueFull, QueueTimeout) as exc:
        raise queue_error(exc) from None
    try:
        resp = await adapter.send(call.endpoint, payload, model=payload["model"])
        try:
            raw = await resp.aread()
        except httpx.HTTPError as exc:
            raise adapter.transport_error(exc) from exc
        finally:
            with anyio.CancelScope(shield=True):
                await resp.aclose()
    except BackendError as exc:
        raise call.api_error(exc) from None
    finally:
        await call.release_slot()

    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise call.api_error(BackendError(
            502, f"Backend '{adapter.name}' returned an invalid response.",
            code="bad_backend_response")) from None
    if data.get("error") and not data.get("choices") and not data.get("data"):
        err = _body_error(adapter, data)
        if err.code != "context_length_exceeded":
            err.code = "backend_error"
        raise call.api_error(err) from None

    if "model" in data:
        data["model"] = call.client_model  # the name the client asked for
    usage = usage_from(data.get("usage"))
    if usage is not None:
        call.set_usage(*usage, "backend")
    else:
        completion = chars_to_tokens(choice_chars(call.endpoint, data.get("choices")))
        call.set_usage(call.prompt_estimate, completion, "estimate")
    await call.finish("ok")
    if as_sse:
        chunk = _as_chunk(call.endpoint, data)
        if want_usage:
            chunk["usage"] = {"prompt_tokens": call.prompt_tokens,
                              "completion_tokens": call.completion_tokens,
                              "total_tokens": call.prompt_tokens + call.completion_tokens}
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **call.headers()}
        return Response(sse_event(chunk) + DONE, headers=headers, media_type="text/event-stream")
    return json_response(data, headers=call.headers())


def _as_chunk(endpoint: str, data: dict) -> dict:
    """A complete (non-streamed) answer as one stream chunk (TIERS 7.5: the public
    entrance answers ``stream: true`` with the whole result at once)."""
    chunk = {k: v for k, v in data.items() if k != "usage"}
    if endpoint != "chat.completions":
        return chunk  # text_completion chunks have the same shape as the full answer
    chunk["object"] = "chat.completion.chunk"
    choices = []
    for i, ch in enumerate(data.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
        delta = {k: v for k, v in msg.items() if v is not None}
        delta.setdefault("role", "assistant")
        if isinstance(delta.get("tool_calls"), list):
            delta["tool_calls"] = [{"index": j, **tc} if isinstance(tc, dict) else tc
                                   for j, tc in enumerate(delta["tool_calls"])]
        out = {"index": ch.get("index", i), "delta": delta,
               "finish_reason": ch.get("finish_reason")}
        if "logprobs" in ch:
            out["logprobs"] = ch["logprobs"]
        choices.append(out)
    chunk["choices"] = choices
    return chunk


# ---------------------------------------------------------------------- streaming
async def _next_chunk(chunks: AsyncIterator[bytes]) -> Any:
    try:
        return await chunks.__anext__()
    except StopAsyncIteration:
        return _EOF


async def _settle(task: asyncio.Task | None) -> None:
    """Cancel ``task`` if it is still running and wait for it. Never raises."""
    if task is None:
        return
    if not task.done():
        task.cancel()
        await asyncio.wait({task})
    if not task.cancelled():
        task.exception()  # mark as retrieved


class _RelayResponse(StreamingResponse):
    """StreamingResponse that always cleans up its relay, even when the client
    disconnects before / while the body is sent."""

    def __init__(self, content: AsyncIterator[bytes], *, relay: "_StreamRelay", **kwargs: Any):
        super().__init__(content, **kwargs)
        self._relay = relay

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()  # type: ignore[attr-defined]
                except RuntimeError:  # generator still running elsewhere
                    pass
                await self._relay.close("cancelled")


class _StreamRelay:
    """Relays one upstream SSE stream to the client.

    The upstream request starts right away. If it answers (or fails) within one
    keepalive interval, errors still get a proper HTTP status; otherwise the 200
    event stream starts with ``: keepalive`` comments while we keep waiting (model
    loading can be slow and tunnels drop silent connections). Every event is parsed
    to count usage, rewrite ``model`` to the client-facing name and drop the usage
    chunk the client did not ask for.
    """

    def __init__(self, call: _Call, payload: dict, want_usage: bool, keepalive: float | None):
        self.call = call
        self.payload = payload
        self.want_usage = want_usage
        self.keepalive = keepalive
        self.open_task: asyncio.Task | None = None
        self.read_task: asyncio.Task | None = None
        self.upstream: httpx.Response | None = None
        self.chunks: AsyncIterator[bytes] | None = None
        self.parser = SSEParser()
        self.status = "cancelled"  # until the stream ends normally or fails
        self.error = ""
        self.events = 0
        self.completion_chars = 0
        self.usage: tuple[int, int] | None = None
        self.last_id: str | None = None
        self.last_created: int | None = None
        self.closed = False

    async def _open(self) -> httpx.Response:
        await self.call.acquire_slot()
        try:
            return await self.call.adapter.send(
                self.call.endpoint, self.payload, model=self.payload["model"]
            )
        except BaseException:
            await self.call.release_slot()
            raise

    async def start(self) -> Response:
        self.open_task = asyncio.create_task(self._open())
        try:
            await asyncio.sleep(0)  # let the request enter the queue (if any)
            if not self.call.queued():
                await asyncio.wait({self.open_task}, timeout=self.keepalive)
            # else: queued -> send the SSE headers now and ": queue ..." comments
        except BaseException:
            await self.close("cancelled")
            raise
        if self.open_task.done():
            exc = self.open_task.exception()
            if exc is not None:
                await self.close("error", getattr(exc, "message", "") or repr(exc))
                err = self.call.api_error(exc)
                if err is not None:
                    raise err
                raise exc
            self.upstream = self.open_task.result()
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **self.call.headers()}
        return _RelayResponse(self._body(), relay=self, headers=headers,
                              media_type="text/event-stream")

    async def _body(self) -> AsyncIterator[bytes]:
        # Keepalive pattern (used twice, inlined so no nested generator is left
        # suspended on close): wait on a task with a timeout, emit a comment on
        # each timeout, never cancel the task while waiting.
        try:
            if self.upstream is None:
                assert self.open_task is not None
                if self.call.queued():
                    yield self.call.queue_comment()
                while not self.open_task.done():
                    queued = self.call.queued()
                    await asyncio.wait({self.open_task},
                                       timeout=QUEUE_NOTICE_SECONDS if queued else self.keepalive)
                    if not self.open_task.done():
                        yield self.call.queue_comment() if self.call.queued() else KEEPALIVE
                if self.open_task.exception() is not None:
                    yield self._fail(self.open_task.exception())
                    return
                self.upstream = self.open_task.result()

            self.chunks = chunks = self.upstream.aiter_bytes()
            while True:
                self.read_task = task = asyncio.create_task(_next_chunk(chunks))
                while not task.done():
                    await asyncio.wait({task}, timeout=self.keepalive)
                    if not task.done():
                        yield KEEPALIVE
                if task.exception() is not None:
                    yield self._fail(task.exception())
                    return
                chunk = task.result()
                if chunk is _EOF:
                    break
                for data in self.parser.feed(chunk):
                    out = self._relay(data)
                    if out:
                        yield out
                    if self.status == "error":
                        return

            for data in self.parser.flush():
                out = self._relay(data)
                if out:
                    yield out
                if self.status == "error":
                    return
            if self.events == 0:
                detail = parse_upstream_error(self.parser.stray)[0] if self.parser.stray else ""
                msg = f"Backend '{self.call.adapter.name}' returned no stream data"
                yield self._fail(BackendError(502, f"{msg}: {detail}" if detail else f"{msg}.",
                                              code="bad_backend_response"))
                return
            if self.want_usage and self.usage is None:
                yield self._usage_chunk()
            self.status = "ok"
            yield DONE
        finally:
            await self.close(self.status, self.error)

    def _relay(self, data: str) -> bytes | None:
        """One upstream event -> bytes for the client (None = drop it)."""
        if data.strip() == "[DONE]":
            return None  # we send our own [DONE] at the very end
        self.events += 1
        try:
            obj = json.loads(data)
        except ValueError:
            return "".join(f"data: {line}\n" for line in data.split("\n")).encode() + b"\n"
        if not isinstance(obj, dict):
            return sse_event(obj)
        if obj.get("error") and not obj.get("choices"):
            return self._fail(_body_error(self.call.adapter, obj))

        usage = usage_from(obj.get("usage"))
        if usage is not None:
            self.usage = usage
        choices = obj.get("choices")
        self.completion_chars += choice_chars(self.call.endpoint, choices)
        if isinstance(obj.get("id"), str):
            self.last_id = obj["id"]
        if isinstance(obj.get("created"), int):
            self.last_created = obj["created"]

        if not self.want_usage:
            if usage is not None and not choices:
                return None  # the usage-only chunk we requested for accounting
            if "usage" in obj and obj["usage"] is None:
                del obj["usage"]
        if "model" in obj:
            obj["model"] = self.call.client_model
        return sse_event(obj)

    def _fail(self, exc: BaseException) -> bytes:
        if isinstance(exc, httpx.HTTPError):
            exc = self.call.adapter.transport_error(exc)
        elif not isinstance(exc, (BackendError, QueueFull, QueueTimeout)):
            log.error("stream %s failed", self.call.request_id, exc_info=exc)
            exc = BackendError(502, "Internal error while streaming.", code="internal_error")
        err = self.call.api_error(exc)
        assert err is not None
        self.status, self.error = "error", getattr(exc, "message", "") or str(exc)
        return sse_event({"error": {"message": err.message, "type": err.type,
                                    "param": err.param, "code": err.code}})

    def _usage_chunk(self) -> bytes:
        """Usage chunk for a client that asked for one when the backend sent none."""
        prompt, completion = self.call.prompt_estimate, chars_to_tokens(self.completion_chars)
        chat = self.call.endpoint == "chat.completions"
        return sse_event({
            "id": self.last_id or f"{'chatcmpl' if chat else 'cmpl'}-{self.call.request_id}",
            "object": "chat.completion.chunk" if chat else "text_completion",
            "created": self.last_created or int(time.time()),
            "model": self.call.client_model,
            "choices": [],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion},
        })

    async def close(self, status: str, error: str = "") -> None:
        """Stop the upstream call, then record usage and release slot + lease. Idempotent."""
        if self.closed:
            return
        self.closed = True
        with anyio.CancelScope(shield=True):
            await _settle(self.read_task)
            await _settle(self.open_task)
            if self.chunks is not None:
                try:
                    await self.chunks.aclose()  # type: ignore[attr-defined]
                except Exception:
                    pass
            resp = self.upstream
            task = self.open_task
            if resp is None and task is not None and not task.cancelled() and task.exception() is None:
                resp = task.result()  # opened, but nobody consumed it
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    log.debug("error closing upstream stream", exc_info=True)
        if self.usage is not None:
            self.call.set_usage(*self.usage, "backend")
        elif self.events or status == "ok":
            self.call.set_usage(self.call.prompt_estimate, chars_to_tokens(self.completion_chars),
                                "estimate")
        await self.call.finish(status, error)
