"""worker WebSocket（FastAPI 版）：註冊、hello、金鑰檢查、連線取代、模型更新、握手限流。
由 B 的 tests/gateway/test_worker_ws.py 移植。"""

from __future__ import annotations

import asyncio
import re
import threading

import aiohttp
from aiohttp import WSMsgType
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llmop import crypto, protocol
from llmop.protocol import Envelope, b64u_decode, b64u_encode
from llmop.secure import build_secure_router, hub as secure_hub, worker_ws

from .conftest import hello_frame, wait_until


async def expect_error(ws, code: str) -> None:
    """握手失敗：先收到 error 訊框，然後連線被關閉。"""
    assert await ws.receive_json(timeout=5) == {"type": "error", "code": code}
    msg = await ws.receive(timeout=5)
    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)


async def test_registration_then_hello(gateway):
    ws, worker_id, worker_token = await gateway.register("家裡的電腦")
    assert re.fullmatch(r"w_[0-9a-f]{12}", worker_id)
    assert worker_token.startswith("llmop_wk_")
    old_key, new_key = crypto.generate_worker_key(), crypto.generate_worker_key()
    await ws.send_json(hello_frame(worker_id, worker_token, [new_key, old_key], ["m1", "m2"], 3))
    assert await ws.receive_json(timeout=5) == {"type": "ready"}

    conn = gateway.hub.workers[worker_id]
    assert conn.name == "家裡的電腦"
    assert list(conn.keys) == [crypto.worker_key_id(crypto.public_raw(k)) for k in (new_key, old_key)]
    assert conn.models == ["m1", "m2"] and conn.max_concurrency == 3
    keys = gateway.store.worker_keys(worker_id)
    assert {k["public_key"] for k in keys} == {b64u_encode(crypto.public_raw(k)) for k in (new_key, old_key)}
    assert all(isinstance(k["first_seen_at"], int) for k in keys)
    assert gateway.store.worker_models(worker_id) == ["m1", "m2"]
    assert gateway.store.get_worker(worker_id).last_seen_at is not None
    await ws.close()


async def test_reconnect_with_saved_token(gateway):
    worker = await gateway.connect_worker()
    await worker.close()
    await wait_until(lambda: gateway.hub.workers == {})
    again = await gateway.reconnect(worker)
    assert worker.worker_id in gateway.hub.workers
    await again.close()


async def test_reused_registration_token(gateway):
    _, reg_token = gateway.store.create_worker("w")
    ws = await gateway.ws_connect()
    await ws.send_json(protocol.register(reg_token))
    assert (await ws.receive_json(timeout=5))["type"] == "registered"
    await ws.close()

    ws = await gateway.ws_connect()
    await ws.send_json(protocol.register(reg_token))
    await expect_error(ws, "unauthorized")


async def test_expired_registration_token(gateway):
    worker, reg_token = gateway.store.create_worker("w")
    gateway.store._exec("UPDATE workers SET reg_expires_at = ? WHERE id = ?", (1, worker.id))
    ws = await gateway.ws_connect()
    await ws.send_json(protocol.register(reg_token))
    await expect_error(ws, "unauthorized")


async def test_reissued_registration_token_replaces_old(gateway):
    worker, old_token = gateway.store.create_worker("w")
    _, new_token = gateway.store.reissue_reg_token(worker.id)
    ws = await gateway.ws_connect()
    await ws.send_json(protocol.register(old_token))
    await expect_error(ws, "unauthorized")
    ws = await gateway.ws_connect()
    await ws.send_json(protocol.register(new_token))
    assert (await ws.receive_json(timeout=5))["worker_id"] == worker.id
    await ws.close()


async def test_wrong_worker_token(gateway):
    worker = await gateway.connect_worker()
    await worker.close()
    ws = await gateway.ws_connect()
    await ws.send_json(hello_frame(worker.worker_id, "llmop_wk_wrong", [worker.worker_key], ["m1"]))
    await expect_error(ws, "unauthorized")


async def test_revoked_worker(gateway):
    worker = await gateway.connect_worker()
    await worker.close()
    gateway.store.revoke_worker(worker.worker_id)
    ws = await gateway.ws_connect()
    await ws.send_json(hello_frame(worker.worker_id, worker.worker_token, [worker.worker_key], ["m1"]))
    await expect_error(ws, "unauthorized")


