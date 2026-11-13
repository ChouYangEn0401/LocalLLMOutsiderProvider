"""Tests for llmop.backends: adapters, model listing, routing, health, reloads."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from llmop.backends import BackendError, BackendRouter, ModelNotFound, OllamaAdapter, OpenAICompatAdapter
from llmop.backends import router as router_mod
from llmop.config import ConfigHolder, parse_config

OLLAMA = {"name": "ollama", "type": "ollama", "base_url": "http://ollama.test:11434"}
VLLM = {"name": "vllm", "type": "vllm", "base_url": "http://vllm.test:8000/v1/",
        "api_key": "upstream-secret"}


def make_router(fake_net, backends=None, aliases=None, **kw) -> BackendRouter:
    cfg = parse_config({"backends": backends if backends is not None else [OLLAMA, VLLM],
                        "model_aliases": aliases or {}})
    return BackendRouter(ConfigHolder(cfg), transport=fake_net.transport, **kw)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ listing
def test_ollama_lists_models_via_api_tags_with_context(fake_net):
    async def go():
        r = make_router(fake_net, [OLLAMA])
        models = await r.list_models()
        await r.aclose()
        return models

    models = run(go())
    assert [m.id for m in models] == ["qwen3.5:9b", "llama3:latest"]
    assert all(m.backend == "ollama" and m.owned_by == "ollama" for m in models)
    assert models[0].context_length == 8192  # Modelfile num_ctx from /api/show
    paths = [r["path"] for r in fake_net.ollama.requests]
    assert paths.count("/api/tags") == 1 and paths.count("/api/show") == 2


def test_ollama_context_from_model_info_without_num_ctx(fake_net):
    fake_net.ollama.context = 0
    models = run(make_router(fake_net, [OLLAMA]).list_models())
    assert models[0].context_length == 40960


def test_ollama_falls_back_to_v1_models(fake_net):
    fake_net.ollama.tags_status = 404
    models = run(make_router(fake_net, [OLLAMA]).list_models())
    assert [m.id for m in models] == ["qwen3.5:9b", "llama3:latest"]
    assert "/v1/models" in [r["path"] for r in fake_net.ollama.requests]


def test_vllm_listing_uses_bearer_root_url_and_max_model_len(fake_net):
    fake_net.vllm.require_key = "upstream-secret"
    models = run(make_router(fake_net, [VLLM]).list_models())
    assert [(m.id, m.context_length, m.created) for m in models] == [
        ("Qwen/Qwen2.5-7B-Instruct", 32768, 1_700_000_000)]
    req = fake_net.vllm.requests[0]
    assert req["path"] == "/v1/models"  # "/v1/" in base_url is not doubled
    assert req["headers"]["authorization"] == "Bearer upstream-secret"


def test_backend_model_patterns_filter_listing(fake_net):
    backend = {**OLLAMA, "models": ["qwen*"]}

    async def go():
        r = make_router(fake_net, [backend])
        return await r.list_models(), await r.health()

    models, health = run(go())
    assert [m.id for m in models] == ["qwen3.5:9b"]
    assert health[0]["model_count"] == 1 and health[0]["hidden_models"] == 1


def test_model_list_is_cached(fake_net):
    async def go(ttl):
        r = make_router(fake_net, [OLLAMA], cache_ttl=ttl)
        await r.list_models()
        await r.list_models()

    run(go(30))
    assert [r["path"] for r in fake_net.ollama.requests].count("/api/tags") == 1
    fake_net.ollama.requests.clear()
    run(go(0))
    assert [r["path"] for r in fake_net.ollama.requests].count("/api/tags") == 2


def test_unreachable_backend_is_skipped_in_listing(fake_net):
    fake_net.vllm.down = True
    models = run(make_router(fake_net).list_models())
    assert {m.backend for m in models} == {"ollama"}


# ------------------------------------------------------------------ routing
def test_resolve_alias_explicit_and_latest(fake_net):
    async def go():
        r = make_router(fake_net, aliases={"fast": "qwen3.5:9b", "big": "vllm/Qwen/Qwen2.5-7B-Instruct"})
        out = {}
        for name in ("fast", "big", "llama3", "llama3:latest", "Qwen/Qwen2.5-7B-Instruct",
                     "ollama/qwen3.5:9b", "vllm/Qwen/Qwen2.5-7B-Instruct"):
            adapter, upstream = await r.resolve(name)
            out[name] = (adapter.name, upstream)
        with pytest.raises(ModelNotFound):
            await r.resolve("nope:1b")
        with pytest.raises(ModelNotFound):
            await r.resolve("vllm/qwen3.5:9b")  # exists, but not on that backend
        return out

    out = run(go())
    assert out == {
        "fast": ("ollama", "qwen3.5:9b"),
        "big": ("vllm", "Qwen/Qwen2.5-7B-Instruct"),
        "llama3": ("ollama", "llama3:latest"),
        "llama3:latest": ("ollama", "llama3:latest"),
        "Qwen/Qwen2.5-7B-Instruct": ("vllm", "Qwen/Qwen2.5-7B-Instruct"),
        "ollama/qwen3.5:9b": ("ollama", "qwen3.5:9b"),
        "vllm/Qwen/Qwen2.5-7B-Instruct": ("vllm", "Qwen/Qwen2.5-7B-Instruct"),
    }


def test_resolve_first_backend_wins(fake_net):
    fake_net.vllm.models = ["qwen3.5:9b"]
    adapter, _ = run(make_router(fake_net).resolve("qwen3.5:9b"))
    assert adapter.name == "ollama"


def test_resolve_respects_backend_patterns(fake_net):
    with pytest.raises(ModelNotFound):
        run(make_router(fake_net, [{**OLLAMA, "models": ["qwen*"]}]).resolve("llama3"))


def test_resolve_passes_through_when_only_backend_unreachable(fake_net):
    fake_net.ollama.down = True
    adapter, upstream = run(make_router(fake_net, [OLLAMA]).resolve("anything:7b"))
    assert (adapter.name, upstream) == ("ollama", "anything:7b")


def test_resolve_with_several_backends_and_one_down(fake_net):
    fake_net.vllm.down = True

    async def go(backends):
        return await make_router(fake_net, backends).resolve("Qwen/Qwen2.5-7B-Instruct")

    with pytest.raises(ModelNotFound):
        run(go([OLLAMA, VLLM]))  # ambiguous: do not guess
    adapter, _ = run(go([OLLAMA, {**VLLM, "models": ["Qwen/*"]}]))
    assert adapter.name == "vllm"  # the down backend names it explicitly


def test_resolve_refreshes_model_list_on_miss(fake_net, monkeypatch):
    monkeypatch.setattr(router_mod, "MISS_REFRESH_AGE", 0.0)

    async def go():
        r = make_router(fake_net, [OLLAMA])
        await r.list_models()
        fake_net.ollama.models.append("gemma3:4b")  # "ollama pull" after caching
        return await r.resolve("gemma3:4b")

    adapter, upstream = run(go())
    assert upstream == "gemma3:4b"


def test_no_backends_configured(fake_net):
    with pytest.raises(ModelNotFound, match="No backends"):
        run(make_router(fake_net, []).resolve("x"))


# ------------------------------------------------------------------ health / reload
def test_health_reports_each_backend(fake_net):
    fake_net.vllm.down = True
    health = run(make_router(fake_net).health())
    by_name = {h["name"]: h for h in health}
    assert by_name["ollama"]["reachable"] is True
    assert by_name["ollama"]["model_count"] == 2
    assert by_name["ollama"]["models"] == ["qwen3.5:9b", "llama3:latest"]
    assert by_name["ollama"]["error"] is None
    assert by_name["vllm"]["reachable"] is False
    assert "not reachable" in by_name["vllm"]["error"] and "vLLM" in by_name["vllm"]["error"]
    assert by_name["vllm"]["base_url"] == "http://vllm.test:8000/v1/"
    assert {"type", "latency_ms", "active", "max_concurrency"} <= set(by_name["vllm"])


def test_config_reload_rebuilds_only_changed_adapters(fake_net):
    r = make_router(fake_net)
    first = {a.name: a for a in r.adapters()}
    assert isinstance(first["ollama"], OllamaAdapter)
    assert type(first["vllm"]) is OpenAICompatAdapter
    assert r.adapters()[0] is first["ollama"]  # unchanged config: same objects

    r.holder.set(parse_config({"backends": [OLLAMA, {**VLLM, "max_concurrency": 1}]}))
    second = {a.name: a for a in r.adapters()}
    assert second["ollama"] is first["ollama"]
    assert second["vllm"] is not first["vllm"]
    assert second["vllm"].semaphore._value == 1

    r.holder.set(parse_config({"backends": [OLLAMA]}))
    assert [a.name for a in r.adapters()] == ["ollama"]
    assert r.adapter("vllm") is None


def test_aclose_resets_client_and_adapters(fake_net):
    async def go():
        r = make_router(fake_net)
        a1 = r.adapter("ollama")
        await r.list_models()
        await r.aclose()
        a2 = r.adapter("ollama")
        models = await r.list_models()  # works again with a fresh client
        await r.aclose()
        return a1, a2, models

    a1, a2, models = run(go())
    assert a1 is not a2 and a1.client is not a2.client and models


# ------------------------------------------------------------------ adapter details
def test_prepare_body_merges_extra_body_and_num_ctx(fake_net):
    extra = {"keep_alive": "30m", "max_tokens": 5, "options": {"temperature": 0}}
    r = make_router(fake_net, [
        {**OLLAMA, "context_size": 16384, "extra_body": extra},
        {**VLLM, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    ])
    ollama, vllm = r.adapters()

    out = ollama.prepare_body("chat.completions", {
        "model": "m", "max_tokens": 10, "options": {"seed": 1}, "keep_alive": "1m"})
    assert out["keep_alive"] == "30m"  # operator wins
    assert out["max_tokens"] == 10  # protected key: client / quota value wins
    assert out["options"] == {"seed": 1, "temperature": 0, "num_ctx": 16384}

    out = ollama.prepare_body("chat.completions", {"model": "m", "max_completion_tokens": 7})
    assert out["max_tokens"] == 7  # mirrored: Ollama may ignore max_completion_tokens

    out = ollama.prepare_body("chat.completions", {"model": "m"})
    assert out["max_tokens"] == 5  # extra_body default when the request set no limit

    out = vllm.prepare_body("chat.completions", {
        "model": "m", "max_completion_tokens": 7, "chat_template_kwargs": {"x": 1}})
    assert "max_tokens" not in out
    assert out["chat_template_kwargs"] == {"x": 1, "enable_thinking": False}


def test_http_error_mapping(fake_net):
    r = make_router(fake_net)
    ollama, vllm = r.adapters()

    e = ollama.http_error(404, '{"error":{"message":"model \\"x\\" not found, try pulling it first"}}', model="x")
    assert (e.status, e.code, e.param) == (404, "model_not_found", "model")
    assert "ollama pull x" in e.message

    e = vllm.http_error(404, "404 page not found")
    assert (e.status, e.code) == (404, "not_found")

    e = vllm.http_error(401, '{"error":"Unauthorized"}')
    assert (e.status, e.code) == (502, "backend_auth_error")

    e = vllm.http_error(400, '{"object":"error","message":"bad temperature","type":"BadRequestError","param":"temperature","code":400}')
    assert (e.status, e.message, e.param, e.type) == (400, "bad temperature", "temperature",
                                                      "invalid_request_error")

    e = vllm.http_error(500, '{"detail":"CUDA error"}')
    assert (e.status, e.type) == (502, "server_error") and "CUDA error" in e.message

    assert vllm.http_error(503, "overloaded").status == 503


def test_transport_error_mapping(fake_net):
    ollama = make_router(fake_net).adapter("ollama")
    e = ollama.transport_error(httpx.ConnectError("refused"))
    assert (e.status, e.code) == (503, "backend_unavailable")
    assert e.message == "Backend 'ollama' is not reachable (is Ollama running?)."
    assert ollama.transport_error(httpx.ReadTimeout("slow")).status == 504
    assert ollama.transport_error(httpx.RemoteProtocolError("eof")).status == 502


def test_send_raises_backend_error_for_upstream_status(fake_net):
    async def go():
        adapter = make_router(fake_net).adapter("ollama")
        with pytest.raises(BackendError) as info:
            await adapter.send("chat.completions", {"model": "ghost", "messages": []}, model="ghost")
        return info.value

    err = run(go())
    assert err.status == 404 and err.code == "model_not_found"
