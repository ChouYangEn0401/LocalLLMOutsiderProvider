"""Shared test helpers: a fake Ollama / vLLM server for ``httpx.MockTransport``.

Usage::

    def test_x(fake_net):
        state = build_state(cfg, store=Store(":memory:"), transport=fake_net.transport)
        fake_net.ollama.reply = "Hi"          # tweak behaviour
        ... fake_net.ollama.requests[-1]      # inspect what the gateway sent

Hosts: ``ollama.test`` (kind "ollama": /api/tags, /api/show and /v1/*) and
``vllm.test`` (kind "vllm": /v1/* only). Any other host is "connection refused".
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from llmop.store import Store

CREATED = 1_700_000_000


class FakeLLM:
    """A fake OpenAI-compatible LLM server. Change attributes to alter behaviour."""

    def __init__(
        self,
        kind: str = "ollama",
        models: list[str] | tuple[str, ...] = ("qwen3.5:9b", "llama3:latest"),
        *,
        reply: str = "Hello world!",
    ):
        self.kind = kind  # "ollama" | "vllm"
        self.models = list(models)
        self.reply = reply
        self.usage = True  # report usage (non-stream always, stream when include_usage)
        self.prompt_tokens = 11
        self.down = False  # raise ConnectError ("connection refused")
        self.missing: set[str] = set()  # listed models that 404 when used
        self.fail: tuple[int, Any] | None = None  # (status, body) for generation endpoints
        self.tags_status = 200  # Ollama /api/tags status (404 -> adapter falls back to /v1/models)
        self.context = 8192  # Ollama Modelfile num_ctx reported by /api/show (0 = none)
        self.require_key: str | None = None  # expected "Authorization: Bearer <key>"
        self.first_delay = 0.0  # seconds before the first byte of a generation
        self.delay = 0.0  # seconds between stream chunks
        self.mid_stream_error = False  # send an error event after the first chunk
        self.drop_mid_stream = False  # break the connection after the first chunk
        self.timeout = False  # raise ReadTimeout instead of answering
        self.requests: list[dict] = []
        self.active = 0
        self.max_active = 0

    # ------------------------------------------------------------ helpers
    @property
    def last(self) -> dict:
        """JSON body of the last generation request."""
        gen = [r for r in self.requests if r["path"].startswith("/v1/") and r["method"] == "POST"]
        return gen[-1]["json"]

    def pieces(self) -> list[str]:
        return re.findall(r"\s*\S+", self.reply) or [""]

    def _usage(self) -> dict:
        n = len(self.pieces())
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": n,
                "total_tokens": self.prompt_tokens + n}

    def _not_found(self, model: str) -> httpx.Response:
        if self.kind == "ollama":
            body = {"error": {"message": f'model "{model}" not found, try pulling it first',
                              "type": "api_error", "param": None, "code": None}}
        else:
            body = {"object": "error", "message": f"The model `{model}` does not exist.",
                    "type": "NotFoundError", "param": None, "code": 404}
        return httpx.Response(404, json=body)

    def _known(self, model: str) -> bool:
        norm = model.removesuffix(":latest")
        return any(m.removesuffix(":latest") == norm for m in self.models) and model not in self.missing

    # ------------------------------------------------------------ handler
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Connection refused", request=request)
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append({"method": request.method, "path": path, "json": body,
                              "headers": dict(request.headers)})
        if self.require_key and request.headers.get("authorization") != f"Bearer {self.require_key}":
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        if self.kind == "ollama" and path == "/api/tags":
            if self.tags_status != 200:
                return httpx.Response(self.tags_status, text="404 page not found")
            return httpx.Response(200, json={"models": [
                {"name": m, "model": m, "digest": f"sha-{m}"} for m in self.models]})
        if self.kind == "ollama" and path == "/api/show":
            return httpx.Response(200, json={
                "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
                "parameters": f"num_ctx                        {self.context}" if self.context else "",
            })
        if path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": m, "object": "model", "created": CREATED, "owned_by": self.kind,
                 **({"max_model_len": 32768} if self.kind == "vllm" else {})}
                for m in self.models]})
        if request.method == "POST" and path in ("/v1/chat/completions", "/v1/completions",
                                                  "/v1/embeddings"):
            return await self._generate(path, body or {})
        return httpx.Response(404, text="404 page not found")

    async def _generate(self, path: str, body: dict) -> httpx.Response:
        model = body.get("model", "")
        if self.fail is not None:
            status, payload = self.fail
            return httpx.Response(status, **({"json": payload} if not isinstance(payload, str)
                                             else {"text": payload}))
        if not self._known(model):
            return self._not_found(model)
        if self.timeout:
            raise httpx.ReadTimeout("timed out")
        if path == "/v1/embeddings":
            inputs = body.get("input")
            inputs = inputs if isinstance(inputs, list) else [inputs]
            data = {"object": "list", "model": model, "data": [
                {"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                for i in range(len(inputs))]}
            if self.usage:
                data["usage"] = {"prompt_tokens": 5, "total_tokens": 5}
            return httpx.Response(200, json=data)

        chat = path == "/v1/chat/completions"
        if body.get("stream"):
            include = bool((body.get("stream_options") or {}).get("include_usage"))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=self._stream(chat, model, include))
        self._enter()
        try:
            if self.first_delay:
                await asyncio.sleep(self.first_delay)
        finally:
            self.active -= 1
        if chat:
            choice = {"index": 0, "message": {"role": "assistant", "content": self.reply},
                      "finish_reason": "stop"}
            data = {"id": "chatcmpl-1", "object": "chat.completion"}
        else:
            choice = {"index": 0, "text": self.reply, "finish_reason": "stop"}
            data = {"id": "cmpl-1", "object": "text_completion"}
        data.update(created=CREATED, model=model, choices=[choice])
        if self.usage:
            data["usage"] = self._usage()
        return httpx.Response(200, json=data)

    def _enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    async def _stream(self, chat: bool, model: str, include_usage: bool):
        self._enter()
        try:
            if self.first_delay:
                await asyncio.sleep(self.first_delay)
            obj = "chat.completion.chunk" if chat else "text_completion"
            base = {"id": "chatcmpl-1" if chat else "cmpl-1", "object": obj,
                    "created": CREATED, "model": model}

            def ev(payload: dict) -> bytes:
                return b"data: " + json.dumps(payload).encode() + b"\n\n"

            for i, piece in enumerate(self.pieces()):
                if i == 1 and self.drop_mid_stream:
                    raise httpx.ReadError("connection reset by peer")
                if i == 1 and self.mid_stream_error:
                    yield b'data: {"error":{"message":"out of memory"}}\n\n'
                    return
                if i and self.delay:
                    await asyncio.sleep(self.delay)
                if chat:
                    delta = {"content": piece, **({"role": "assistant"} if i == 0 else {})}
                    choice = {"index": 0, "delta": delta, "finish_reason": None}
                else:
                    choice = {"index": 0, "text": piece, "finish_reason": None}
                yield ev({**base, "choices": [choice]})
            last = {"index": 0, "finish_reason": "stop", **({"delta": {}} if chat else {"text": ""})}
            yield ev({**base, "choices": [last]})
            if include_usage and self.usage:
                yield ev({**base, "choices": [], "usage": self._usage()})
            yield b"data: [DONE]\n\n"
        finally:
            self.active -= 1


class FakeNetwork:
    """Routes requests to fake servers by host name; unknown hosts refuse connections."""

    def __init__(self, servers: dict[str, FakeLLM]):
        self.servers = servers

    async def handler(self, request: httpx.Request) -> httpx.Response:
        server = self.servers.get(request.url.host)
        if server is None:
            raise httpx.ConnectError("Connection refused", request=request)
        return await server(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def ollama(self) -> FakeLLM:
        return self.servers["ollama.test"]

    @property
    def vllm(self) -> FakeLLM:
        return self.servers["vllm.test"]


@pytest.fixture
def fake_net() -> FakeNetwork:
    """``ollama.test`` (models qwen3.5:9b, llama3:latest) and ``vllm.test``
    (model Qwen/Qwen2.5-7B-Instruct)."""
    return FakeNetwork({
        "ollama.test": FakeLLM("ollama"),
        "vllm.test": FakeLLM("vllm", ["Qwen/Qwen2.5-7B-Instruct"]),
    })


@pytest.fixture
def memory_store() -> Iterator[Store]:
    store = Store(":memory:")
    yield store
    store.close()
