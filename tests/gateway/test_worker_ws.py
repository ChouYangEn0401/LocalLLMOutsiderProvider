"""worker WebSocket：註冊、hello、金鑰檢查、連線取代、模型更新。"""

from __future__ import annotations

import asyncio
import re

from aiohttp import WSMsgType

from llmop import crypto, protocol
from llmop.gateway import worker_ws
from llmop.protocol import b64u_encode

from .conftest import hello_frame


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
    assert gateway.hub.workers == {}
    again = await gateway.reconnect(worker)
    assert worker.worker_id in gateway.hub.workers
    await again.close()


async def test_reused_registration_token(gateway):
    _, reg_token = gateway.store.create_worker("w")
    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_json(protocol.register(reg_token))
    assert (await ws.receive_json(timeout=5))["type"] == "registered"
    await ws.close()

    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_json(protocol.register(reg_token))
    await expect_error(ws, "unauthorized")


async def test_expired_registration_token(gateway):
    worker, reg_token = gateway.store.create_worker("w")
    gateway.store.db.execute("UPDATE workers SET reg_expires_at = ? WHERE id = ?", (1, worker.id))
    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_json(protocol.register(reg_token))
    await expect_error(ws, "unauthorized")


async def test_reissued_registration_token_replaces_old(gateway):
    worker, old_token = gateway.store.create_worker("w")
    _, new_token = gateway.store.reissue_reg_token(worker.id)
    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_json(protocol.register(old_token))
    await expect_error(ws, "unauthorized")
    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_json(protocol.register(new_token))
    assert (await ws.receive_json(timeout=5))["worker_id"] == worker.id
    await ws.close()


async def test_wrong_worker_token(gateway):
    worker = await gateway.connect_worker()
    await worker.close()
    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_json(hello_frame(worker.worker_id, "llmop_wk_wrong", [worker.worker_key], ["m1"]))
    await expect_error(ws, "unauthorized")


async def test_revoked_worker(gateway):
    worker = await gateway.connect_worker()
    await worker.close()
    gateway.store.revoke_worker(worker.worker_id)
    ws = await gateway.client.ws_connect("/worker/ws")
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

    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_json({"type": "models", "models": []})
    await expect_error(ws, "bad_envelope")

    ws = await gateway.client.ws_connect("/worker/ws")
    await ws.send_str("not json")
    await expect_error(ws, "bad_envelope")


async def test_first_frame_deadline(gateway, monkeypatch):
    monkeypatch.setattr(worker_ws, "FIRST_FRAME_SECONDS", 0.05)
    ws = await gateway.client.ws_connect("/worker/ws")
    await expect_error(ws, "unauthorized")


async def test_second_connection_replaces_first(make_gateway):
    gw = await make_gateway(sync_wait_seconds=0.01)
    first = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, opener = api_client.seal(first.public)
    assert (await gw.post(api_client, envelope))[0] == 202

    second = await gw.reconnect(first)
    status, data = await gw.poll(api_client, envelope.header.request_id)
    assert (status, data["error"]["code"]) == (502, "worker_disconnected")
    assert gw.hub.workers[first.worker_id] is not None
    await first.task  # 舊連線被閘道關閉，假 worker 的讀取迴圈隨之結束
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
    for _ in range(100):
        if gateway.hub.workers[worker.worker_id].models == ["m2"]:
            break
        await asyncio.sleep(0.01)
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
    # 連線仍然可用。
    api_client = gateway.new_api_client()
    envelope, opener = api_client.seal(worker.public)
    status, data = await gateway.post(api_client, envelope)
    assert status == 200
    assert "忽略不在等待中的回覆" in caplog.text
