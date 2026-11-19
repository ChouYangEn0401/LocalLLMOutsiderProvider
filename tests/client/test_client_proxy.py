from __future__ import annotations

import dataclasses
import json
import logging

import pytest

from llmop import crypto
from llmop.client import proxy
from llmop.protocol import b64u_encode

from .fakes import API_KEY, PROMPT, REPLY_TEXT, completion

MODEL = "qwen3:8b"
MESSAGES = [{"role": "user", "content": PROMPT}]


@pytest.fixture
async def client(config, signing_key, aiohttp_client):
    return await aiohttp_client(proxy.make_app(config, signing_key, keepalive=0.05))


def parse_sse(text: str) -> list[str]:
    """回傳每個事件的原文（"data: …" 或註解）。所有事件都必須以空行結尾，每行都要是 data 或註解。"""
    assert text.endswith("\n\n")
    events = text[:-2].split("\n\n")
    for event in events:
        for line in event.split("\n"):
            assert line.startswith("data: ") or line.startswith(":"), line
    return events


def data_events(events: list[str]) -> list[str]:
    return [e[len("data: ") :] for e in events if e.startswith("data: ")]


# ---------------------------------------------------------------------------
# 非串流
# ---------------------------------------------------------------------------


async def test_non_stream_strips_fields_and_sets_header(client, fake):
    request = {
        "model": MODEL,
        "messages": MESSAGES,
        "temperature": 0.3,
        "stream": False,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 77,
    }
    resp = await client.post("/v1/chat/completions", json=request)
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == REPLY_TEXT
    header, sealed_body = fake.received[0]
    assert (header.model, header.max_tokens) == (MODEL, 77)
    assert sealed_body == {"messages": MESSAGES, "temperature": 0.3}


@pytest.mark.parametrize(
    ("limits", "expected"),
    [({"max_tokens": 55, "max_completion_tokens": 99}, 55), ({"max_completion_tokens": 99}, 99), ({}, 1024)],
)
async def test_max_tokens_source(client, fake, limits, expected):
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, **limits})
    assert resp.status == 200
    header, sealed_body = fake.received[0]
    assert header.max_tokens == expected
    assert sealed_body == {"messages": MESSAGES}


async def test_models_list_is_openai_format(client, fake):
    unpinned = b64u_encode(crypto.public_raw(crypto.generate_worker_key()))
    fake.models = [fake.entry(MODEL), fake.entry(MODEL), fake.entry("hidden", unpinned)]
    resp = await client.get("/v1/models")
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == [MODEL]
    assert body["data"][0]["object"] == "model"


# ---------------------------------------------------------------------------
# 串流
# ---------------------------------------------------------------------------


async def test_stream_is_sse_with_waiting_one_chunk_and_done(client, fake):
    fake.delay = 0.3
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": True})
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    events = parse_sse(await resp.text())
    assert ": waiting" in events
    data = data_events(events)
    assert data[-1] == "[DONE]"
    chunks = [json.loads(d) for d in data[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    content_chunks = [c for c in chunks if any("content" in ch["delta"] for ch in c["choices"])]
    assert len(content_chunks) == 1
    delta = content_chunks[0]["choices"][0]["delta"]
    assert delta == {"role": "assistant", "content": REPLY_TEXT}
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["delta"] == {}
    assert final["usage"]["completion_tokens"] == 5
    # stream 與 stream_options 不會被封裝進去。
    assert "stream" not in fake.received[0][1]


async def test_stream_tool_calls_and_reasoning_carry_index(client, fake):
    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"台北"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "get_time", "arguments": "{}"}},
    ]
    message = {"role": "assistant", "content": None, "reasoning_content": "想一想", "tool_calls": tool_calls}
    fake.reply = lambda body, header: {"ok": True, "body": completion(header, message, "tool_calls")}
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": True})
    chunks = [json.loads(d) for d in data_events(parse_sse(await resp.text()))[:-1]]
    delta = chunks[0]["choices"][0]["delta"]
    assert delta["reasoning_content"] == "想一想"
    assert [c["index"] for c in delta["tool_calls"]] == [0, 1]
    assert delta["tool_calls"][0]["function"]["name"] == "get_weather"
    assert delta["tool_calls"][1]["id"] == "call_2"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# 錯誤
# ---------------------------------------------------------------------------