async def test_key_id_must_match_public_key(gateway):
    ws, worker_id, worker_token = await gateway.register()
    frame = hello_frame(worker_id, worker_token, [crypto.generate_worker_key()], ["m1"])
    other = crypto.generate_worker_key()
    frame["keys"][0]["key_id"] = crypto.worker_key_id(crypto.public_raw(other))
    await ws.send_json(frame)
    await expect_error(ws, "bad_envelope")
    assert gateway.hub.workers == {}


async def test_key_conflict(gateway):
    first = await gateway.connect_worker()
    ws, worker_id, worker_token = await gateway.register("second")
    await ws.send_json(hello_frame(worker_id, worker_token, [crypto.generate_worker_key(), first.worker_key], ["m1"]))
    await expect_error(ws, "key_conflict")
    assert worker_id not in gateway.hub.workers
    # 整批拒絕：連沒有衝突的那把金鑰也不記錄。
    assert gateway.store.worker_keys(worker_id) == []


async def test_unsupported_version_and_bad_first_frame(gateway):
    ws, worker_id, worker_token = await gateway.register()
    frame = hello_frame(worker_id, worker_token, [crypto.generate_worker_key()], ["m1"]) | {"v": 2}
    await ws.send_json(frame)
    await expect_error(ws, "unsupported_version")

    ws = await gateway.ws_connect()
    await ws.send_json({"type": "models", "models": []})
    await expect_error(ws, "bad_envelope")

    ws = await gateway.ws_connect()
    await ws.send_str("not json")
    await expect_error(ws, "bad_envelope")

    ws = await gateway.ws_connect()
    await ws.send_bytes(b"\x00binary")
    await expect_error(ws, "bad_envelope")


async def test_first_frame_deadline(gateway, monkeypatch):
    monkeypatch.setattr(worker_ws, "FIRST_FRAME_SECONDS", 0.05)
    ws = await gateway.ws_connect()
    await expect_error(ws, "unauthorized")


async def test_second_connection_replaces_first(make_gateway):
    gw = await make_gateway(sync_wait_seconds=0.01)
    first = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, opener = api_client.seal(first.public)
    assert (await gw.post(api_client, envelope))[0] == 202
    await first.wait_received(1)

    second = await gw.reconnect(first)
    status, data = await gw.poll(api_client, envelope.header.request_id)
    assert (status, data["error"]["code"]) == (502, "worker_disconnected")
    assert gw.hub.workers[first.worker_id] is not None
    await asyncio.wait_for(first.task, 5)  # 舊連線被閘道關閉，假 worker 的讀取迴圈隨之結束
    assert first.ws.closed

    # 新的請求改由新連線處理。
    envelope, _ = api_client.seal(first.public)
    assert (await gw.post(api_client, envelope))[0] in (200, 202)
    await second.wait_received(1)
    assert second.received[0]["request_id"] == envelope.header.request_id


async def test_models_frame_updates_routing(gateway):
    worker = await gateway.connect_worker(models=["m1"])
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public, model="m2")
    status, data = await gateway.post(api_client, envelope)
    assert (status, data["error"]["code"]) == (404, "model_unavailable")

    await worker.ws.send_json(protocol.models(["m2"]))
    await wait_until(lambda: gateway.hub.workers[worker.worker_id].models == ["m2"])
    assert gateway.store.worker_models(worker.worker_id) == ["m2"]

    envelope, opener = api_client.seal(worker.public, model="m2")
    status, data = await gateway.post(api_client, envelope)
    assert status == 200 and opener.open(data["sealed"])["ok"] is True
    envelope, _ = api_client.seal(worker.public, model="m1")
    status, data = await gateway.post(api_client, envelope)
    assert (status, data["error"]["code"]) == (404, "model_unavailable")


async def test_reply_for_unknown_request_is_ignored(gateway, caplog):
    worker = await gateway.connect_worker()
    sealed = b64u_encode(bytes(64))
    await worker.ws.send_json(protocol.response_ok(protocol.new_request_id(), sealed, 1, 1))
    await worker.ws.send_json({"type": "nonsense"})
    await worker.ws.send_bytes(b"binary")
    # 連線仍然可用。
    api_client = gateway.new_api_client()
    envelope, opener = api_client.seal(worker.public)
    status, data = await gateway.post(api_client, envelope)
    assert status == 200
    assert "忽略不在等待中的回覆" in caplog.text


# ---------------------------------------------------------------------------
# 握手失敗限流（MERGE_PLAN 6.1 第 7 項）
# ---------------------------------------------------------------------------


