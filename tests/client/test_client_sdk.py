from __future__ import annotations

import dataclasses
import socket
import time

import pytest

from llmop import crypto
from llmop.client import sdk
from llmop.client.sdk import ClientError, WorkerModel
from llmop.protocol import b64u_encode

from .fakes import PROMPT, REPLY_TEXT

MODEL = "qwen3:8b"
BODY = {"messages": [{"role": "user", "content": PROMPT}], "temperature": 0.2}


def other_worker_key() -> str:
    return b64u_encode(crypto.public_raw(crypto.generate_worker_key()))


async def run_chat(session, config, signing_key, **kwargs):
    kwargs.setdefault("model", MODEL)
    kwargs.setdefault("max_tokens", 256)
    return await sdk.chat(session, config, signing_key, dict(BODY), **kwargs)


async def test_happy_path_immediate_200(session, config, signing_key, fake):
    result = await run_chat(session, config, signing_key)
    assert result["choices"][0]["message"]["content"] == REPLY_TEXT
    assert fake.posts == 1 and fake.polls == 0
    header, body = fake.received[0]
    assert (header.model, header.max_tokens, header.key_id) == (MODEL, 256, fake.key_id)
    assert body == BODY


async def test_happy_path_202_then_polls(session, config, signing_key, fake):
    fake.pending_polls = 3
    result = await run_chat(session, config, signing_key)
    assert result["choices"][0]["message"]["content"] == REPLY_TEXT
    assert fake.posts == 1 and fake.polls == 3


async def test_list_models_ignores_unpinned_keys(session, config, fake):
    unpinned = other_worker_key()
    fake.models = [fake.entry(MODEL), fake.entry(MODEL, unpinned), fake.entry("only-unpinned", unpinned)]
    assert await sdk.list_models(session, config) == [WorkerModel(MODEL, fake.key_id, fake.public_key)]


async def test_model_served_only_by_unpinned_worker_sends_nothing(session, config, signing_key, fake):
    fake.models = [fake.entry(MODEL), fake.entry("only-unpinned", other_worker_key())]
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key, model="only-unpinned")
    assert info.value.code == "model_unavailable"
    assert "釘選" in info.value.message
    assert fake.posts == 0


async def test_entry_with_wrong_key_id_is_dropped(session, config, signing_key, fake):
    wrong = crypto.worker_key_id(crypto.public_raw(crypto.generate_worker_key()))
    fake.models = [fake.entry(MODEL, key_id=wrong)]
    assert await sdk.list_models(session, config) == []
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key)
    assert info.value.code == "model_unavailable"
    assert fake.posts == 0


async def test_first_pinned_worker_in_config_order_is_used(session, config, signing_key, fake):
    # 設定檔裡排在前面的釘選公鑰優先，與閘道清單的順序無關。
    second_pub = other_worker_key()
    config = dataclasses.replace(config, pinned_workers=(second_pub, fake.public_key))
    fake.models = [fake.entry(MODEL), fake.entry(MODEL, second_pub)]
    chosen = await sdk._pick_worker(session, config, MODEL)
    assert chosen.public_key == second_pub


async def test_gateway_error_raises_with_code(session, config, signing_key, fake):
    fake.gateway_error = "quota_exceeded"
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key)
    assert (info.value.code, info.value.status, info.value.sealed) == ("quota_exceeded", 429, False)
    assert fake.posts == 1 and fake.polls == 0  # 閘道錯誤不重試


async def test_sealed_error_raises_with_code(session, config, signing_key, fake):
    fake.reply = lambda body, header: {"ok": False, "error": {"code": "backend_error", "message": "後端掛了"}}
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key)
    assert (info.value.code, info.value.status, info.value.sealed) == ("backend_error", 502, True)
    assert info.value.message == "後端掛了"


async def test_reply_sealed_for_other_request_fails(session, config, signing_key, fake):
    fake.wrong_request = True
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key)
    assert info.value.code == "decrypt_failed"
    assert info.value.status == 502


async def test_overall_deadline_ends_polling(session, config, signing_key, fake):
    fake.pending_polls = 10**9
    fake.poll_delay = 0.05
    started = time.monotonic()
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key, deadline=0.4)
    assert info.value.code == "timeout"
    assert info.value.status == 504
    assert time.monotonic() - started < 2
    assert fake.polls >= 1


async def test_network_failure_while_polling_is_retried(session, config, signing_key, fake):
    fake.pending_polls = 1
    fake.flaky_polls = 2
    result = await run_chat(session, config, signing_key)
    assert result["choices"][0]["message"]["content"] == REPLY_TEXT
    assert fake.posts == 1 and fake.polls == 3


