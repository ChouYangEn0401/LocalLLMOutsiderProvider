"""``forward``: send one OpenAI-style request to a backend, enforcing the account's
model permissions and quota, and record its token usage.

Shared by the API layer (``/v1/*``) and the web playground (``key_id=None``), so it
does not depend on the incoming Request. It always returns a Response; errors are
OpenAI-shaped. A returned streaming Response must be sent (its cleanup - usage
record, quota lease, backend slot - runs while / after it is sent).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import anyio
import httpx
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from ..backends import BackendAdapter, BackendError, ModelNotFound
from ..backends.base import error_fields, parse_upstream_error
from ..config import Account, Config
from ..quota import Lease, QuotaDenied
from ..store import UsageEvent
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


# ---------------------------------------------------------------------- validation
def _validate(endpoint: str, body: Any) -> None:
    if not isinstance(body, dict):
        raise ApiError(400, "The request body must be a JSON object.", code="invalid_json")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
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


def _acquire(state: AppState, account: Account) -> Lease:
    try:
        return state.quota.acquire(account)
    except QuotaDenied as exc:
        if exc.code == "account_disabled":
            raise ApiError(403, exc.message, code="account_disabled") from None
        headers = dict(state.quota.headers(account))
        if exc.retry_after:
            headers["Retry-After"] = str(int(exc.retry_after))
        # OpenAI uses type "insufficient_quota" / "requests" for these two codes.
        etype = "insufficient_quota" if exc.code == "insufficient_quota" else "requests"
        raise ApiError(429, exc.message, type=etype, code=exc.code, headers=headers) from None


# ---------------------------------------------------------------------- bookkeeping
class _Call:
    """One request that holds a quota lease: owns the lease and the backend slot,
    and writes its usage record exactly once (:meth:`finish`)."""

    def __init__(
        self, state: AppState, endpoint: str, account: Account, key_id: str | None,
        client_ip: str, client_model: str, request_id: str, stream: bool,
        adapter: BackendAdapter, lease: Lease, started: float,
    ):
        self.state = state
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
        await self.adapter.acquire()
        self._slot = True

    def release_slot(self) -> None:
        if self._slot:
            self._slot = False
            self.adapter.release()

    def set_usage(self, prompt: int, completion: int, source: str) -> None:
        self.prompt_tokens, self.completion_tokens, self.usage_source = prompt, completion, source

    def headers(self) -> dict[str, str]:
        h = {"x-request-id": self.request_id}
        try:
            h.update(self.state.quota.headers(self.account))
        except Exception:  # headers are a courtesy; never fail the request for them
            log.debug("x-ratelimit headers failed", exc_info=True)
        return h

    def finish(self, status: str, error: str = "") -> None:
        if self.finished:
            return
        self.finished = True
        self.release_slot()
        try:
            self.state.store.record_usage(UsageEvent(
                username=self.account.username,
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
                error=error,
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
    account: Account,
    key_id: str | None,
    client_ip: str = "",
) -> Response:
    """Forward an OpenAI request body to the right backend.

    ``endpoint`` is ``"chat.completions"``, ``"completions"`` or ``"embeddings"``.
    ``key_id`` is None for the web playground. Never raises for request/backend
    problems: returns an OpenAI-style error response instead.
    """
    request_id = "req_" + secrets.token_hex(12)
    try:
        return await _forward(state, endpoint, body, account, key_id, client_ip, request_id)
    except ApiError as exc:
        return exc.response({"x-request-id": request_id})
    except Exception:
        log.exception("unexpected error forwarding %s (request %s)", endpoint, request_id)
        return openai_error(500, "Internal server error.", "server_error", "internal_error",
                            headers={"x-request-id": request_id})


async def _forward(
    state: AppState, endpoint: str, body: Any, account: Account, key_id: str | None,
    client_ip: str, request_id: str,
) -> Response:
    if endpoint not in ENDPOINTS:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    started = time.monotonic()
    cfg = state.config
    _validate(endpoint, body)
    body = dict(body)  # we modify it; the caller's dict stays untouched
    model: str = body["model"]
    stream = endpoint != "embeddings" and body.get("stream") is True

    try:
        adapter, upstream = await state.router.resolve(model)
    except ModelNotFound as exc:
        raise ApiError(404, exc.message, code="model_not_found", param="model") from None
    except BackendError as exc:
        raise ApiError.from_backend(exc) from None
    if not model_permitted(account, cfg, model, adapter.name, upstream):
        raise ApiError(403, f"Your account is not allowed to use the model '{model}'.",
                       code="model_not_allowed", param="model")

    lease = _acquire(state, account)
    call = _Call(state, endpoint, account, key_id, client_ip, model, request_id, stream,
                 adapter, lease, started)
    try:
        if endpoint != "embeddings":
            _clamp_max_tokens(body, lease.max_tokens)
        body["model"] = upstream
        want_usage = False
        if stream:
            opts = body.get("stream_options")
            opts = dict(opts) if isinstance(opts, dict) else {}
            want_usage = opts.get("include_usage") is True
            opts["include_usage"] = True  # we need the usage chunk for accounting
            body["stream_options"] = opts
        call.prompt_estimate = estimate_prompt_tokens(endpoint, body)
        payload = adapter.prepare_body(endpoint, body)
        if stream:
            keepalive = cfg.server.sse_keepalive
            relay = _StreamRelay(call, payload, want_usage, keepalive if keepalive > 0 else None)
            return await relay.start()
        return await _complete(call, payload)
    except BaseException as exc:
        # Not handed off to a streaming response: record and release now (idempotent).
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        call.finish(status, getattr(exc, "message", "") or type(exc).__name__)
        raise


# ---------------------------------------------------------------------- non-streaming
async def _complete(call: _Call, payload: dict) -> Response:
    adapter = call.adapter
    await call.acquire_slot()
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
        raise ApiError.from_backend(exc) from None
    finally:
        call.release_slot()

    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ApiError(502, f"Backend '{adapter.name}' returned an invalid response.",
                       code="bad_backend_response")
    if data.get("error") and not data.get("choices") and not data.get("data"):
        message = (error_fields(data) or ("unknown error",))[0]
        raise ApiError(502, f"Backend '{adapter.name}' error: {message}", code="backend_error")

    if "model" in data:
        data["model"] = call.client_model  # the name the client asked for
    usage = usage_from(data.get("usage"))
    if usage is not None:
        call.set_usage(*usage, "backend")
    else:
        completion = chars_to_tokens(choice_chars(call.endpoint, data.get("choices")))
        call.set_usage(call.prompt_estimate, completion, "estimate")
    call.finish("ok")
    return json_response(data, headers=call.headers())


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
            self.call.release_slot()
            raise

    async def start(self) -> Response:
        self.open_task = asyncio.create_task(self._open())
        try:
            await asyncio.wait({self.open_task}, timeout=self.keepalive)
        except BaseException:
            await self.close("cancelled")
            raise
        if self.open_task.done():
            exc = self.open_task.exception()
            if exc is not None:
                await self.close("error", getattr(exc, "message", "") or repr(exc))
                if isinstance(exc, BackendError):
                    raise ApiError.from_backend(exc)
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
                while not self.open_task.done():
                    await asyncio.wait({self.open_task}, timeout=self.keepalive)
                    if not self.open_task.done():
                        yield KEEPALIVE
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
            message, code, _ = error_fields(obj) or ("unknown error", None, None)
            return self._fail(BackendError(
                502, f"Backend '{self.call.adapter.name}' error: {message}",
                code=code or "backend_error",
            ))

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
        elif not isinstance(exc, BackendError):
            log.error("stream %s failed", self.call.request_id, exc_info=exc)
            exc = BackendError(502, "Internal error while streaming.", code="internal_error")
        self.status, self.error = "error", exc.message
        return sse_event({"error": {"message": exc.message, "type": exc.type,
                                    "param": exc.param, "code": exc.code}})

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
        self.call.finish(status, error)