def assert_openai_error(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"message", "type", "code"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


async def test_gateway_error_uses_protocol_status(client, fake):
    fake.gateway_error = "quota_exceeded"
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES})
    assert resp.status == 429
    assert_openai_error(await resp.json(), "quota_exceeded")


async def test_sealed_error_is_502(client, fake):
    fake.reply = lambda body, header: {"ok": False, "error": {"code": "backend_error", "message": "後端掛了"}}
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES})
    assert resp.status == 502
    body = await resp.json()
    assert_openai_error(body, "backend_error")
    assert body["error"]["type"] == "server_error"


async def test_unpinned_model_is_404(client, fake):
    resp = await client.post("/v1/chat/completions", json={"model": "nobody-serves-this", "messages": MESSAGES})
    assert resp.status == 404
    assert_openai_error(await resp.json(), "model_unavailable")
    assert fake.posts == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json",
        b"[1, 2]",
        json.dumps({"messages": MESSAGES}).encode(),
        json.dumps({"model": MODEL, "messages": MESSAGES, "max_tokens": 0}).encode(),
        json.dumps({"model": MODEL, "messages": MESSAGES, "max_tokens": "10"}).encode(),
        json.dumps({"model": MODEL, "messages": MESSAGES, "stream": "yes"}).encode(),
        b'{"model": "qwen3:8b", "messages": [], "temperature": NaN}',
    ],
)
async def test_bad_request_is_400(client, fake, payload):
    resp = await client.post("/v1/chat/completions", data=payload, headers={"Content-Type": "application/json"})
    assert resp.status == 400
    body = await resp.json()
    assert_openai_error(body, "bad_request")
    assert body["error"]["type"] == "invalid_request_error"
    assert fake.posts == 0


async def test_stream_error_after_headers_is_data_event(client, fake):
    fake.gateway_error = "worker_busy"
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": True})
    assert resp.status == 200
    data = data_events(parse_sse(await resp.text()))
    assert len(data) == 1
    assert_openai_error(json.loads(data[0]), "worker_busy")


async def test_models_gateway_error_shape(config, signing_key, aiohttp_client, fake):
    bad = dataclasses.replace(config, api_key="llmop_sk_wrong")
    client = await aiohttp_client(proxy.make_app(bad, signing_key))
    resp = await client.get("/v1/models")
    assert resp.status == 401
    assert_openai_error(await resp.json(), "unauthorized")


# ---------------------------------------------------------------------------
# 綁定位址
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])
async def test_refuses_non_loopback_host(config, signing_key, host):
    with pytest.raises(ValueError):
        await proxy.start_proxy(config, signing_key, host=host, port=0)


async def test_starts_on_loopback(config, signing_key, session):
    runner = await proxy.start_proxy(config, signing_key, port=0)
    try:
        (address,) = runner.addresses
        assert address[0] == "127.0.0.1"
        async with session.get(f"http://127.0.0.1:{address[1]}/v1/models") as resp:
            assert resp.status == 200
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# 紀錄
# ---------------------------------------------------------------------------


async def test_logs_never_contain_prompt_reply_or_secrets(client, fake, signing_key, caplog):
    caplog.set_level(logging.DEBUG)
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES})
    assert resp.status == 200
    fake.delay = 0.2
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": True})
    assert REPLY_TEXT in await resp.text()
    fake.reply = lambda body, header: {"ok": False, "error": {"code": "backend_error", "message": f"壞了：{PROMPT}"}}
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES})
    assert resp.status == 502

    text = caplog.text
    request_ids = [header.request_id for header, _ in fake.received]
    assert all(rid in text for rid in request_ids)  # 確實有紀錄
    assert fake.key_id in text
    for secret in (PROMPT, REPLY_TEXT, API_KEY, b64u_encode(crypto.private_raw(signing_key))):
        assert secret not in text


# ---------------------------------------------------------------------------
# 本機防護：Host、Origin、Sec-Fetch-Site、Content-Type
# ---------------------------------------------------------------------------