async def test_network_failure_on_post_then_poll_finds_request(session, config, signing_key, fake):
    fake.drop_post = "after"
    result = await run_chat(session, config, signing_key)
    assert result["choices"][0]["message"]["content"] == REPLY_TEXT
    assert fake.posts == 1 and fake.polls == 1


async def test_network_failure_on_post_then_unknown_request_raises(session, config, signing_key, fake):
    fake.drop_post = "before"
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key)
    assert info.value.code == "network_error"
    assert fake.posts == 1 and fake.polls == 1


async def test_unreachable_gateway_raises_network_error(session, config, signing_key):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = dataclasses.replace(config, gateway_url=f"http://127.0.0.1:{port}")
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key)
    assert info.value.code == "network_error"


@pytest.mark.parametrize("kwargs", [{"max_tokens": 0}, {"max_tokens": 1_000_001}, {"model": ""}, {"model": "m" * 201}])
async def test_invalid_parameters_send_nothing(session, config, signing_key, fake, kwargs):
    with pytest.raises(ClientError) as info:
        await run_chat(session, config, signing_key, **kwargs)
    assert (info.value.code, info.value.status) == ("bad_request", 400)
    assert fake.posts == 0


# ---------------------------------------------------------------------------
# gate 路徑、/e2e/v1、key 前綴、排隊資訊
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gateway_url", "expected"),
    [
        ("https://demo.example.com", "https://demo.example.com/e2e/v1/models"),
        ("https://demo.example.com/", "https://demo.example.com/e2e/v1/models"),
        ("https://demo.example.com/g4te-S3cret", "https://demo.example.com/g4te-S3cret/e2e/v1/models"),
        ("https://demo.example.com/g4te-S3cret/", "https://demo.example.com/g4te-S3cret/e2e/v1/models"),
        ("http://127.0.0.1:8782/abc", "http://127.0.0.1:8782/abc/e2e/v1/models"),
    ],
)
def test_url_composes_gate_and_e2e_prefix(config, gateway_url, expected):
    config = dataclasses.replace(config, gateway_url=gateway_url)
    assert sdk._url(config, "/models") == expected


async def test_chat_through_gate_path(session, signing_key, aiohttp_server):
    from llmop.client.config import ClientConfig

    from .fakes import API_KEY, FakeGateway

    gate = "g4te-S3cret_x"
    fake = FakeGateway(crypto.public_raw(signing_key))
    fake.pending_polls = 1
    server = await aiohttp_server(fake.app(gate=gate))
    base = str(server.make_url("")).rstrip("/")
    config = ClientConfig(gateway_url=f"{base}/{gate}/", api_key=API_KEY, pinned_workers=(fake.public_key,))
    result = await run_chat(session, config, signing_key)
    assert result["choices"][0]["message"]["content"] == REPLY_TEXT
    assert fake.posts == 1 and fake.polls == 1
    # 少了 gate 就找不到 API。
    no_gate = dataclasses.replace(config, gateway_url=base)
    with pytest.raises(ClientError):
        await sdk.list_models(session, no_gate)


@pytest.mark.parametrize("api_key", ["sk-llop-" + "B" * 43, "llmop_sk_" + "C" * 43])
async def test_both_key_prefixes_work(session, signing_key, aiohttp_server, api_key):
    from llmop.client.config import ClientConfig

    from .fakes import FakeGateway

    fake = FakeGateway(crypto.public_raw(signing_key), api_key=api_key)
    server = await aiohttp_server(fake.app())
    config = ClientConfig(
        gateway_url=str(server.make_url("")).rstrip("/"), api_key=api_key, pinned_workers=(fake.public_key,)
    )
    result = await run_chat(session, config, signing_key)
    assert result["choices"][0]["message"]["content"] == REPLY_TEXT


async def test_on_queue_receives_pending_queue_info(session, config, signing_key, fake):
    fake.pending_polls = 2
    fake.queue = {"position": 3, "eta": 40, "secret": "x"}
    seen = []
    result = await run_chat(session, config, signing_key, on_queue=seen.append)
    assert result["choices"][0]["message"]["content"] == REPLY_TEXT
    assert seen and all(q == {"position": 3, "eta": 40} for q in seen)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({}, None),
        ({"queue": None}, None),
        ({"queue": "3"}, None),
        ({"queue": {"position": 3, "eta": 40}}, {"position": 3, "eta": 40}),
        ({"queue": {"position": 2, "eta": 12.6}}, {"position": 2, "eta": 13}),
        ({"queue": {"position": "3\n\ndata: x", "eta": True}}, None),
        ({"queue": {"position": -1, "eta": float("nan")}}, None),
        ({"queue": {"position": 1, "eta": float("inf")}}, {"position": 1}),
    ],
)
def test_queue_info_keeps_only_integers(data, expected):
    assert sdk.queue_info(data) == expected
