"""Backend adapters: one object per configured LLM server.

Every server is reached through its OpenAI-compatible ``/v1`` API. An adapter adds
the per-server details: how to list models, auth headers, request-body tweaks,
a concurrency limit, and how upstream failures are phrased for our API clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from ..config import Backend, model_allowed

log = logging.getLogger("llmop.backends")

ENDPOINT_PATHS: dict[str, str] = {
    "chat.completions": "/v1/chat/completions",
    "completions": "/v1/completions",
    "embeddings": "/v1/embeddings",
}

# Body keys that ``backend.extra_body`` may only fill in, never override: they carry
# routing (model), protocol (stream) or quota (max_tokens) decisions.
PROTECTED_KEYS = frozenset(
    {"model", "stream", "stream_options", "max_tokens", "max_completion_tokens"}
)
_TOKEN_LIMIT_KEYS = ("max_tokens", "max_completion_tokens")

LIST_TIMEOUT = 10.0  # seconds for model listing / health probes
FAILED_LIST_TTL = 5.0  # a failed model listing is retried after at most this long


@dataclass
class ModelInfo:
    id: str
    backend: str
    owned_by: str
    context_length: int | None = None
    created: int | None = None  # Unix seconds if the backend reports it


class ModelNotFound(Exception):
    def __init__(self, model: str, message: str | None = None):
        self.model = model
        self.message = message or (
            f"The model '{model}' does not exist or is not available. "
            "GET /v1/models lists the models you can use."
        )
        super().__init__(self.message)


class BackendError(Exception):
    """An upstream failure, already phrased for the API client (OpenAI error fields)."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str | None = "backend_error",
        type: str | None = None,
        param: str | None = None,
        backend: str = "",
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.type = type or ("invalid_request_error" if status < 500 else "server_error")
        self.param = param
        self.backend = backend


def norm_model(name: str) -> str:
    return name.removesuffix(":latest")


def find_model(models: list[ModelInfo], name: str) -> ModelInfo | None:
    """Exact id first, then ignoring Ollama's ``:latest`` tag on both sides."""
    for m in models:
        if m.id == name:
            return m
    n = norm_model(name)
    return next((m for m in models if norm_model(m.id) == n), None)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def error_fields(obj: Any) -> tuple[str, str | None, str | None] | None:
    """``(message, code, param)`` if ``obj`` looks like an error body in one of the
    usual shapes: OpenAI ``{"error": {...}}``, Ollama ``{"error": "..."}``,
    vLLM ``{"object": "error", "message": ...}`` or FastAPI ``{"detail": ...}``."""
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")[:500], _str(err.get("code")), _str(err.get("param"))
    if isinstance(err, str):
        return err[:500], None, None
    if obj.get("message"):
        return str(obj["message"])[:500], _str(obj.get("code")), _str(obj.get("param"))
    if obj.get("detail"):
        return str(obj["detail"])[:500], None, None
    return None


