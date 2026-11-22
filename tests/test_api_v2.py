"""DISPATCH T-6: the direct ``/v1`` surface per zone (single model, scheduler queue,
usage tables, entrance policy, body limit, context overflow, hidden backend errors)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI

from llmop import security
from llmop.api import build_api_router, forward, proxy, routes
from llmop.app import build_state
from llmop.config import parse_config
from llmop.quota import QuotaManager
from llmop.scheduler import Scheduler
from llmop.store import Store
from llmop.usage import UsageManager

FAST_HASH = security.hash_password("pw", n=2**4)
OLLAMA = {"name": "ollama", "type": "ollama", "base_url": "http://ollama.test:11434"}
ACCOUNTS = {
    "alice": {"password_hash": FAST_HASH},
    "root": {"password_hash": FAST_HASH, "role": "admin"},
}
MODEL = "qwen3.5:9b"
CHAT = {"model": "whatever-the-client-says",
        "messages": [{"role": "user", "content": "Say hello please"}]}


def make_state(net, *, backends=None, scheduler=None, **extra):
    cfg = parse_config({
        "backends": backends or [OLLAMA],
        "accounts": ACCOUNTS,
        "model": MODEL,
        "local_account": "root",
        **extra,
    })
    state = build_state(cfg, store=Store(":memory:"), transport=net.transport)
    usage = UsageManager(state.store, state.holder)
    state.usage = usage
    state.quota = QuotaManager(state.store, tables=usage)
    state.scheduler = scheduler or Scheduler(slots=2)
    return state


def build_app(state, zone, **kw) -> FastAPI:
    app = FastAPI()
    app.include_router(build_api_router(state, zone, **kw))
    return app


def run(coro):
    return asyncio.run(coro)


async def call(state, zone, method, path, *, key=None, json_body=None, content=None,
               headers=None, **kw):
    h = dict(headers or {})
    if key:
        h["Authorization"] = f"Bearer {key}"
    transport = httpx.ASGITransport(app=build_app(state, zone, **kw))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json=json_body, content=content, headers=h)


def sse_data(text: str) -> list:
    out = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            data = line[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


@pytest.fixture
def st(fake_net):
    made = []

    def factory(**kw):
        made.append(make_state(fake_net, **kw))
        return made[-1]

    yield factory
    for s in made:
        s.store.close()


# ------------------------------------------------------------------ zones, model
def test_unknown_zone_rejected(st):
    with pytest.raises(ValueError):
        build_api_router(st(), "lan")


def test_model_is_always_overridden(st, fake_net):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=CHAT))
    assert resp.status_code == 200, resp.text
    assert fake_net.ollama.last["model"] == MODEL
    assert resp.json()["model"] == MODEL
    # No model at all is fine too.
    body = {"messages": CHAT["messages"]}
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=body))
    assert resp.status_code == 200, resp.text


def test_models_lists_only_the_configured_model(st):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    resp = run(call(state, "public", "GET", "/v1/models", key=key))
    data = resp.json()["data"]
    assert [m["id"] for m in data] == [MODEL]
    assert run(call(state, "public", "GET", f"/v1/models/{MODEL}", key=key)).status_code == 200
    resp = run(call(state, "public", "GET", "/v1/models/llama3", key=key))
    assert resp.status_code == 404


def test_local_zone_needs_no_key_and_uses_local_account(st):
    state = st()
    resp = run(call(state, "local", "POST", "/v1/chat/completions", json_body=CHAT))
    assert resp.status_code == 200, resp.text
    [row] = state.store.recent_usage(None)
    assert row["username"] == "root" and row["key_id"] is None
    me = run(call(state, "local", "GET", "/v1/me")).json()
    assert me["username"] == "root" and me["key"] is None and me["zone"] == "local"


def test_local_zone_streams_by_default(st):
    state = st()
    resp = run(call(state, "local", "POST", "/v1/chat/completions",
                    json_body={**CHAT, "stream": True}))
    events = sse_data(resp.text)
    assert len(events) > 3 and events[-1] == "[DONE]"


def test_public_zone_requires_key(st):
    state = st()
    resp = run(call(state, "public", "POST", "/v1/chat/completions", json_body=CHAT))
    assert resp.status_code == 401


def test_public_zone_refuses_encryption_required_key(st):
    state = st()
    key, _ = state.store.create_api_key("alice", "k", encryption="required")
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=CHAT))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "encryption_required"


# ------------------------------------------------------------------ streaming policy
def test_public_zone_answers_stream_with_one_chunk(st, fake_net):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    body = {**CHAT, "stream": True, "stream_options": {"include_usage": True}}
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=body))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert fake_net.ollama.last.get("stream") is False
    assert "stream_options" not in fake_net.ollama.last
    chunk, done = sse_data(resp.text)
    assert done == "[DONE]"
    assert chunk["object"] == "chat.completion.chunk" and chunk["model"] == MODEL
    assert chunk["choices"][0]["delta"] == {"role": "assistant", "content": "Hello world!"}
    assert chunk["choices"][0]["finish_reason"] == "stop"
    assert chunk["usage"]["total_tokens"] == 13
    [row] = state.store.recent_usage(None)
    assert row["stream"] == 1 and row["total_tokens"] == 13


def test_public_zone_can_enable_streaming(st):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key,
                    json_body={**CHAT, "stream": True}, public_stream=True))
    assert len(sse_data(resp.text)) > 3


# ------------------------------------------------------------------ queue
def test_scheduler_replaces_backend_semaphore(st, fake_net):
    state = st(backends=[{**OLLAMA, "max_concurrency": 1}],
               accounts={**ACCOUNTS, "root": {"password_hash": FAST_HASH, "role": "admin",
                                              "tier": "vip3"}})
    fake_net.ollama.first_delay = 0.1
    account = state.config.accounts["root"]

    async def go():
        return await asyncio.gather(*(
            forward(state, "chat.completions", dict(CHAT), account=account, key_id=None,
                    zone="local") for _ in range(2)))

    assert [r.status_code for r in run(go())] == [200, 200]
    assert fake_net.ollama.max_active == 2  # the scheduler's 2 slots, not max_concurrency=1
    assert state.router.adapter("ollama").active == 0
    assert state.scheduler.running == 0


def test_queue_timeout_is_429_with_retry_after(st):
    sched = Scheduler(slots=1, max_wait_seconds={"public": 0.05})
    state = st(scheduler=sched)
    key, _ = state.store.create_api_key("alice", "k")

    async def go():
        async with sched.slot("someone", "vip3", "public"):
            return await call(state, "public", "POST", "/v1/chat/completions", key=key,
                              json_body=CHAT)

    resp = run(go())
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["code"] == "queue_wait"
    assert "目前排第 1 位" in err["message"] and "queue position 1" in err["message"]
    assert int(resp.headers["Retry-After"]) >= 1
    assert state.store.recent_usage(None) == []  # never served: nothing recorded
    assert state.quota.usage(state.config.accounts["alice"])["in_flight"] == 0


def test_queue_full_is_503_with_retry_after(st):
    sched = Scheduler(slots=1, max_length=0)
    state = st(scheduler=sched)
    key, _ = state.store.create_api_key("alice", "k")

    async def go():
        async with sched.slot("someone", "vip3", "public"):
            return await call(state, "public", "POST", "/v1/chat/completions", key=key,
                              json_body=CHAT)

    resp = run(go())
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "queue_full"
    assert "Retry-After" in resp.headers


def test_queue_waited_header(st):
    sched = Scheduler(slots=1)
    state = st(scheduler=sched)
    key, _ = state.store.create_api_key("alice", "k")

    async def go():
        cm = sched.slot("someone", "vip3", "public")
        await cm.__aenter__()
        task = asyncio.create_task(call(state, "public", "POST", "/v1/chat/completions",
                                        key=key, json_body=CHAT))
        await asyncio.sleep(0.1)
        await cm.__aexit__(None, None, None)
        return await task

    resp = run(go())
    assert resp.status_code == 200
    assert float(resp.headers["X-LLMOP-Queue-Waited"]) >= 0.05


async def drive(resp, *, until_closed=None) -> list[bytes]:
    sent: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.body":
            sent.append(message.get("body", b""))

    async def receive():
        await asyncio.sleep(3600)

    await resp({"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST",
                "path": "/"}, receive, send)
    return sent


def test_streaming_request_gets_queue_comments(st, monkeypatch):
    monkeypatch.setattr(proxy, "QUEUE_NOTICE_SECONDS", 0.03)
    sched = Scheduler(slots=1)
    state = st(scheduler=sched)
    account = state.config.accounts["alice"]

    async def go():
        cm = sched.slot("someone", "vip3", "local")
        await cm.__aenter__()
        resp = await forward(state, "chat.completions", {**CHAT, "stream": True},
                             account=account, key_id=None, zone="local")
        task = asyncio.create_task(drive(resp))
        await asyncio.sleep(0.15)
        await cm.__aexit__(None, None, None)
        return await task

    body = b"".join(run(go())).decode()
    assert ": queue position=1 eta=" in body and "tier=normal" in body
    assert body.count(": queue") >= 2
    assert sse_data(body)[-1] == "[DONE]"
    [row] = state.store.recent_usage(None)
    assert row["status"] == "ok"


# ------------------------------------------------------------------ usage, /v1/me
def test_usage_tables_charged_after_request(st):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=CHAT))
    status = state.usage.status(state.config.accounts["alice"])
    assert status["weekly"]["used"] == 13
    assert status["bucket"]["level"] == status["bucket"]["capacity"] - 13


def test_me_reports_tier_status_tables_and_queue(st):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=CHAT))
    me = run(call(state, "public", "GET", "/v1/me", key=key)).json()
    assert me["username"] == "alice" and me["tier"] == "normal" and me["status"] == "NORMAL"
    assert me["usage"]["weekly"]["used"] == 13 and me["usage"]["bucket"]["capacity"] == 80000
    assert me["queue"]["slots"] == 2 and me["queue"]["position"] is None
    assert me["models"] == [MODEL] and me["key"]["encryption"] == "optional"


def test_guest_key_on_public_zone(st):
    state = st()
    plain, rec = state.store.create_guest_key("visitor", budget_tokens=1000)
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=plain, json_body=CHAT))
    assert resp.status_code == 200, resp.text
    [row] = state.store.recent_usage(None)
    assert row["username"] == f"guest:{rec.id}" and row["key_id"] == rec.id
    me = run(call(state, "public", "GET", "/v1/me", key=plain)).json()
    assert me["role"] == "guest" and me["tier"] == "guest"
    assert me["usage"]["guest"]["used"] == 13


def test_exhausted_guest_key_is_401(st):
    state = st()
    plain, rec = state.store.create_guest_key("visitor", budget_tokens=10)
    assert run(call(state, "public", "POST", "/v1/chat/completions", key=plain,
                    json_body=CHAT)).status_code == 200
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=plain, json_body=CHAT))
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "key_exhausted"


# ------------------------------------------------------------------ body limit
def test_body_limit_by_content_length(st):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    big = b"x" * (4 * 1024 * 1024 + 1)
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, content=big,
                    headers={"content-type": "application/json"}))
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "request_too_large"


def test_body_limit_while_reading_chunked(st, monkeypatch):
    monkeypatch.setattr(routes, "MAX_BODY_BYTES", 1000)
    state = st()
    key, _ = state.store.create_api_key("alice", "k")

    async def gen():
        for _ in range(5):
            yield b" " * 300

    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, content=gen()))
    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 413
    # under the limit: fine
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=CHAT))
    assert resp.status_code == 200


# ------------------------------------------------------------------ backend errors
CONTEXT_FAIL = (500, {"code": 500, "message": "Context size has been exceeded.",
                      "type": "server_error"})


def test_context_overflow_is_400(st, fake_net):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    fake_net.ollama.fail = CONTEXT_FAIL
    resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=CHAT))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "context_length_exceeded"


def test_context_overflow_in_stream(st, fake_net):
    state = st()
    fake_net.ollama.fail = (400, {"error": {"message": "the request exceeds the available "
                                            "context size, try increasing it",
                                            "type": "exceed_context_size_error"}})
    resp = run(call(state, "local", "POST", "/v1/chat/completions",
                    json_body={**CHAT, "stream": True}))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "context_length_exceeded"


def test_backend_error_details_neither_in_response_nor_in_log(st, fake_net, caplog):
    """The backend's wording stays out of the response (public zone), the log and the
    usage record: it may quote the conversation. Status, code and backend are kept."""
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    fake_net.ollama.fail = (500, "llama runner process has terminated: secret path /x/y")
    with caplog.at_level(logging.DEBUG):
        resp = run(call(state, "public", "POST", "/v1/chat/completions", key=key,
                        json_body=CHAT))
    assert resp.status_code == 502
    assert "llama runner" not in resp.text and "ollama" not in resp.text
    assert "llama runner" not in caplog.text
    assert "backend error" in caplog.text and "HTTP 502" in caplog.text
    [row] = state.store.recent_usage(None)
    assert "llama runner" not in row["error"]
    assert row["error"].startswith("HTTP 502") and "ollama" in row["error"]


def test_backend_error_quoting_the_prompt_never_reaches_log_or_db(st, fake_net, caplog):
    """vLLM-style validation errors echo the input; plain /v1 must not write it anywhere."""
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    marker = "PROMPT-MARKER-7c1f"
    fake_net.ollama.fail = (400, {"object": "error", "message": f"invalid input: '{marker}'"})
    body = {"model": CHAT["model"], "messages": [{"role": "user", "content": marker}]}
    with caplog.at_level(logging.DEBUG):
        run(call(state, "public", "POST", "/v1/chat/completions", key=key, json_body=body))
        run(call(state, "local", "POST", "/v1/chat/completions",
                 json_body={**body, "stream": True}))
    assert marker not in caplog.text
    assert all(marker not in str(r) for r in state.store.recent_usage(None))


def test_backend_error_hidden_in_stream_event(st, fake_net, caplog):
    state = st()
    fake_net.ollama.mid_stream_error = True
    with caplog.at_level(logging.DEBUG):
        resp = run(call(state, "local", "POST", "/v1/chat/completions",
                        json_body={**CHAT, "stream": True}))
    events = sse_data(resp.text)
    err = events[-1]["error"]
    assert "out of memory" not in resp.text and err["code"] == "backend_error"
    assert "out of memory" not in caplog.text
    [row] = state.store.recent_usage(None)
    assert "out of memory" not in row["error"] and row["error"].startswith("HTTP ")


def test_context_overflow_also_without_zone(st, fake_net):
    state = st()
    key, _ = state.store.create_api_key("alice", "k")
    fake_net.ollama.fail = (400, {"object": "error", "type": "BadRequestError", "code": 400,
                                  "message": "This model's maximum context length is 4096 "
                                             "tokens. However, you requested 5000 tokens."})
    app = FastAPI()
    app.include_router(build_api_router(state))  # v0.1 call, no zone

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post("/v1/chat/completions", json=CHAT,
                                     headers={"Authorization": f"Bearer {key}"})

    resp = run(go())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "context_length_exceeded"


def test_too_many_waiting_per_account_is_429(st):
    sched = Scheduler(slots=1, max_per_account=0)
    state = st(scheduler=sched)
    key, _ = state.store.create_api_key("alice", "k")

    async def go():
        async with sched.slot("someone", "vip3", "public"):
            return await call(state, "public", "POST", "/v1/chat/completions", key=key,
                              json_body=CHAT)

    resp = run(go())
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "queue_full"
    assert "Retry-After" in resp.headers


def test_disconnect_while_queued_leaves_queue_without_usage(st):
    sched = Scheduler(slots=1)
    state = st(scheduler=sched)
    account = state.config.accounts["alice"]

    async def go():
        async with sched.slot("someone", "vip3", "local"):
            resp = await forward(state, "chat.completions", {**CHAT, "stream": True},
                                 account=account, key_id=None, zone="local")
            assert sched.waiting == 1

            async def send(message):
                raise OSError("client went away")

            async def receive():
                await asyncio.sleep(3600)

            with pytest.raises(Exception):
                await resp({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            await asyncio.sleep(0)
            assert sched.waiting == 0

    run(go())
    assert state.store.recent_usage(None) == []
    assert state.quota.usage(account)["in_flight"] == 0
    assert state.usage.status(account)["weekly"]["used"] == 0
