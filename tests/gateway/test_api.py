"""用戶端 API：正常流程、202 輪詢、九個步驟的拒絕順序、配額、worker 錯誤與逾時。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from llmop import crypto
from llmop.gateway import api
from llmop.gateway.store import utc_day
from llmop.protocol import MAX_BODY_BYTES, b64u_decode, b64u_encode

from .conftest import DEFAULT_USAGE


def assert_error(status: int, body: dict, expected_status: int, code: str, request_id: str | None = None) -> None:
    assert (status, body["status"], body["error"]["code"]) == (expected_status, "error", code), body
    assert body["error"]["message"]
    if request_id is None:
        assert "request_id" not in body
    else:
        assert body["request_id"] == request_id


# ---------------------------------------------------------------------------
# 正常流程
# ---------------------------------------------------------------------------


async def test_happy_path(gateway):
    worker = await gateway.connect_worker(models=["m1"])
    api_client = gateway.new_api_client()
    body = {"messages": [{"role": "user", "content": "你好"}], "temperature": 0.2}
    envelope, opener = api_client.seal(worker.public, body=body)

    status, data = await gateway.post(api_client, envelope)

    assert status == 200
    assert data.keys() == {"request_id", "status", "sealed"}
    assert data["request_id"] == envelope.header.request_id
    assert data["status"] == "done"
    reply = opener.open(data["sealed"])
    assert reply == {"ok": True, "body": {"object": "chat.completion", "echo": body}}
    # worker 收到的是原封不動的信封，以及這把 API 金鑰登記的簽章公鑰。
    frame = worker.received[0]
    assert frame["envelope"] == envelope.to_json()
    assert b64u_decode(frame["client_sig_pub"]) == crypto.public_raw(api_client.signing_key)
    # 已完成的結果仍可輪詢取回。
    status, polled = await gateway.poll(api_client, envelope.header.request_id)
    assert (status, polled) == (200, data)


async def test_models_and_healthz(gateway):
    worker = await gateway.connect_worker(models=["m1", "m2"], name="家裡的電腦")
    api_client = gateway.new_api_client()

    resp = await gateway.client.get("/healthz")
    assert resp.status == 200 and await resp.json() == {"ok": True}

    resp = await gateway.client.get("/v1/models")
    assert resp.status == 401

    resp = await gateway.client.get("/v1/models", headers=api_client.headers)
    data = await resp.json()
    assert data["object"] == "list"
    public_key = b64u_encode(worker.public)
    assert data["data"] == [
        {"id": m, "object": "model", "owned_by": "家裡的電腦",
         "llmop": {"key_id": worker.key_id, "public_key": public_key}}
        for m in ("m1", "m2")
    ]


async def test_pending_then_poll(make_gateway):
    gw = await make_gateway(sync_wait_seconds=0.05)
    worker = await gw.connect_worker(silent=True)
    owner = gw.new_api_client()
    other = gw.new_api_client()
    envelope, opener = owner.seal(worker.public)
    request_id = envelope.header.request_id

    status, data = await gw.post(owner, envelope)
    assert (status, data) == (202, {"request_id": request_id, "status": "pending"})

    status, data = await gw.poll(owner, request_id)
    assert (status, data) == (202, {"request_id": request_id, "status": "pending"})

    await worker.wait_received(1)
    await worker.answer(worker.received[0])
    status, data = await gw.poll(owner, request_id)
    assert status == 200 and data["status"] == "done"
    assert opener.open(data["sealed"])["ok"] is True

    status, data = await gw.poll(other, request_id)
    assert_error(status, data, 404, "unknown_request", request_id)


async def test_waiters_wake_when_reply_arrives(make_gateway):
    gw = await make_gateway(sync_wait_seconds=10)
    worker = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, opener = api_client.seal(worker.public)
    post = asyncio.create_task(gw.post(api_client, envelope))
    await worker.wait_received(1)
    poll = asyncio.create_task(gw.poll(api_client, envelope.header.request_id))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await worker.answer(worker.received[0])
    (status1, data1), (status2, data2) = await asyncio.gather(post, poll)
    assert status1 == status2 == 200
    assert data1 == data2
    assert opener.open(data1["sealed"])["ok"] is True
    assert time.monotonic() - started < 5


async def test_poll_requires_api_key(gateway):
    resp = await gateway.client.get("/v1/requests/abc")
    assert resp.status == 401
    assert (await resp.json())["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# 九個步驟的拒絕
# ---------------------------------------------------------------------------


async def test_bad_json(gateway):
    api_client = gateway.new_api_client()
    resp = await gateway.client.post("/v1/chat/completions", data=b"{not json", headers=api_client.headers)
    assert_error(resp.status, await resp.json(), 400, "bad_envelope")


async def test_bad_envelope_shape_keeps_request_id_when_header_parses(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    obj = envelope.to_json() | {"extra": 1}
    resp = await gateway.client.post("/v1/chat/completions", json=obj, headers=api_client.headers)
    assert_error(resp.status, await resp.json(), 400, "bad_envelope", envelope.header.request_id)


async def test_unsupported_version(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    header = json.loads(b64u_decode(envelope.protected)) | {"v": 2}
    obj = envelope.to_json() | {"protected": b64u_encode(json.dumps(header).encode())}
    resp = await gateway.client.post("/v1/chat/completions", json=obj, headers=api_client.headers)
    assert_error(resp.status, await resp.json(), 400, "unsupported_version")


async def test_oversized_body(gateway):
    api_client = gateway.new_api_client()
    resp = await gateway.client.post(
        "/v1/chat/completions", data=b"x" * (MAX_BODY_BYTES + 1), headers=api_client.headers
    )
    assert_error(resp.status, await resp.json(), 413, "payload_too_large")


async def test_unknown_and_revoked_api_key(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    request_id = envelope.header.request_id

    for headers in ({}, {"Authorization": "Bearer llmop_sk_nope"}, {"Authorization": api_client.api_key}):
        resp = await gateway.client.post("/v1/chat/completions", json=envelope.to_json(), headers=headers)
        assert_error(resp.status, await resp.json(), 401, "unauthorized", request_id)

    gateway.store.revoke_api_key(api_client.key_id)
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 401, "unauthorized", request_id)


async def test_bad_json_is_checked_before_api_key(gateway):
    resp = await gateway.client.post("/v1/chat/completions", data=b"[]", headers={"Authorization": "Bearer x"})
    assert_error(resp.status, await resp.json(), 400, "bad_envelope")


async def test_wrong_sig_key_id(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    stranger = replace(api_client, signing_key=crypto.generate_signing_key())
    envelope, _ = stranger.seal(worker.public)
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 403, "bad_signature", envelope.header.request_id)


async def test_missing_signing_key_is_bad_signature(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    gateway.store.update_api_key(api_client.key_id, {"sig_public_key": None})
    envelope, _ = api_client.seal(worker.public)
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 403, "bad_signature", envelope.header.request_id)


async def test_bad_signature(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    sig = bytearray(b64u_decode(envelope.sig))
    sig[0] ^= 1
    tampered = replace(envelope, sig=b64u_encode(bytes(sig)))
    status, data = await gateway.post(api_client, tampered)
    assert_error(status, data, 403, "bad_signature", envelope.header.request_id)
    assert worker.received == []


async def test_signature_checked_before_timestamp(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    stranger = replace(api_client, signing_key=crypto.generate_signing_key())
    envelope, _ = stranger.seal(worker.public, now=int(time.time()) - 1000)
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 403, "bad_signature", envelope.header.request_id)


@pytest.mark.parametrize("offset", [-301, 301])
async def test_stale_timestamp(gateway, offset):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public, now=int(time.time()) + offset)
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 400, "stale_timestamp", envelope.header.request_id)


async def test_duplicate_request_id(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    status, _ = await gateway.post(api_client, envelope)
    assert status == 200
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 409, "duplicate_request", envelope.header.request_id)
    assert len(worker.received) == 1
    assert gateway.store.usage_for(api_client.key_id, utc_day(time.time()))["requests"] == 1


async def test_request_quota(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client(request_limit=1)
    first, _ = api_client.seal(worker.public)
    assert (await gateway.post(api_client, first))[0] == 200
    second, _ = api_client.seal(worker.public)
    status, data = await gateway.post(api_client, second)
    assert_error(status, data, 429, "quota_exceeded", second.header.request_id)


async def test_token_quota(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client(token_limit=40)
    too_big, _ = api_client.seal(worker.public, max_tokens=41)
    status, data = await gateway.post(api_client, too_big)
    assert_error(status, data, 429, "quota_exceeded", too_big.header.request_id)

    # 第一筆用掉 7 + 11 = 18 個 token；之後 18 + 22 = 40 還可以，18 + 23 就超過。
    first, _ = api_client.seal(worker.public, max_tokens=40)
    assert (await gateway.post(api_client, first))[0] == 200
    ok, _ = api_client.seal(worker.public, max_tokens=22)
    assert (await gateway.post(api_client, ok))[0] == 200
    over, _ = api_client.seal(worker.public, max_tokens=5)
    status, data = await gateway.post(api_client, over)
    assert_error(status, data, 429, "quota_exceeded", over.header.request_id)


async def test_quota_checked_before_routing(gateway):
    api_client = gateway.new_api_client(request_limit=0)
    envelope, _ = api_client.seal(crypto.public_raw(crypto.generate_worker_key()))
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 429, "quota_exceeded", envelope.header.request_id)


async def test_unknown_key_id(gateway):
    await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(crypto.public_raw(crypto.generate_worker_key()))
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 404, "unknown_key_id", envelope.header.request_id)
    # 被拒絕的請求不計入配額。
    assert gateway.store.usage_for(api_client.key_id, utc_day(time.time()))["requests"] == 0


async def test_model_not_served(gateway):
    worker = await gateway.connect_worker(models=["m1"])
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public, model="m2")
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 404, "model_unavailable", envelope.header.request_id)
    assert gateway.store.usage_for(api_client.key_id, utc_day(time.time()))["requests"] == 0


async def test_worker_busy(make_gateway):
    gw = await make_gateway(sync_wait_seconds=0.01)
    worker = await gw.connect_worker(max_concurrency=1, silent=True)
    api_client = gw.new_api_client()
    for _ in range(8):
        envelope, _ = api_client.seal(worker.public)
        assert (await gw.post(api_client, envelope))[0] == 202
    envelope, _ = api_client.seal(worker.public)
    status, data = await gw.post(api_client, envelope)
    assert_error(status, data, 503, "worker_busy", envelope.header.request_id)
    assert gw.store.usage_for(api_client.key_id, utc_day(time.time()))["requests"] == 8


async def test_gateway_busy(make_gateway):
    gw = await make_gateway(sync_wait_seconds=0.01, max_inflight=2)
    worker = await gw.connect_worker(max_concurrency=4, silent=True)
    api_client = gw.new_api_client()
    for _ in range(2):
        envelope, _ = api_client.seal(worker.public)
        assert (await gw.post(api_client, envelope))[0] == 202
    envelope, _ = api_client.seal(worker.public)
    status, data = await gw.post(api_client, envelope)
    assert_error(status, data, 503, "gateway_busy", envelope.header.request_id)

    # 一筆完成後就有空位。
    await worker.wait_received(2)
    await worker.answer(worker.received[0])
    status, _ = await gw.poll(api_client, worker.received[0]["request_id"])
    assert status == 200
    envelope, _ = api_client.seal(worker.public)
    assert (await gw.post(api_client, envelope))[0] == 202


# ---------------------------------------------------------------------------
# 配額計數
# ---------------------------------------------------------------------------


async def test_quota_counts_and_new_day(gateway, monkeypatch):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client(request_limit=2)
    day1 = 1_800_000_000  # 某天的 UTC 時間
    monkeypatch.setattr(api, "_now", lambda: day1)

    for expected in (1, 2):
        envelope, _ = api_client.seal(worker.public, now=day1)
        assert (await gateway.post(api_client, envelope))[0] == 200
        usage = gateway.store.usage_for(api_client.key_id, utc_day(day1))
        assert usage == {
            "requests": expected,
            "prompt_tokens": DEFAULT_USAGE[0] * expected,
            "completion_tokens": DEFAULT_USAGE[1] * expected,
        }
    envelope, _ = api_client.seal(worker.public, now=day1)
    assert (await gateway.post(api_client, envelope))[0] == 429
    assert gateway.store.usage_for(api_client.key_id, utc_day(day1))["requests"] == 2

    day2 = day1 + 86_400
    monkeypatch.setattr(api, "_now", lambda: day2)
    assert gateway.store.usage_for(api_client.key_id, utc_day(day2)) == {
        "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
    }
    envelope, _ = api_client.seal(worker.public, now=day2)
    assert (await gateway.post(api_client, envelope))[0] == 200
    assert gateway.store.usage_for(api_client.key_id, utc_day(day2))["requests"] == 1


async def test_sealed_error_reply_is_done_with_zero_usage(gateway):
    worker = await gateway.connect_worker(usage=(0, 0))
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    status, data = await gateway.post(api_client, envelope)
    assert status == 200
    assert gateway.store.usage_for(api_client.key_id, utc_day(time.time())) == {
        "requests": 1, "prompt_tokens": 0, "completion_tokens": 0,
    }


# ---------------------------------------------------------------------------
# worker 的錯誤、斷線與逾時
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status"),
    [("decrypt_failed", 400), ("replayed", 409), ("untrusted_client", 403), ("internal_error", 500),
     ("model_unavailable", 404)],
)
async def test_worker_error_frame(gateway, code, status):
    worker = await gateway.connect_worker(error_code=code)
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    got_status, data = await gateway.post(api_client, envelope)
    assert_error(got_status, data, status, code, envelope.header.request_id)
    # 錯誤回覆不帶 usage，token 數不變，但請求已經計數。
    assert gateway.store.usage_for(api_client.key_id, utc_day(time.time())) == {
        "requests": 1, "prompt_tokens": 0, "completion_tokens": 0,
    }


async def test_timeout(make_gateway):
    gw = await make_gateway(request_timeout_seconds=0.1, sync_wait_seconds=5)
    worker = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    status, data = await gw.post(api_client, envelope)
    assert_error(status, data, 504, "timeout", envelope.header.request_id)
    # 逾時後才到的回覆被忽略，結果維持 timeout。
    await worker.answer(worker.received[0])
    status, data = await gw.poll(api_client, envelope.header.request_id)
    assert_error(status, data, 504, "timeout", envelope.header.request_id)
    assert gw.hub.inflight == 0


async def test_worker_disconnect_fails_pending(make_gateway):
    gw = await make_gateway(sync_wait_seconds=0.01)
    worker = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    assert (await gw.post(api_client, envelope))[0] == 202
    await worker.close()
    status, data = await gw.poll(api_client, envelope.header.request_id)
    assert_error(status, data, 502, "worker_disconnected", envelope.header.request_id)
    assert gw.hub.workers == {}


async def test_results_are_purged_after_ttl(gateway, monkeypatch):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    assert (await gateway.post(api_client, envelope))[0] == 200
    assert gateway.hub.purge() == 0
    monkeypatch.setattr("llmop.gateway.hub.RESULT_TTL_SECONDS", -1)
    assert gateway.hub.purge() == 1
    status, data = await gateway.poll(api_client, envelope.header.request_id)
    assert_error(status, data, 404, "unknown_request", envelope.header.request_id)