async def test_handshake_failures_are_rate_limited(gateway, monkeypatch):
    monkeypatch.setattr(secure_hub, "HANDSHAKE_FAILURE_LIMIT", 3)
    for _ in range(3):
        ws = await gateway.ws_connect()
        await ws.send_json(protocol.register("llmop_reg_guess"))
        await expect_error(ws, "unauthorized")

    # 第 4 次：在 WebSocket 握手階段就被拒絕（HTTP 403），連正確的權杖也一樣。
    _, reg_token = gateway.store.create_worker("legit")
    try:
        await gateway.ws_connect()
    except aiohttp.WSServerHandshakeError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("被限流的來源仍然可以連線")

    # 失敗滑出時間窗之後恢復。
    monkeypatch.setattr(secure_hub, "HANDSHAKE_WINDOW_SECONDS", 0.0)
    ws = await gateway.ws_connect()
    await ws.send_json(protocol.register(reg_token))
    assert (await ws.receive_json(timeout=5))["type"] == "registered"
    await ws.close()


async def test_successful_handshakes_do_not_count(gateway, monkeypatch):
    monkeypatch.setattr(secure_hub, "HANDSHAKE_FAILURE_LIMIT", 2)
    for _ in range(4):
        worker = await gateway.connect_worker()
        await worker.close()
    assert not gateway.hub.throttle.blocked("127.0.0.1")


def test_throttle_tracks_sources_separately(monkeypatch):
    monkeypatch.setattr(secure_hub, "HANDSHAKE_FAILURE_LIMIT", 2)
    now = [1000.0]
    throttle = secure_hub.HandshakeThrottle(clock=lambda: now[0])
    throttle.failed("203.0.113.1")
    throttle.failed("203.0.113.1")
    assert throttle.blocked("203.0.113.1")
    assert not throttle.blocked("203.0.113.2")
    now[0] += secure_hub.HANDSHAKE_WINDOW_SECONDS + 1
    assert not throttle.blocked("203.0.113.1")


# ---------------------------------------------------------------------------
# 用 FastAPI TestClient 的 websocket 當假 worker 跑通一筆（DISPATCH T-5 驗收）
# ---------------------------------------------------------------------------


def test_one_request_through_testclient_websocket(tmp_path):
    from types import SimpleNamespace

    from llmop.quota import QuotaManager
    from llmop.scheduler import Scheduler
    from llmop.store import Store
    from llmop.usage import UsageManager

    from .conftest import ApiClient, make_config

    cfg = make_config()
    store = Store(tmp_path / "llmop.sqlite3")
    usage = UsageManager(store, cfg)
    state = SimpleNamespace(store=store, config=cfg, usage=usage,
                            quota=QuotaManager(store, tables=usage), scheduler=Scheduler())
    app = FastAPI()
    app.include_router(build_secure_router(state, sync_wait_seconds=5))

    signing_key = crypto.generate_signing_key()
    secret, key = store.create_api_key("alice", sig_public_key=b64u_encode(crypto.public_raw(signing_key)))
    api_client = ApiClient(api_key=secret, key_id=key.id, signing_key=signing_key)
    worker_key = crypto.generate_worker_key()
    _, reg_token = store.create_worker("tc-worker")

    with TestClient(app) as client, client.websocket_connect("/worker/ws") as ws:
        ws.send_json(protocol.register(reg_token))
        registered = ws.receive_json()
        ws.send_json(hello_frame(registered["worker_id"], registered["worker_token"], [worker_key], ["m1"]))
        assert ws.receive_json() == {"type": "ready"}

        def serve_one() -> None:
            frame = ws.receive_json()
            envelope = Envelope.from_json(frame["envelope"])
            crypto.verify_request(envelope, b64u_decode(frame["client_sig_pub"]))
            body, sealer = crypto.open_request(envelope, worker_key)
            sealed = sealer.seal({"ok": True, "body": {"echo": body}})
            ws.send_json(protocol.response_ok(frame["request_id"], sealed, 3, 4))

        worker_thread = threading.Thread(target=serve_one, daemon=True)
        worker_thread.start()
        envelope, opener = api_client.seal(crypto.public_raw(worker_key), body={"messages": []})
        resp = client.post("/e2e/v1/chat/completions", json=envelope.to_json(), headers=api_client.headers)
        worker_thread.join(5)
        assert resp.status_code == 200, resp.text
        assert opener.open(resp.json()["sealed"]) == {"ok": True, "body": {"echo": {"messages": []}}}

        resp = client.get("/e2e/v1/models", headers=api_client.headers)
        assert [m["id"] for m in resp.json()["data"]] == ["m1"]

    rows = store._all("SELECT usage_source, total_tokens FROM usage_events")
    assert [tuple(r) for r in rows] == [("worker", 7)]
    store.close()