def chat_request() -> dict:
    return {"model": MODEL, "messages": MESSAGES}


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "LocalHost", "[::1]"])
async def test_loopback_host_with_port_allowed(client, fake, name):
    port = client.server.port
    resp = await client.post("/v1/chat/completions", json=chat_request(), headers={"Host": f"{name}:{port}"})
    assert resp.status == 200
    assert fake.posts == 1


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",
        "evil.example.com:{port}",
        "127.0.0.1",
        "localhost",
        "127.0.0.1:{other}",
        "localhost.:{port}",
        "127.0.0.2:{port}",
        "[::1]",
        "::1:{port}",
        "0.0.0.0:{port}",
        "127.0.0.1:{port}.evil.com",
        "",
    ],
)
async def test_wrong_host_is_403(client, fake, host):
    port = client.server.port
    host = host.format(port=port, other=port + 1 if port < 65535 else port - 1)
    for method, path, kwargs in (("POST", "/v1/chat/completions", {"json": chat_request()}), ("GET", "/v1/models", {})):
        resp = await client.request(method, path, headers={"Host": host}, **kwargs)
        assert resp.status == 403
        assert_openai_error(await resp.json(), "forbidden_host")
    assert fake.posts == 0


@pytest.mark.parametrize(
    "content_type", ["text/plain", "text/plain;charset=UTF-8", "application/x-www-form-urlencoded", "multipart/form-data", None]
)
async def test_non_json_content_type_is_415(client, fake, content_type):
    headers = {"Content-Type": content_type} if content_type else {}
    resp = await client.post("/v1/chat/completions", data=json.dumps(chat_request()).encode(), headers=headers)
    assert resp.status == 415
    assert_openai_error(await resp.json(), "unsupported_media_type")
    assert fake.posts == 0


async def test_json_content_type_with_charset_allowed(client, fake):
    resp = await client.post(
        "/v1/chat/completions",
        data=json.dumps(chat_request()).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status == 200


@pytest.mark.parametrize("origin", ["https://evil.example.com", "null", "http://127.0.0.1:{port}", ""])
async def test_origin_present_is_403(client, fake, origin):
    origin = origin.format(port=client.server.port)
    for method, path, kwargs in (("POST", "/v1/chat/completions", {"json": chat_request()}), ("GET", "/v1/models", {})):
        resp = await client.request(method, path, headers={"Origin": origin}, **kwargs)
        assert resp.status == 403
        assert_openai_error(await resp.json(), "forbidden_origin")
    assert fake.posts == 0


async def test_sec_fetch_site_cross_site_is_403(client, fake):
    for method, path, kwargs in (("POST", "/v1/chat/completions", {"json": chat_request()}), ("GET", "/v1/models", {})):
        resp = await client.request(method, path, headers={"Sec-Fetch-Site": "cross-site"}, **kwargs)
        assert resp.status == 403
        assert_openai_error(await resp.json(), "forbidden_origin")
    assert fake.posts == 0


async def test_sec_fetch_site_none_allowed(client, fake):
    resp = await client.post("/v1/chat/completions", json=chat_request(), headers={"Sec-Fetch-Site": "none"})
    assert resp.status == 200


async def test_guard_checks_host_before_content_type(client, fake):
    resp = await client.post(
        "/v1/chat/completions", data=b"x", headers={"Host": "evil.example.com", "Content-Type": "text/plain"}
    )
    assert resp.status == 403


async def test_real_proxy_rejects_rebinding_host(config, signing_key, session):
    runner = await proxy.start_proxy(config, signing_key, port=0)
    try:
        (address,) = runner.addresses
        port = address[1]
        async with session.get(f"http://127.0.0.1:{port}/v1/models", headers={"Host": f"rebind.evil.com:{port}"}) as resp:
            assert resp.status == 403
        async with session.get(f"http://127.0.0.1:{port}/v1/models", headers={"Host": f"localhost:{port}"}) as resp:
            assert resp.status == 200
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# 排隊資訊轉成保活註解
# ---------------------------------------------------------------------------


async def test_stream_queue_info_becomes_keepalive_comment(config, signing_key, aiohttp_client, fake):
    fake.pending_polls = 2
    fake.poll_delay = 0.15
    fake.queue = {"position": 3, "eta": 40}
    client = await aiohttp_client(proxy.make_app(config, signing_key, keepalive=0.03))
    resp = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": True})
    events = parse_sse(await resp.text())
    assert ": queue position=3 eta=40" in events
    assert data_events(events)[-1] == "[DONE]"


@pytest.mark.parametrize(
    ("queue", "expected"),
    [
        (None, b": waiting\n\n"),
        ({}, b": waiting\n\n"),
        ({"position": 3, "eta": 40}, b": queue position=3 eta=40\n\n"),
        ({"position": 0}, b": queue position=0\n\n"),
    ],
)
def test_keepalive_comment(queue, expected):
    assert proxy.keepalive_comment(queue) == expected
