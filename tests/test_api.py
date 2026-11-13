"""Tests for the /v1 API layer and llmop.api.proxy.forward (fake backends via MockTransport)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from llmop import security
from llmop.api import build_api_router, forward
from llmop.app import build_state
from llmop.config import parse_config
from llmop.store import Store, UsageEvent

FAST_HASH = security.hash_password("pw", n=2**4)
OLLAMA = {"name": "ollama", "type": "ollama", "base_url": "http://ollama.test:11434"}
VLLM = {"name": "vllm", "type": "vllm", "base_url": "http://vllm.test:8000/v1",
        "api_key": "upstream-secret"}
ACCOUNTS = {
    "alice": {"password_hash": FAST_HASH},
    "bob": {"password_hash": FAST_HASH, "models": ["qwen3.5:9b"]},
    "carol": {"password_hash": FAST_HASH, "disabled": True},
    "root": {"password_hash": FAST_HASH, "role": "admin"},
}
ALIASES = {"fast": "qwen3.5:9b", "big": "vllm/Qwen/Qwen2.5-7B-Instruct"}
CHAT = {"model": "qwen3.5:9b", "messages": [{"role": "user", "content": "Say hello please"}]}


class Api:
    def __init__(self, state, client: TestClient, keys: dict[str, str], net):
        self.state, self.client, self.keys, self.net = state, client, keys, net

    def headers(self, user: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[user]}"} if user else {}

    def post(self, path: str, body=None, user: str | None = "alice", **kw):
        return self.client.post(path, json=body, headers=self.headers(user), **kw)

    def get(self, path: str, user: str | None = "alice", **kw):
        return self.client.get(path, headers=self.headers(user), **kw)

    def chat(self, user: str = "alice", **fields):
        return self.post("/v1/chat/completions", {**CHAT, **fields}, user=user)

    def usage(self) -> list[dict]:
        return self.state.store.recent_usage(None)


def build_app(state) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        yield
        await state.router.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(build_api_router(state))
    return app


def make_state(net, *, backends=None, accounts=None, quota=None, aliases=None, server=None):
    cfg = parse_config({
        "server": server or {},
        "backends": backends if backends is not None else [OLLAMA, VLLM],
        "accounts": accounts or ACCOUNTS,
        "default_quota": quota or {},
        "model_aliases": ALIASES if aliases is None else aliases,
    })
    return build_state(cfg, store=Store(":memory:"), transport=net.transport)


@pytest.fixture
def new_state(fake_net):
    """Factory for AppState objects wired to ``fake_net``; stores closed at teardown."""
    made = []

    def factory(**kw):
        made.append(make_state(fake_net, **kw))
        return made[-1]

    yield factory
    for state in made:
        state.store.close()


@pytest.fixture
def make_api(new_state, fake_net):
    opened: list[TestClient] = []

    def factory(**kw) -> Api:
        state = new_state(**kw)
        client = TestClient(build_app(state))
        client.__enter__()
        opened.append(client)
        keys = {u: state.store.create_api_key(u, "test")[0] for u in state.config.accounts}
        return Api(state, client, keys, fake_net)

    yield factory
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def api(make_api) -> Api:
    return make_api()


def sse_events(text: str) -> list:
    """Data payloads of an SSE body ("[DONE]" kept as a string)."""
    out = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                data = line[6:]
                out.append(data if data == "[DONE]" else json.loads(data))
    return out


def assert_openai_error(resp, status: int, code: str | None = None):
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert set(err) == {"message", "type", "param", "code"}
    assert err["message"]
    if code is not None:
        assert err["code"] == code
    return err


# ------------------------------------------------------------------ models
def test_models_list_filtered_by_account(api):
    resp = api.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == ["qwen3.5:9b", "llama3:latest", "Qwen/Qwen2.5-7B-Instruct", "fast", "big"]
    first = body["data"][0]
    assert first["object"] == "model" and isinstance(first["created"], int)
    assert first["owned_by"] == "ollama" and first["context_length"] == 8192

    bob = [m["id"] for m in api.get("/v1/models", user="bob").json()["data"]]
    assert bob == ["qwen3.5:9b", "fast"]  # "big" points at a model bob may not use


def test_models_list_skips_unreachable_backend(api, fake_net):
    fake_net.vllm.down = True
    ids = [m["id"] for m in api.get("/v1/models").json()["data"]]
    assert ids == ["qwen3.5:9b", "llama3:latest", "fast"]


def test_retrieve_model(api):
    assert api.get("/v1/models/qwen3.5:9b").json()["id"] == "qwen3.5:9b"
    assert api.get("/v1/models/Qwen/Qwen2.5-7B-Instruct").json()["owned_by"] == "vllm"
    assert api.get("/v1/models/fast").json()["id"] == "fast"
    assert api.get("/v1/models/ollama/llama3").json()["id"] == "ollama/llama3"
    assert_openai_error(api.get("/v1/models/llama3:latest", user="bob"), 404, "model_not_found")
    assert_openai_error(api.get("/v1/models/nope"), 404, "model_not_found")


# ------------------------------------------------------------------ auth
def test_auth_errors_are_openai_shaped(api):
    err = assert_openai_error(api.get("/v1/models", user=None), 401, "invalid_api_key")
    assert "Authorization: Bearer" in err["message"]
    assert_openai_error(api.client.get("/v1/models", headers={"Authorization": "Bearer sk-llop-bogus"}),
                        401, "invalid_api_key")
    key_id = api.state.store.list_api_keys("alice")[0].id
    api.state.store.revoke_api_key(key_id)
    assert_openai_error(api.chat(), 401, "invalid_api_key")
    assert_openai_error(api.get("/v1/models", user="carol"), 403, "account_disabled")
    assert api.usage() == []


def test_x_api_key_header_accepted(api):
    resp = api.client.post("/v1/chat/completions", json=CHAT, headers={"x-api-key": api.keys["alice"]})
    assert resp.status_code == 200


# ------------------------------------------------------------------ chat (non-stream)
def test_chat_non_stream_records_backend_usage(api, fake_net):
    resp = api.chat(model="fast")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "fast"  # the name the client sent
    assert body["choices"][0]["message"]["content"] == "Hello world!"
    assert resp.headers["x-request-id"].startswith("req_")
    assert fake_net.ollama.last["model"] == "qwen3.5:9b"
    assert fake_net.ollama.last["messages"] == CHAT["messages"]
    assert "max_tokens" not in fake_net.ollama.last  # no cap configured

    [row] = api.usage()
    key_id = api.state.store.list_api_keys("alice")[0].id
    assert row["username"] == "alice" and row["key_id"] == key_id
    assert (row["endpoint"], row["model"], row["backend"]) == ("chat.completions", "fast", "ollama")
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (11, 2, 13)
    assert (row["usage_source"], row["status"], row["stream"]) == ("backend", "ok", 0)
    assert row["client_ip"] == "testclient" and row["duration_ms"] >= 0


def test_explicit_backend_model_and_upstream_api_key(api, fake_net):
    fake_net.vllm.require_key = "upstream-secret"
    resp = api.chat(model="vllm/Qwen/Qwen2.5-7B-Instruct")
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "vllm/Qwen/Qwen2.5-7B-Instruct"
    assert fake_net.vllm.last["model"] == "Qwen/Qwen2.5-7B-Instruct"
    assert api.usage()[0]["backend"] == "vllm"


def test_non_stream_estimates_usage_when_backend_reports_none(api, fake_net):
    fake_net.ollama.usage = False
    assert api.chat().status_code == 200
    [row] = api.usage()
    assert row["usage_source"] == "estimate"
    assert row["completion_tokens"] == 3  # len("Hello world!") / 4
    assert row["prompt_tokens"] == (16 + len("Say hello please") + 3) // 4


def test_ratelimit_headers_on_success(make_api):
    api = make_api(quota={"requests_per_minute": 10, "tokens_per_day": 1000})
    resp = api.chat()
    assert resp.headers["x-ratelimit-limit-requests"] == "10"
    assert resp.headers["x-ratelimit-remaining-requests"] == "9"
    assert resp.headers["x-ratelimit-limit-tokens"] == "1000"
    assert resp.headers["x-ratelimit-remaining-tokens"] == str(1000 - 13)


# ------------------------------------------------------------------ quota
def test_max_tokens_clamped_to_quota(make_api, fake_net):
    api = make_api(quota={"max_tokens_per_request": 100})
    sent = []
    for fields in ({"max_tokens": 5000}, {}, {"max_tokens": 50}, {"max_completion_tokens": 500}):
        assert api.chat(**fields).status_code == 200
        sent.append({k: fake_net.ollama.last.get(k) for k in ("max_tokens", "max_completion_tokens")})
    assert sent == [
        {"max_tokens": 100, "max_completion_tokens": None},
        {"max_tokens": 100, "max_completion_tokens": None},
        {"max_tokens": 50, "max_completion_tokens": None},
        {"max_tokens": 100, "max_completion_tokens": 100},  # mirrored for Ollama
    ]


def test_max_tokens_clamped_to_remaining_daily_tokens(make_api, fake_net):
    api = make_api(quota={"tokens_per_day": 1000})
    api.state.store.record_usage(UsageEvent("alice", "chat.completions", "m", "ollama",
                                            prompt_tokens=900, completion_tokens=60))
    assert api.chat(max_tokens=500).status_code == 200
    assert fake_net.ollama.last["max_tokens"] == 40


def test_invalid_max_tokens_rejected(api):
    assert_openai_error(api.chat(max_tokens="9999"), 400)
    assert_openai_error(api.chat(max_tokens=1.5), 400)
    assert api.usage() == []


def test_quota_exhausted_returns_429_insufficient_quota(make_api):
    api = make_api(quota={"tokens_per_day": 100})
    api.state.store.record_usage(UsageEvent("alice", "chat.completions", "m", "ollama",
                                            prompt_tokens=100, completion_tokens=50))
    resp = api.chat()
    err = assert_openai_error(resp, 429, "insufficient_quota")
    assert err["type"] == "insufficient_quota"
    assert int(resp.headers["Retry-After"]) > 0
    assert "x-request-id" in resp.headers
    assert len(api.usage()) == 1  # the denied request is not recorded


def test_rpm_limit_returns_429_rate_limit_exceeded(make_api):
    api = make_api(quota={"requests_per_minute": 2})
    assert api.chat().status_code == 200
    assert api.chat().status_code == 200
    resp = api.chat()
    assert_openai_error(resp, 429, "rate_limit_exceeded")
    assert 1 <= int(resp.headers["Retry-After"]) <= 61
    assert resp.headers["x-ratelimit-remaining-requests"] == "0"


def test_concurrency_lease_released_after_requests(api):
    for _ in range(3):  # max_concurrent defaults to 2
        assert api.chat().status_code == 200
        assert api.chat(stream=True).status_code == 200
    assert api.state.quota.usage(api.state.config.accounts["alice"])["in_flight"] == 0


# ------------------------------------------------------------------ permissions / routing errors
def test_model_not_allowed_returns_403(api):
    assert_openai_error(api.chat(user="bob", model="llama3"), 403, "model_not_allowed")
    assert_openai_error(api.chat(user="bob", model="big"), 403, "model_not_allowed")
    assert api.chat(user="bob", model="fast").status_code == 200
    assert api.chat(user="bob", model="ollama/qwen3.5:9b").status_code == 200


def test_permissions_follow_config_reload(api):
    assert api.chat(user="bob", model="llama3").status_code == 403
    cfg = api.state.config
    cfg.accounts["bob"].models = ["*"]
    api.state.holder.set(cfg)
    assert api.chat(user="bob", model="llama3").status_code == 200


def test_unknown_model_returns_404(api):
    err = assert_openai_error(api.chat(model="nope:1b"), 404, "model_not_found")
    assert err["param"] == "model" and "/v1/models" in err["message"]


def test_backend_down_returns_503(make_api, fake_net):
    api = make_api(backends=[OLLAMA], aliases={})
    fake_net.ollama.down = True
    err = assert_openai_error(api.chat(), 503, "backend_unavailable")
    assert "is Ollama running?" in err["message"]
    assert_openai_error(api.chat(stream=True), 503, "backend_unavailable")
    rows = api.usage()
    assert [r["status"] for r in rows] == ["error", "error"]
    assert all(r["total_tokens"] == 0 and r["usage_source"] == "none" for r in rows)


def test_upstream_model_404_is_helpful(api, fake_net):
    fake_net.ollama.missing = {"llama3:latest"}
    err = assert_openai_error(api.chat(model="llama3"), 404, "model_not_found")
    assert "ollama pull llama3:latest" in err["message"]


def test_upstream_400_passed_through(api, fake_net):
    fake_net.ollama.fail = (400, {"error": {"message": "temperature too high", "type": "x",
                                            "param": "temperature", "code": "invalid_value"}})
    err = assert_openai_error(api.chat(temperature=9), 400, "invalid_value")
    assert err["param"] == "temperature" and err["message"] == "temperature too high"


def test_upstream_500_becomes_502(api, fake_net):
    fake_net.ollama.fail = (500, "llama runner process has terminated")
    err = assert_openai_error(api.chat(), 502, "backend_error")
    assert "llama runner" in err["message"]


def test_backend_timeout_returns_504(api, fake_net):
    fake_net.ollama.timeout = True
    assert_openai_error(api.chat(), 504, "backend_timeout")


def test_bad_request_bodies(api):
    url = "/v1/chat/completions"
    hdrs = {**api.headers("alice"), "Content-Type": "application/json"}
    assert_openai_error(api.client.post(url, content=b"{not json", headers=hdrs), 400, "invalid_json")
    assert_openai_error(api.client.post(url, content=b"", headers=hdrs), 400, "invalid_json")
    assert_openai_error(api.post(url, [1, 2]), 400, "invalid_json")
    assert assert_openai_error(api.post(url, {"messages": []}), 400)["param"] == "model"
    assert assert_openai_error(api.post(url, {"model": "fast"}), 400)["param"] == "messages"
    assert_openai_error(api.chat(stream="yes"), 400)


def test_unknown_v1_endpoint_is_openai_shaped(api):
    err = assert_openai_error(api.post("/v1/messages", {}), 404, "unknown_url")
    assert "/v1/chat/completions" in err["message"]


# ------------------------------------------------------------------ streaming
def test_stream_relays_chunks_and_strips_usage_chunk(api, fake_net):
    resp = api.chat(model="fast", stream=True)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.headers["x-request-id"].startswith("req_")
    events = sse_events(resp.text)
    assert events[-1] == "[DONE]" and resp.text.endswith("data: [DONE]\n\n")
    chunks = events[:-1]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello world!"
    assert all(c["choices"] for c in chunks)  # usage-only chunk removed
    assert all("usage" not in c for c in chunks)
    assert {c["model"] for c in chunks} == {"fast"}
    assert fake_net.ollama.last["stream_options"] == {"include_usage": True}

    [row] = api.usage()
    assert (row["prompt_tokens"], row["completion_tokens"]) == (11, 2)
    assert (row["usage_source"], row["status"], row["stream"]) == ("backend", "ok", 1)


def test_stream_keeps_usage_chunk_when_requested(api):
    resp = api.chat(stream=True, stream_options={"include_usage": True})
    events = sse_events(resp.text)
    assert events[-1] == "[DONE]"
    assert events[-2]["choices"] == []
    assert events[-2]["usage"] == {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}


def test_stream_estimates_usage_when_backend_sends_none(api, fake_net):
    fake_net.ollama.usage = False
    resp = api.chat(stream=True, stream_options={"include_usage": True})
    events = sse_events(resp.text)
    synthesized = events[-2]
    assert synthesized["choices"] == [] and synthesized["model"] == "qwen3.5:9b"
    prompt = (16 + len("Say hello please") + 3) // 4
    assert synthesized["usage"] == {"prompt_tokens": prompt, "completion_tokens": 3,
                                    "total_tokens": prompt + 3}
    [row] = api.usage()
    assert (row["usage_source"], row["prompt_tokens"], row["completion_tokens"]) == ("estimate", prompt, 3)


def test_stream_completions_endpoint(api, fake_net):
    resp = api.post("/v1/completions", {"model": "llama3", "prompt": "Once upon", "stream": True})
    events = sse_events(resp.text)
    assert "".join(e["choices"][0]["text"] for e in events[:-1]) == "Hello world!"
    assert events[-1] == "[DONE]"
    [row] = api.usage()
    assert (row["endpoint"], row["model"], row["completion_tokens"]) == ("completions", "llama3", 2)


def test_stream_sends_keepalive_while_waiting(make_api, fake_net):
    api = make_api(server={"sse_keepalive": 0.05})
    fake_net.ollama.first_delay = 0.3
    resp = api.chat(stream=True)
    assert resp.status_code == 200
    assert resp.text.startswith(": keepalive\n\n")
    assert sse_events(resp.text)[-1] == "[DONE]"
    assert api.usage()[0]["status"] == "ok"


def test_stream_slow_backend_failure_becomes_error_event(make_api, fake_net):
    api = make_api(server={"sse_keepalive": 0.05})
    fake_net.ollama.first_delay = 0.3
    fake_net.ollama.drop_mid_stream = True
    resp = api.chat(stream=True)
    assert resp.status_code == 200  # already committed to the stream
    events = sse_events(resp.text)
    assert events[0]["choices"][0]["delta"]["content"] == "Hello"
    assert events[-1]["error"]["code"] == "backend_error"
    assert "[DONE]" not in events
    [row] = api.usage()
    assert row["status"] == "error" and row["usage_source"] == "estimate"


def test_stream_upstream_error_event(api, fake_net):
    fake_net.ollama.mid_stream_error = True
    resp = api.chat(stream=True)
    events = sse_events(resp.text)
    assert events[0]["choices"][0]["delta"]["content"] == "Hello"
    assert events[-1]["error"]["message"] == "Backend 'ollama' error: out of memory"
    assert "[DONE]" not in events
    [row] = api.usage()
    assert row["status"] == "error" and "out of memory" in row["error"]


def test_stream_upstream_404_gets_http_status(api, fake_net):
    fake_net.ollama.missing = {"qwen3.5:9b"}
    assert_openai_error(api.chat(stream=True), 404, "model_not_found")


# ------------------------------------------------------------------ embeddings / me
def test_embeddings(api, fake_net):
    resp = api.post("/v1/embeddings", {"model": "qwen3.5:9b", "input": ["a", "b"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 2 and body["model"] == "qwen3.5:9b"
    fake_net.ollama.usage = False
    assert api.post("/v1/embeddings", {"model": "qwen3.5:9b", "input": "x" * 40}).status_code == 200
    first, second = reversed(api.usage())
    assert (first["endpoint"], first["prompt_tokens"], first["usage_source"]) == ("embeddings", 5, "backend")
    assert (second["prompt_tokens"], second["completion_tokens"], second["usage_source"]) == (10, 0, "estimate")
    assert "max_tokens" not in fake_net.ollama.last


def test_me(make_api):
    api = make_api(quota={"tokens_per_day": 500})
    api.chat()
    resp = api.get("/v1/me")
    assert resp.status_code == 200
    me = resp.json()
    assert me["username"] == "alice" and me["role"] == "user" and me["models"] == ["*"]
    assert me["quota"]["tokens_today"] == 13
    assert me["quota"]["limits"]["tokens_per_day"] == 500
    key = api.state.store.list_api_keys("alice")[0]
    assert me["key"]["id"] == key.id and me["key"]["display"] == key.display
    assert api.keys["alice"] not in resp.text


# ------------------------------------------------------------------ forward() directly
def test_forward_for_playground_without_key(new_state):
    state = new_state()
    account = state.config.accounts["alice"]

    async def go():
        resp = await forward(state, "chat.completions", dict(CHAT), account=account, key_id=None)
        await state.router.aclose()
        return resp

    resp = asyncio.run(go())
    assert resp.status_code == 200
    assert json.loads(resp.body)["model"] == "qwen3.5:9b"
    [row] = state.store.recent_usage("alice")
    assert row["key_id"] is None and row["client_ip"] == ""


def test_forward_does_not_mutate_caller_body(new_state):
    state = new_state(quota={"max_tokens_per_request": 10})
    body = {**CHAT, "model": "fast", "stream": True}
    snapshot = json.dumps(body, sort_keys=True)

    async def go():
        resp = await forward(state, "chat.completions", body,
                             account=state.config.accounts["alice"], key_id=None)
        await drive(resp)

    asyncio.run(go())
    assert json.dumps(body, sort_keys=True) == snapshot


def test_backend_semaphore_limits_concurrency(new_state, fake_net):
    state = new_state(backends=[{**OLLAMA, "max_concurrency": 1}],
                       quota={"max_concurrent": 10})
    fake_net.ollama.first_delay = 0.05
    account = state.config.accounts["alice"]

    async def one(stream: bool):
        resp = await forward(state, "chat.completions", {**CHAT, "stream": stream},
                             account=account, key_id=None)
        if stream:
            await drive(resp)
        return resp.status_code

    async def go():
        return await asyncio.gather(one(False), one(True), one(False), one(True))

    assert asyncio.run(go()) == [200] * 4
    assert fake_net.ollama.max_active == 1
    assert state.router.adapter("ollama").active == 0


# ------------------------------------------------------------------ client disconnects
async def drive(resp, *, fail_after: int | None = None, disconnect_after: float | None = None,
                spec: str = "2.4") -> list[bytes]:
    """Run a Response as an ASGI app. ``fail_after``: the client vanishes after that many
    body chunks (send raises OSError). ``disconnect_after``: receive() reports a
    disconnect after that many seconds (ASGI < 2.4 path)."""
    sent: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.body":
            if fail_after is not None and len(sent) >= fail_after:
                raise OSError("client went away")
            sent.append(message.get("body", b""))

    async def receive():
        if disconnect_after is None:
            await asyncio.sleep(3600)
        await asyncio.sleep(disconnect_after)
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": spec}, "method": "POST", "path": "/"}
    await resp(scope, receive, send)
    return sent


@pytest.mark.parametrize("mode", ["send_fails", "disconnect_message"])
def test_stream_client_disconnect_is_recorded_and_cleaned_up(new_state, fake_net, mode):
    state = new_state()
    fake_net.ollama.delay = 0.2
    fake_net.ollama.reply = "one two three four five six"
    account = state.config.accounts["alice"]

    async def go():
        resp = await forward(state, "chat.completions", {**CHAT, "stream": True},
                             account=account, key_id="key_x")
        if mode == "send_fails":
            with pytest.raises(ClientDisconnect):
                await drive(resp, fail_after=1)
        else:
            await drive(resp, disconnect_after=0.1, spec="2.3")
        await asyncio.sleep(0)  # let cancelled tasks settle

    started = time.monotonic()
    asyncio.run(go())
    assert time.monotonic() - started < 1.0  # did not wait for the whole upstream stream
    [row] = state.store.recent_usage("alice")
    assert row["status"] == "cancelled" and row["stream"] == 1
    assert row["usage_source"] == "estimate" and row["completion_tokens"] >= 1
    assert state.quota.usage(account)["in_flight"] == 0
    assert state.router.adapter("ollama").active == 0
    assert fake_net.ollama.active == 0  # upstream stream was closed


def test_streaming_response_never_sent_still_cleans_up(new_state):
    state = new_state()
    account = state.config.accounts["alice"]

    async def go():
        resp = await forward(state, "chat.completions", {**CHAT, "stream": True},
                             account=account, key_id=None)

        async def send(message):
            raise OSError("gone before headers")

        async def receive():
            await asyncio.sleep(3600)

        with pytest.raises(ClientDisconnect):
            await resp({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    asyncio.run(go())
    [row] = state.store.recent_usage("alice")
    assert row["status"] == "cancelled" and row["total_tokens"] == 0
    assert state.quota.usage(account)["in_flight"] == 0
    assert state.router.adapter("ollama").active == 0
