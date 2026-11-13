"""Ollama adapter: OpenAI-compatible ``/v1`` for requests, native API for extras.

* Models are listed with ``GET /api/tags`` (falls back to ``/v1/models``).
* ``context_length`` comes from ``POST /api/show`` (best effort, cached per digest):
  a Modelfile ``num_ctx`` parameter if set, otherwise the model's trained maximum.
* ``backend.context_size`` is sent as ``options.num_ctx``. NOTE: Ollama's ``/v1``
  endpoints currently ignore ``options`` (it is harmless and kept for forward
  compatibility). To really change the context window, operators should start
  Ollama with ``OLLAMA_CONTEXT_LENGTH=<n>`` or set ``PARAMETER num_ctx <n>`` in a
  Modelfile; Ollama's small default context silently truncates long prompts.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from .base import BackendError, ModelInfo, OpenAICompatAdapter

_NUM_CTX = re.compile(r"^\s*num_ctx\s+(\d+)", re.MULTILINE)


def context_from_show(show: object) -> int | None:
    """Context window from an ``/api/show`` response, or None."""
    if not isinstance(show, dict):
        return None
    params = show.get("parameters")
    if isinstance(params, str):
        m = _NUM_CTX.search(params)
        if m:
            return int(m.group(1))
    info = show.get("model_info")
    if isinstance(info, dict):
        for key, value in info.items():
            if key.endswith(".context_length") and isinstance(value, int) and value > 0:
                return value
    return None


class OllamaAdapter(OpenAICompatAdapter):
    SHOW_CONCURRENCY = 4

    def __init__(self, config, client):
        super().__init__(config, client)
        self._ctx_cache: dict[tuple[str, str], int | None] = {}

    def not_found_message(self, model: str, upstream: str) -> str:
        return (
            f"The model '{model}' is not installed on backend '{self.name}' (Ollama: {upstream}). "
            f"The operator can install it with: ollama pull {model}"
        )

    def prepare_body(self, endpoint: str, body: dict) -> dict:
        out = super().prepare_body(endpoint, body)
        if self.config.context_size > 0:
            opts = out.get("options")
            opts = dict(opts) if isinstance(opts, dict) else {}
            opts["num_ctx"] = int(self.config.context_size)  # see module docstring
            out["options"] = opts
        return out

    async def list_models(self) -> list[ModelInfo]:
        try:
            data = await self._get_json("/api/tags")
        except BackendError as exc:
            if exc.status in (503, 504):  # server down: /v1/models would fail too
                raise
            return await super().list_models()
        entries = data.get("models") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return await super().list_models()

        found: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name:
                found.append((name, str(entry.get("digest") or "")))

        limit = asyncio.Semaphore(self.SHOW_CONCURRENCY)

        async def ctx(name: str, digest: str) -> int | None:
            async with limit:
                return await self._context_length(name, digest)

        contexts = await asyncio.gather(*(ctx(n, d) for n, d in found))
        return [
            ModelInfo(id=name, backend=self.name, owned_by=self.name, context_length=c)
            for (name, _), c in zip(found, contexts)
        ]

    async def _context_length(self, name: str, digest: str) -> int | None:
        key = (name, digest)
        if key in self._ctx_cache:
            return self._ctx_cache[key]
        try:
            resp = await self.client.post(
                self.url("/api/show"), json={"model": name}, headers=self.headers(),
                timeout=self.list_timeout,
            )
            if resp.status_code != 200:
                return None
            value = context_from_show(resp.json())
        except (httpx.HTTPError, ValueError):
            return None  # best effort; retried on the next listing
        self._ctx_cache[key] = value
        return value
