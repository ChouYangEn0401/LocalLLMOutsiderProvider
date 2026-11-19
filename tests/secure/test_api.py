"""加密面的用戶端 API：正常流程、202 輪詢、驗證步驟的拒絕順序、准入（T-2）、排隊（T-3）、
用量紀錄、worker 錯誤與逾時。由 B 的 tests/gateway/test_api.py 移植（對照表見各測試的說明）。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from llmop import crypto
from llmop.protocol import MAX_BODY_BYTES, b64u_decode, b64u_encode
from llmop.secure import api

from .conftest import DEFAULT_USAGE, wait_until


def assert_error(status: int, body: dict, expected_status: int, code: str, request_id: str | None = None) -> None:
    assert (status, body["status"], body["error"]["code"]) == (expected_status, "error", code), body
    assert body["error"]["message"]
    if request_id is None:
        assert "request_id" not in body
    else:
        assert body["request_id"] == request_id


def requests_counted(gw, api_client) -> int:
    return len(gw.usage_events(api_client.key_id))


# ---------------------------------------------------------------------------
# 正常流程
# ---------------------------------------------------------------------------


async def test_happy_path(gateway):
    worker = await gateway.connect_worker(models=["m1"], name="家裡的電腦")
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
    # worker 回報的用量寫進 usage_events（usage_source="worker"）。
    [event] = gateway.usage_events(api_client.key_id)
    assert event["username"] == "alice"
    assert event["usage_source"] == "worker"
    assert event["status"] == "ok"
    assert event["backend"] == "家裡的電腦"
    assert event["model"] == "m1"
    assert (event["prompt_tokens"], event["completion_tokens"], event["total_tokens"]) == (7, 11, 18)
    # 額度租約已釋放，排程器的名額也還回去了。
    assert gateway.state.quota._inflight.get("alice", 0) == 0
    await wait_until(lambda: gateway.state.scheduler.running == 0)


async def test_models(gateway):
    worker = await gateway.connect_worker(models=["m1", "m2"], name="家裡的電腦")
    api_client = gateway.new_api_client()

    status, data = await gateway.get("/e2e/v1/models")
    assert status == 401 and data["error"]["code"] == "unauthorized"

    status, data = await gateway.get("/e2e/v1/models", api_client.headers)
    assert status == 200
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
    owner = gw.new_api_client("alice")
    other = gw.new_api_client("bob")
    envelope, opener = owner.seal(worker.public)
    request_id = envelope.header.request_id

    status, data = await gw.post(owner, envelope)
    # 已經轉送給 worker：排隊位置 0。
    assert (status, data) == (202, {"request_id": request_id, "status": "pending",
                                    "queue": {"position": 0, "eta": 0}})

    status, data = await gw.poll(owner, request_id)
    assert (status, data["status"], data["queue"]["position"]) == (202, "pending", 0)

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
    status, data = await gateway.get("/e2e/v1/requests/abc")
    assert status == 401
    assert data["error"]["code"] == "unauthorized"


async def test_guest_key_without_account(gateway):
    """GUEST key（沒有帳號）也能用加密面；用量記在那把 key 上。"""
    worker = await gateway.connect_worker()
    guest = gateway.new_api_client(guest=True)
    envelope, opener = guest.seal(worker.public)
    status, data = await gateway.post(guest, envelope)
    assert status == 200 and opener.open(data["sealed"])["ok"] is True
    [event] = gateway.usage_events(guest.key_id)
    assert (event["username"], event["usage_source"], event["total_tokens"]) == ("", "worker", 18)
    assert gateway.state.usage.status(None, gateway.store.get_api_key(guest.key_id))  # 查得到


# ---------------------------------------------------------------------------
# 驗證步驟的拒絕
# ---------------------------------------------------------------------------


async def test_bad_json(gateway):
    api_client = gateway.new_api_client()
    status, data, _ = await gateway.post_raw(data=b"{not json", headers=api_client.headers)
    assert_error(status, data, 400, "bad_envelope")


async def test_bad_envelope_shape_keeps_request_id_when_header_parses(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    obj = envelope.to_json() | {"extra": 1}
    status, data, _ = await gateway.post_raw(json_body=obj, headers=api_client.headers)
    assert_error(status, data, 400, "bad_envelope", envelope.header.request_id)


async def test_unsupported_version(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    header = json.loads(b64u_decode(envelope.protected)) | {"v": 2}
    obj = envelope.to_json() | {"protected": b64u_encode(json.dumps(header).encode())}
    status, data, _ = await gateway.post_raw(json_body=obj, headers=api_client.headers)
    assert_error(status, data, 400, "unsupported_version")


async def test_oversized_body(gateway):
    api_client = gateway.new_api_client()
    status, data, _ = await gateway.post_raw(data=b"x" * (MAX_BODY_BYTES + 1), headers=api_client.headers)
    assert_error(status, data, 413, "payload_too_large")


async def test_unknown_revoked_expired_api_key_and_disabled_account(make_gateway):
    gw = await make_gateway()
    worker = await gw.connect_worker()
    api_client = gw.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    request_id = envelope.header.request_id

    for headers in ({}, {"Authorization": "Bearer sk-llop-nope"}, {"Authorization": api_client.api_key}):
        status, data, _ = await gw.post_raw(json_body=envelope.to_json(), headers=headers)
        assert_error(status, data, 401, "unauthorized", request_id)

    gw.store.revoke_api_key(api_client.key_id)
    status, data = await gw.post(api_client, envelope)
    assert_error(status, data, 401, "unauthorized", request_id)

    expired = gw.new_api_client(expires_at=time.time() - 1)
    envelope, _ = expired.seal(worker.public)
    status, data = await gw.post(expired, envelope)
    assert_error(status, data, 401, "unauthorized", envelope.header.request_id)

    gw2 = await make_gateway(disabled=True)
    worker2 = await gw2.connect_worker()
    disabled = gw2.new_api_client("alice")
    envelope, _ = disabled.seal(worker2.public)
    status, data = await gw2.post(disabled, envelope)
    assert_error(status, data, 401, "unauthorized", envelope.header.request_id)
    assert worker.received == [] and worker2.received == []


async def test_bad_json_is_checked_before_api_key(gateway):
    status, data, _ = await gateway.post_raw(data=b"[]", headers={"Authorization": "Bearer x"})
    assert_error(status, data, 400, "bad_envelope")


async def test_wrong_sig_key_id(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    stranger = replace(api_client, signing_key=crypto.generate_signing_key())
    envelope, _ = stranger.seal(worker.public)
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 403, "bad_signature", envelope.header.request_id)


async def test_key_without_signing_key_is_rejected(gateway):
    """沒有 sig_public_key 的 key 一律拒絕（建立時沒給、或之後被清掉）。"""
    worker = await gateway.connect_worker()
    plain = gateway.new_api_client(signing=False)
    envelope, _ = plain.seal(worker.public)
    status, data = await gateway.post(plain, envelope)
    assert_error(status, data, 403, "bad_signature", envelope.header.request_id)

    api_client = gateway.new_api_client()
    gateway.store._exec("UPDATE api_keys SET sig_public_key = NULL WHERE id = ?", (api_client.key_id,))
    envelope, _ = api_client.seal(worker.public)
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 403, "bad_signature", envelope.header.request_id)
    assert worker.received == []
    assert gateway.usage_events() == []


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
    assert requests_counted(gateway, api_client) == 1


# ---------------------------------------------------------------------------
# 准入（T-2：帳號額度與兩個用量表）
# ---------------------------------------------------------------------------


async def test_request_quota(make_gateway):
    gw = await make_gateway(quota={"requests_per_day": 1})
    worker = await gw.connect_worker()
    api_client = gw.new_api_client()
    first, _ = api_client.seal(worker.public)
    assert (await gw.post(api_client, first))[0] == 200
    second, _ = api_client.seal(worker.public)
    status, data = await gw.post(api_client, second)
    assert_error(status, data, 429, "quota_exceeded", second.header.request_id)
    assert len(worker.received) == 1


async def test_token_quota_is_charged_after_the_fact(make_gateway):
    """D5：事後記帳，不預扣 max_tokens。用完之後的下一筆才被擋。"""
    gw = await make_gateway(quota={"tokens_per_day": 20})
    worker = await gw.connect_worker()
    api_client = gw.new_api_client()
    # 7 + 11 = 18 < 20：第二筆還可以（即使 max_tokens 很大）；之後 36 ≥ 20 就被擋。
    for _ in range(2):
        envelope, _ = api_client.seal(worker.public, max_tokens=1000)
        assert (await gw.post(api_client, envelope))[0] == 200
    over, _ = api_client.seal(worker.public, max_tokens=1)
    status, data = await gw.post(api_client, over)
    assert_error(status, data, 429, "quota_exceeded", over.header.request_id)


async def test_quota_checked_before_routing(make_gateway):
    """額度用完的 GUEST key：即使沒有 worker 持有這個 key_id，也先回額度錯誤（401，key 已失效）。"""
    gw = await make_gateway()
    guest = gw.new_api_client(guest=True, budget_tokens=10)
    key = gw.store.get_api_key(guest.key_id)
    gw.state.usage.charge(10, None, key)
    envelope, _ = guest.seal(crypto.public_raw(crypto.generate_worker_key()))
    status, data = await gw.post(guest, envelope)
    assert_error(status, data, 401, "unauthorized", envelope.header.request_id)
    assert "exhausted" in data["error"]["message"]


async def test_usage_bucket_empty_gives_retry_after(make_gateway):
    """GUEST 的 1.5 小時桶見底：429 並附 Retry-After（TIERS 3）。"""
    gw = await make_gateway()
    worker = await gw.connect_worker()
    guest = gw.new_api_client(guest=True, budget_tokens=10_000_000)
    key = gw.store.get_api_key(guest.key_id)
    gw.state.usage.charge(30_000, None, key)  # 桶 20,000 → 欠到下限
    envelope, _ = guest.seal(worker.public)
    async with gw.http.post(gw.base + "/e2e/v1/chat/completions", json=envelope.to_json(),
                            headers=guest.headers) as resp:
        status, data, retry = resp.status, await resp.json(), resp.headers.get("Retry-After")
    assert_error(status, data, 429, "quota_exceeded", envelope.header.request_id)
    assert retry is not None and int(retry) > 0
    assert worker.received == []


async def test_worker_usage_is_charged_to_usage_tables(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    account = gateway.config.account("alice")
    before = gateway.state.usage.status(account)
    envelope, _ = api_client.seal(worker.public)
    assert (await gateway.post(api_client, envelope))[0] == 200
    after = gateway.state.usage.status(account)
    assert before != after
    row = gateway.store.get_bucket("acct:alice")
    assert row["period_used"] == sum(DEFAULT_USAGE)


async def test_unknown_key_id(gateway):
    await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(crypto.public_raw(crypto.generate_worker_key()))
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 404, "unknown_key_id", envelope.header.request_id)
    # 被拒絕的請求不記用量，租約也已釋放。
    assert requests_counted(gateway, api_client) == 0
    assert gateway.state.quota._inflight.get("alice", 0) == 0


async def test_model_not_served(gateway):
    worker = await gateway.connect_worker(models=["m1"])
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public, model="m2")
    status, data = await gateway.post(api_client, envelope)
    assert_error(status, data, 404, "model_unavailable", envelope.header.request_id)
    assert requests_counted(gateway, api_client) == 0


# ---------------------------------------------------------------------------
# 排隊（T-3 的排程器，取代 B 的 8 × max_concurrency 與 max_inflight）
# ---------------------------------------------------------------------------


async def test_account_queue_limit(make_gateway):
    """B 的 worker_busy → 每帳號排隊上限：429 queue_wait＋Retry-After，不記用量。"""
    gw = await make_gateway(sync_wait_seconds=0.01, scheduler={"max_per_account": 2})
    worker = await gw.connect_worker(max_concurrency=1, silent=True)
    api_client = gw.new_api_client()
    # NORMAL 同時 1 筆：第 1 筆在跑，第 2、3 筆排隊。
    for _ in range(3):
        envelope, _ = api_client.seal(worker.public)
        assert (await gw.post(api_client, envelope))[0] == 202
    envelope, _ = api_client.seal(worker.public)
    status, data, headers = await gw.post_raw(json_body=envelope.to_json(), headers=api_client.headers)
    assert_error(status, data, 429, "queue_wait", envelope.header.request_id)
    assert int(headers["Retry-After"]) >= 1
    assert len(worker.received) == 1
    # 被拒的那筆沒有留下來：可以用同一個 request_id 之後再送。
    assert gw.hub.get(api_client.key_id, envelope.header.request_id) is None


async def test_queue_full_then_recovers(make_gateway):
    """B 的 gateway_busy → 排隊總長已滿：503 gateway_busy；一筆完成後就有空位。"""
    gw = await make_gateway(sync_wait_seconds=0.01, tier="vip3",
                            scheduler={"slots": 1, "max_length": 1, "max_per_account": 10})
    worker = await gw.connect_worker(max_concurrency=4, silent=True)
    api_client = gw.new_api_client()
    first, _ = api_client.seal(worker.public)
    assert (await gw.post(api_client, first))[0] == 202
    second, _ = api_client.seal(worker.public)
    status, data = await gw.post(api_client, second)
    # 第二筆在排隊：pending 帶著位置。
    assert status == 202 and data["queue"]["position"] == 1 and data["queue"]["eta"] >= 0
    third, _ = api_client.seal(worker.public)
    status, data, headers = await gw.post_raw(json_body=third.to_json(), headers=api_client.headers)
    assert_error(status, data, 503, "gateway_busy", third.header.request_id)
    assert "Retry-After" in headers

    await worker.wait_received(1)
    await worker.answer(worker.received[0])
    status, _ = await gw.poll(api_client, first.header.request_id)
    assert status == 200
    await worker.wait_received(2)  # 排隊中的第二筆輪到了
    assert worker.received[1]["request_id"] == second.header.request_id
    fourth, _ = api_client.seal(worker.public)
    assert (await gw.post(api_client, fourth))[0] == 202


async def test_queue_wait_timeout(make_gateway):
    gw = await make_gateway(sync_wait_seconds=5, tier="vip3",
                            scheduler={"slots": 1, "max_wait_seconds": {"public": 0.1}})
    worker = await gw.connect_worker(silent=True)
    alice = gw.new_api_client("alice")
    first, _ = alice.seal(worker.public)
    task = asyncio.create_task(gw.post(alice, first))
    await worker.wait_received(1)
    second, _ = alice.seal(worker.public)
    status, data, headers = await gw.post_raw(json_body=second.to_json(), headers=alice.headers)
    assert_error(status, data, 429, "queue_wait", second.header.request_id)
    assert "1" in data["error"]["message"] and int(headers["Retry-After"]) >= 1
    # 結果仍可輪詢；沒有轉送給 worker，不記用量。
    status, data = await gw.poll(alice, second.header.request_id)
    assert_error(status, data, 429, "queue_wait", second.header.request_id)
    assert len(worker.received) == 1
    await worker.answer(worker.received[0])
    assert (await task)[0] == 200
    assert requests_counted(gw, alice) == 1


async def test_low_tier_queues_behind_others(make_gateway):
    """用量表判定為 LOW 的帳號用 low 順位排隊（准入結果傳給排程器）。"""
    gw = await make_gateway(sync_wait_seconds=0.05, scheduler={"slots": 1})
    worker = await gw.connect_worker(silent=True)
    alice, bob = gw.new_api_client("alice"), gw.new_api_client("bob")
    gw.state.usage.charge(80_000, gw.config.account("alice"))  # alice 的桶見底 → LOW
    blocker, _ = bob.seal(worker.public)
    assert (await gw.post(bob, blocker))[0] == 202
    low, _ = alice.seal(worker.public)
    assert (await gw.post(alice, low))[0] == 202
    normal, _ = bob.seal(worker.public)
    assert (await gw.post(bob, normal))[0] == 202
    status, data = await gw.poll(alice, low.header.request_id)
    assert data["queue"]["position"] == 2  # bob 後到但排在前面
    tickets = {j.request_id: j.ticket.tier for j in gw.hub.jobs.values()}
    assert tickets[low.header.request_id] == "low"


# ---------------------------------------------------------------------------
# 用量計數
# ---------------------------------------------------------------------------


async def test_quota_counts_and_new_day(make_gateway, monkeypatch):
    gw = await make_gateway(quota={"requests_per_day": 2})
    worker = await gw.connect_worker()
    api_client = gw.new_api_client()
    day1 = 1_800_000_000
    monkeypatch.setattr(api, "_now", lambda: day1)

    for expected in (1, 2):
        envelope, _ = api_client.seal(worker.public, now=day1)
        assert (await gw.post(api_client, envelope))[0] == 200
        events = gw.usage_events(api_client.key_id)
        assert len(events) == expected
        assert sum(e["prompt_tokens"] for e in events) == DEFAULT_USAGE[0] * expected
        assert sum(e["completion_tokens"] for e in events) == DEFAULT_USAGE[1] * expected
        assert all(e["ts"] == day1 for e in events)
    envelope, _ = api_client.seal(worker.public, now=day1)
    assert (await gw.post(api_client, envelope))[0] == 429
    assert len(gw.usage_events(api_client.key_id)) == 2

    day2 = day1 + 86_400
    monkeypatch.setattr(api, "_now", lambda: day2)
    envelope, _ = api_client.seal(worker.public, now=day2)
    assert (await gw.post(api_client, envelope))[0] == 200
    assert len(gw.usage_events(api_client.key_id)) == 3


async def test_sealed_error_reply_is_done_with_zero_usage(make_gateway):
    gw = await make_gateway()
    worker = await gw.connect_worker(usage=(0, 0))
    api_client = gw.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    status, data = await gw.post(api_client, envelope)
    assert status == 200
    [event] = gw.usage_events(api_client.key_id)
    assert (event["status"], event["total_tokens"], event["usage_source"]) == ("ok", 0, "worker")


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
    # 錯誤回覆不帶 usage：記一筆 0 token 的錯誤。
    [event] = gateway.usage_events(api_client.key_id)
    assert (event["status"], event["error"], event["total_tokens"], event["usage_source"]) == (
        "error", code, 0, "none")


async def test_timeout(make_gateway):
    gw = await make_gateway(request_timeout_seconds=0.2, sync_wait_seconds=5)
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
    await wait_until(lambda: gw.state.scheduler.running == 0)


async def test_worker_disconnect_fails_pending(make_gateway):
    gw = await make_gateway(sync_wait_seconds=0.01)
    worker = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    assert (await gw.post(api_client, envelope))[0] == 202
    await worker.wait_received(1)
    await worker.close()
    status, data = await gw.poll(api_client, envelope.header.request_id)
    assert_error(status, data, 502, "worker_disconnected", envelope.header.request_id)
    await wait_until(lambda: gw.hub.workers == {})
    await wait_until(lambda: gw.state.scheduler.running == 0)


async def test_results_are_purged_after_ttl(gateway, monkeypatch):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    assert (await gateway.post(api_client, envelope))[0] == 200
    assert gateway.hub.purge() == 0
    monkeypatch.setattr("llmop.secure.hub.RESULT_TTL_SECONDS", -1)
    assert gateway.hub.purge() == 1
    status, data = await gateway.poll(api_client, envelope.header.request_id)
    assert_error(status, data, 404, "unknown_request", envelope.header.request_id)