def parse_upstream_error(text: str) -> tuple[str, str | None, str | None]:
    """``(message, code, param)`` from an upstream error body (JSON or plain text)."""
    try:
        fields = error_fields(json.loads(text))
    except ValueError:
        fields = None
    return fields or (text.strip()[:500], None, None)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class BackendAdapter:
    """Shared machinery: HTTP calls, error mapping, model-list cache, concurrency slot.

    Subclasses implement :meth:`list_models`.
    """

    # Server name used in "is <label> running?" hints.
    LABELS: ClassVar[dict[str, str]] = {
        "ollama": "Ollama",
        "vllm": "vLLM",
        "lmstudio": "the LM Studio server",
        "llamacpp": "llama-server",
        "openai": "the backend server",
    }
    # Servers that may ignore ``max_completion_tokens``: we copy it into ``max_tokens``
    # so the quota cap is enforced.
    MIRROR_MAX_TOKENS: ClassVar[frozenset[str]] = frozenset({"ollama", "lmstudio", "llamacpp"})

    def __init__(self, config: Backend, client: httpx.AsyncClient):
        self.config = config
        self.client = client
        # Held for the whole upstream call, including streaming (protects the GPU).
        self.semaphore = asyncio.Semaphore(max(1, int(config.max_concurrency or 1)))
        self.active = 0
        self._models: list[ModelInfo] = []
        self._hidden = 0  # models the backend has but config.models does not expose
        self._models_at: float | None = None  # monotonic time of the last fetch
        self._models_error: BackendError | None = None
        self._models_lock = asyncio.Lock()

    # ------------------------------------------------------------ basics
    @property
    def name(self) -> str:
        return self.config.name

    @property
    def type(self) -> str:
        return self.config.type

    @property
    def label(self) -> str:
        return self.LABELS.get(self.config.type, "the backend server")

    def url(self, path: str) -> str:
        return self.config.root_url + path

    def headers(self) -> dict[str, str]:
        if self.config.api_key:
            return {"Authorization": f"Bearer {self.config.api_key}"}
        return {}

    @property
    def timeout(self) -> httpx.Timeout:
        c = self.config
        return httpx.Timeout(connect=c.connect_timeout, read=c.timeout, write=60.0, pool=c.timeout)

    @property
    def list_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(LIST_TIMEOUT, connect=self.config.connect_timeout)

    # ------------------------------------------------------------ concurrency
    async def acquire(self) -> None:
        await self.semaphore.acquire()
        self.active += 1

    def release(self) -> None:
        self.active -= 1
        self.semaphore.release()

    # ------------------------------------------------------------ errors
    def transport_error(self, exc: BaseException) -> BackendError:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return BackendError(
                503, f"Backend '{self.name}' is not reachable (is {self.label} running?).",
                code="backend_unavailable", backend=self.name,
            )
        if isinstance(exc, httpx.TimeoutException):
            return BackendError(
                504, f"Backend '{self.name}' timed out (no progress for {self.config.timeout:g} s).",
                code="backend_timeout", backend=self.name,
            )
        return BackendError(
            502, f"Lost connection to backend '{self.name}' ({type(exc).__name__}).",
            code="backend_error", backend=self.name,
        )

    def not_found_message(self, model: str, upstream: str) -> str:
        return f"The model '{model}' does not exist on backend '{self.name}' ({upstream})."

    def http_error(self, status: int, text: str, *, model: str = "") -> BackendError:
        """Translate an upstream HTTP error status + body."""
        message, code, param = parse_upstream_error(text)
        message = message or f"HTTP {status}"
        if status in (401, 403):
            # Not the client's fault: our own backends[].api_key is wrong / missing.
            return BackendError(
                502, f"Backend '{self.name}' rejected the gateway's credentials "
                "(check api_key in the backend config).",
                code="backend_auth_error", backend=self.name,
            )
        if status == 404:
            if "model" in message.lower():
                return BackendError(
                    404, self.not_found_message(model, message), code="model_not_found",
                    param="model", backend=self.name,
                )
            return BackendError(
                404, f"Backend '{self.name}' does not support this endpoint ({message}).",
                code="not_found", backend=self.name,
            )
        if status == 429:
            return BackendError(
                429, f"Backend '{self.name}' is busy: {message}", code="backend_busy",
                type="rate_limit_error", backend=self.name,
            )
        if status < 500:
            # The client's request was rejected (bad parameter etc.): pass it through.
            return BackendError(status, message, code=code, param=param, backend=self.name)
        if status in (503, 504):
            code = "backend_unavailable" if status == 503 else "backend_timeout"
            return BackendError(status, f"Backend '{self.name}': {message}", code=code,
                                backend=self.name)
        return BackendError(502, f"Backend '{self.name}' error: {message}", backend=self.name)

    # ------------------------------------------------------------ HTTP
    async def _get_json(self, path: str) -> Any:
        try:
            resp = await self.client.get(
                self.url(path), headers=self.headers(), timeout=self.list_timeout
            )
        except httpx.HTTPError as exc:
            raise self.transport_error(exc) from exc
        if resp.status_code >= 400:
            raise self.http_error(resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError:
            raise BackendError(
                502, f"Backend '{self.name}' returned invalid JSON for {path}.", backend=self.name
            ) from None

    def prepare_body(self, endpoint: str, body: dict) -> dict:
        """The upstream request body: ``body`` + the operator's ``extra_body``.

        ``extra_body`` wins (deep-merged), except for :data:`PROTECTED_KEYS`, which it
        only fills in when absent.
        """
        out = dict(body)
        has_limit = any(out.get(k) is not None for k in _TOKEN_LIMIT_KEYS)
        for key, value in (self.config.extra_body or {}).items():
            if key in PROTECTED_KEYS:
                # The two token-limit keys count as one: a default for either only
                # applies when the request set neither.
                if out.get(key) is None and not (key in _TOKEN_LIMIT_KEYS and has_limit):
                    out[key] = value
            elif isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = _deep_merge(out[key], value)
            else:
                out[key] = value
        if (
            self.type in self.MIRROR_MAX_TOKENS
            and endpoint == "chat.completions"
            and out.get("max_completion_tokens") is not None
            and out.get("max_tokens") is None
        ):
            out["max_tokens"] = out["max_completion_tokens"]
        return out

    async def send(self, endpoint: str, body: dict, *, model: str = "") -> httpx.Response:
        """POST ``body`` to the endpoint and return the *open* (streaming) response.

        The caller must ``aclose()`` it. Raises :class:`BackendError` for connection
        problems and for upstream status >= 400 (the body is read and translated).
        """
        request = self.client.build_request(
            "POST",
            self.url(ENDPOINT_PATHS[endpoint]),
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={**self.headers(), "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        try:
            resp = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise self.transport_error(exc) from exc
        if resp.status_code >= 400:
            try:
                raw = await resp.aread()
            except httpx.HTTPError:
                raw = b""
            finally:
                await resp.aclose()
            raise self.http_error(resp.status_code, raw.decode("utf-8", "replace"), model=model)
        return resp

    # ------------------------------------------------------------ models
    async def list_models(self) -> list[ModelInfo]:
        """All models the server reports (uncached, unfiltered). Raises BackendError."""
        raise NotImplementedError

    def _fresh(self, max_age: float) -> bool:
        if self._models_at is None:
            return False
        ttl = min(max_age, FAILED_LIST_TTL) if self._models_error else max_age
        return time.monotonic() - self._models_at < ttl

    def _cached(self) -> list[ModelInfo]:
        if self._models_error is not None:
            raise self._models_error.with_traceback(None)
        return list(self._models)

    async def _refresh(self) -> None:
        try:
            models = await self.list_models()
        except BackendError as exc:
            self._models_error = exc
            log.warning("backend %s: cannot list models: %s", self.name, exc.message)
        except Exception as exc:  # malformed response or similar
            log.warning("backend %s: cannot list models", self.name, exc_info=True)
            self._models_error = BackendError(
                502, f"Backend '{self.name}' returned an unexpected model list "
                f"({type(exc).__name__}).", backend=self.name,
            )
        else:
            exposed = [m for m in models if model_allowed(m.id, self.config.models)]
            self._models, self._hidden = exposed, len(models) - len(exposed)
            self._models_error = None
        self._models_at = time.monotonic()

    async def get_models(self, max_age: float = 30.0) -> list[ModelInfo]:
        """Models exposed by this backend (filtered by ``backend.models``), cached for
        ``max_age`` seconds. A failed listing is cached briefly and re-raised."""
        if not self._fresh(max_age):
            async with self._models_lock:
                if not self._fresh(max_age):
                    await self._refresh()
        return self._cached()

    async def health(self) -> dict:
        """Fresh probe for the admin page / ``llmop check``."""
        started = time.monotonic()
        async with self._models_lock:
            await self._refresh()
        ok = self._models_error is None
        return {
            "name": self.name,
            "type": self.type,
            "base_url": self.config.base_url,
            "reachable": ok,
            "model_count": len(self._models) if ok else 0,
            "models": [m.id for m in self._models[:50]] if ok else [],
            "hidden_models": self._hidden if ok else 0,
            "error": None if ok else self._models_error.message,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "active": self.active,
            "max_concurrency": max(1, int(self.config.max_concurrency or 1)),
        }


class OpenAICompatAdapter(BackendAdapter):
    """vLLM, LM Studio, llama.cpp server or any other OpenAI-compatible server."""

    async def list_models(self) -> list[ModelInfo]:
        data = await self._get_json("/v1/models")
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise BackendError(
                502, f"Backend '{self.name}' returned an unexpected /v1/models response.",
                backend=self.name,
            )
        out: list[ModelInfo] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            out.append(ModelInfo(
                id=item["id"],
                backend=self.name,
                owned_by=self.name,
                # vLLM reports max_model_len; some servers use context_length.
                context_length=_int(item.get("max_model_len")) or _int(item.get("context_length")),
                created=_int(item.get("created")),
            ))
        return out
