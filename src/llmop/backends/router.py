"""Model routing across the configured backends."""

from __future__ import annotations

import asyncio
import logging
import threading

import httpx

from ..config import Backend, ConfigHolder, model_allowed
from .base import BackendAdapter, BackendError, ModelInfo, ModelNotFound, OpenAICompatAdapter, find_model
from .ollama import OllamaAdapter

log = logging.getLogger("llmop.backends")

MISS_REFRESH_AGE = 2.0  # on a routing miss, re-list models older than this (new pulls)


def make_adapter(backend: Backend, client: httpx.AsyncClient) -> BackendAdapter:
    if backend.type == "ollama":
        return OllamaAdapter(backend, client)
    return OpenAICompatAdapter(backend, client)


def _explicitly_allowed(model: str, patterns: list[str]) -> bool:
    """Allowed by a pattern other than the catch-all ``*``."""
    return model_allowed(model, [p for p in patterns if p != "*"])


class BackendRouter:
    """Owns one adapter per configured backend and one shared HTTP client.

    Adapters follow config reloads: a backend whose settings changed gets a fresh
    adapter (new semaphore / model cache); unchanged backends keep theirs. Requests
    already in flight keep using the adapter they started with.
    """

    def __init__(
        self,
        holder: ConfigHolder,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        cache_ttl: float = 30.0,
    ):
        self.holder = holder
        self.cache_ttl = cache_ttl
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._adapters: dict[str, BackendAdapter] = {}
        self._snapshot: list[Backend] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ adapters
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=False: backends are local; a system HTTP(S)_PROXY must not
            # capture requests to 127.0.0.1.
            self._client = httpx.AsyncClient(
                transport=self._transport, trust_env=False, follow_redirects=False
            )
        return self._client

    def adapters(self) -> list[BackendAdapter]:
        """Current adapters in config order (rebuilt when the backend config changed)."""
        backends = self.holder.get().backends
        with self._lock:
            if backends != self._snapshot:
                fresh: dict[str, BackendAdapter] = {}
                for b in backends:
                    old = self._adapters.get(b.name)
                    fresh[b.name] = old if old is not None and old.config == b else make_adapter(b, self.client)
                self._adapters = fresh
                self._snapshot = list(backends)
            return list(self._adapters.values())

    def adapter(self, name: str) -> BackendAdapter | None:
        return next((a for a in self.adapters() if a.name == name), None)

    def _explicit(self, name: str) -> tuple[BackendAdapter, str] | None:
        """``"<backend>/<model>"`` when the prefix names a configured backend
        (model ids like ``Qwen/Qwen2.5-7B`` contain slashes too)."""
        if "/" not in name:
            return None
        prefix, rest = name.split("/", 1)
        adapter = self.adapter(prefix)
        return (adapter, rest) if adapter is not None and rest else None

    async def _fetch_all(
        self, adapters: list[BackendAdapter], max_age: float
    ) -> list[list[ModelInfo] | BackendError]:
        results = await asyncio.gather(
            *(a.get_models(max_age) for a in adapters), return_exceptions=True
        )
        out: list[list[ModelInfo] | BackendError] = []
        for adapter, res in zip(adapters, results):
            if isinstance(res, BackendError) or not isinstance(res, BaseException):
                out.append(res)
            elif isinstance(res, Exception):
                log.warning("backend %s: model listing failed: %r", adapter.name, res)
                out.append(BackendError(502, str(res), backend=adapter.name))
            else:  # CancelledError etc.
                raise res
        return out

    # ------------------------------------------------------------ public API
    async def list_models(self) -> list[ModelInfo]:
        """Models of all reachable backends, in config order (cached ``cache_ttl``
        seconds). Unreachable backends are skipped (and logged). The same id can
        appear for several backends; routing picks the first."""
        results = await self._fetch_all(self.adapters(), self.cache_ttl)
        return [m for res in results if not isinstance(res, BackendError) for m in res]

    def lookup(self, models: list[ModelInfo], name: str) -> ModelInfo | None:
        """Find ``name`` (plain or ``backend/model``) in a :meth:`list_models` result."""
        explicit = self._explicit(name)
        if explicit is not None:
            adapter, rest = explicit
            return find_model([m for m in models if m.backend == adapter.name], rest)
        return find_model(models, name)

    async def resolve(self, model: str) -> tuple[BackendAdapter, str]:
        """``(adapter, upstream_model_name)`` for a client-facing model name.

        Order: ``model_aliases`` -> explicit ``"<backend>/<model>"`` -> first backend
        whose (allowed) model list contains it, ignoring ``:latest``. If the model is
        not listed anywhere but a backend could not be asked, the request goes there
        when that is unambiguous (single backend, or the backend's ``models`` names it
        explicitly). Raises :class:`ModelNotFound`.
        """
        cfg = self.holder.get()
        adapters = self.adapters()
        if not adapters:
            raise ModelNotFound(model, "No backends are configured on this server.")
        target = cfg.model_aliases.get(model, model)

        explicit = self._explicit(target)
        if explicit is not None:
            adapter, name = explicit
            return adapter, await self._resolve_on(adapter, name, model)

        failed: list[BackendAdapter] = []
        for max_age in (self.cache_ttl, MISS_REFRESH_AGE):
            failed = []
            for adapter, res in zip(adapters, await self._fetch_all(adapters, max_age)):
                if isinstance(res, BackendError):
                    failed.append(adapter)
                    continue
                hit = find_model(res, target)
                if hit is not None:
                    return adapter, hit.id

        if failed:
            only = adapters[0]
            if len(adapters) == 1 and model_allowed(target, only.config.models):
                return only, target
            for adapter in failed:
                if _explicitly_allowed(target, adapter.config.models):
                    return adapter, target
        raise ModelNotFound(model)

    async def _resolve_on(self, adapter: BackendAdapter, name: str, requested: str) -> str:
        for max_age in (self.cache_ttl, MISS_REFRESH_AGE):
            try:
                models = await adapter.get_models(max_age)
            except BackendError:
                # Cannot ask the backend: pass through if its config allows the name.
                if model_allowed(name, adapter.config.models):
                    return name
                raise ModelNotFound(requested) from None
            hit = find_model(models, name)
            if hit is not None:
                return hit.id
        raise ModelNotFound(requested)

    async def health(self) -> list[dict]:
        """One dict per backend: name, type, base_url, reachable, model_count,
        models (first 50), hidden_models, error, latency_ms, active, max_concurrency."""
        return list(await asyncio.gather(*(a.health() for a in self.adapters())))

    async def aclose(self) -> None:
        """Close the HTTP client. Call it on the event loop that used the router."""
        client, self._client = self._client, None
        with self._lock:
            self._adapters, self._snapshot = {}, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # e.g. closed from another event loop
                log.debug("error closing backend HTTP client", exc_info=True)
