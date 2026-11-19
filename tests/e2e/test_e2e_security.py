"""端對端：閘道看不到內容、簽章錯、釘選不符、重放、金鑰輪替、worker 釘選用戶端、請求途中 worker 被殺掉。

閘道是真的統一節點（public-api 入口，/<gate>/e2e/v1 與 /<gate>/worker/ws）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from llmop import crypto, paths
from llmop.protocol import b64u_decode, b64u_encode
from llmop.worker import cli as worker_cli

from .harness import (
    DEFAULT_REPLY,
    MODELS,
    FakeGateway,
    Stack,
    parse_key_blocks,
    wait_until,
)


def _seal(stack: Stack, *, prompt: str = "新的請求", worker_public: str | None = None, signing_key=None):
    return crypto.seal_request(
        {"messages": [{"role": "user", "content": prompt}]}, model=MODELS["ollama"], max_tokens=16,
        worker_public=b64u_decode(worker_public or stack.worker_keys[0]["public_key"]),
        signing_key=signing_key or stack.client.signing_key,
    )


# ---------------------------------------------------------------------------
# 閘道看不到內容
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["ollama", "openai"])
async def test_gateway_never_sees_plaintext(make_stack, caplog, kind):
    caplog.set_level(logging.DEBUG)
    prompt_marker = f"E2E-PROMPT-{uuid.uuid4().hex}"
    reply_marker = f"E2E-REPLY-{uuid.uuid4().hex}"
    stack = await make_stack()
    backend = stack.backends[kind]
    backend.reply = f"答案在這裡：{reply_marker}"

    status, data, _ = await stack.chat(MODELS[kind], f"請記住這個代號 {prompt_marker}")

    # 對照組：內容確實走完了全程（後端看到提示，用戶端拿到回答）。
    assert status == 200
    assert reply_marker in data["choices"][0]["message"]["content"]
    assert prompt_marker in json.dumps(backend.calls[-1], ensure_ascii=False)

    # 對照組：錄音是完整、而且解得開的。閘道從用戶端收到的信封，就是它轉給 worker 的那一個；
    # 閘道從 worker 收到的封裝回覆，就是它交給用戶端的那一個。四個方向都看得到。
    (envelope,) = stack.relay.posted_envelopes()
    from_worker, to_worker = stack.relay.ws_frames()
    (request_frame,) = [f for f in to_worker if f["type"] == "request"]
    assert request_frame["envelope"] == envelope.to_json()
    (response_frame,) = [f for f in from_worker if f["type"] == "response"]
    assert response_frame["ok"] is True
    final = [json.loads(r.body) for _, r in stack.relay.http_exchanges() if r is not None and r.status == 200
             and r.body.startswith(b'{"request_id"')]
    assert final == [{"request_id": envelope.header.request_id, "status": "done", "sealed": response_frame["sealed"]}]

    # 1. 閘道收送的每一個位元組（原始位元組與解框後的 WebSocket 訊息）都沒有內容。
    blobs = stack.relay.blobs()
    assert blobs
    for marker in (prompt_marker, reply_marker):
        for blob in blobs:
            assert marker.encode() not in blob

    # 2. DEBUG 等級的紀錄裡沒有內容（整個程序的紀錄都檢查，不只閘道）。
    gateway_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("llmop.secure"))
    assert envelope.header.request_id in gateway_log  # 對照組：閘道的紀錄確實有收進來
    for marker in (prompt_marker, reply_marker):
        assert marker not in caplog.text
    # 紀錄裡也不能有信封、封裝回覆、API 金鑰、各種權杖，以及秘密路徑（gate）。
    worker_token = stack.worker.state["worker_token"]
    secrets_ = (envelope.sealed, envelope.sig, envelope.protected, response_frame["sealed"],
                stack.client.api_key, worker_token, stack.reg_token)
    for secret in (*secrets_, stack.gate):
        assert secret not in caplog.text

    # 3. 節點的資料目錄（llmop.sqlite3 以及 SQLite 可能留下的其他檔案）沒有內容，也沒有任何秘密值的明文。
    files = stack.gateway_files()
    assert "llmop.sqlite3" in files
    db = b"".join(files.values())
    assert stack.worker_keys[0]["public_key"].encode() in db  # 對照組：讀到的是真的資料庫內容
    for needle in (prompt_marker, reply_marker, *secrets_):
        assert needle.encode() not in db


# ---------------------------------------------------------------------------
# 簽章錯、釘選不符
# ---------------------------------------------------------------------------


async def test_wrong_signature_is_rejected_by_gateway_and_by_worker(make_stack, aiohttp_server):
    stack = await make_stack()

    # 別人的簽章金鑰（知道 API 金鑰，但沒有登記的簽章私鑰）：sig_key_id 對不上。
    stranger = crypto.generate_signing_key()
    forged, _ = _seal(stack, signing_key=stranger)
    status, data = await stack.post_envelope(forged)
    assert status == 403
    assert data["error"]["code"] == "bad_signature"

    # 簽好之後改動 sealed（隧道上的竄改）：簽章驗證失敗。
    good, _ = _seal(stack)
    middle = len(good.sealed) // 2
    flipped = "A" if good.sealed[middle] != "A" else "B"
    tampered = dataclasses.replace(good, sealed=good.sealed[:middle] + flipped + good.sealed[middle + 1:])
    status, data = await stack.post_envelope(tampered)
    assert status == 403
    assert data["error"]["code"] == "bad_signature"
    assert stack.backends["ollama"].calls == []

    # 被入侵的閘道略過自己的檢查，把竄改過的信封直接送給 worker：worker 自己也驗簽。
    fake = await _switch_worker_to(stack, aiohttp_server)
    response = await fake.send_request(tampered, stack.client.public_key)
    assert response["ok"] is False
    assert response["error"] == "bad_signature"
    assert stack.backends["ollama"].calls == []


async def test_pin_mismatch_never_sends_anything(make_stack):
    """用戶端釘選的 worker 公鑰不是閘道公告的那一把（例如閘道被換掉、公告了自己的金鑰）：
    代理不會改用未釘選的金鑰，直接拒絕，隧道上沒有任何信封。"""
    stack = await make_stack()
    impostor = b64u_encode(X25519PrivateKey.generate().public_key().public_bytes_raw())
    proxy = await stack.start_proxy(pinned=[impostor])
    status, data, _ = await stack.chat(MODELS["ollama"], "不該送出的內容", proxy_url=proxy)
    assert status == 404
    assert data["error"]["code"] == "model_unavailable"
    assert stack.relay.posted_envelopes() == []
    assert stack.backends["ollama"].calls == []
    async with stack.http.get(proxy + "/v1/models") as resp:
        assert (await resp.json())["data"] == []


# ---------------------------------------------------------------------------
# 重放
# ---------------------------------------------------------------------------


async def _switch_worker_to(stack: Stack, aiohttp_server) -> FakeGateway:
    """讓 worker 改連到假閘道：之後的新連線轉到假閘道，再切斷 worker 目前的連線，讓它自己重連。"""
    fake = FakeGateway()
    server = await aiohttp_server(fake.app())
    stack.relay.retarget(server.port)
    assert stack.relay.sever_websockets() >= 1
    await wait_until(lambda: fake.ws is not None, what=" worker 連上假閘道")
    return fake


async def test_replay_is_rejected_by_gateway_and_by_worker(make_stack, aiohttp_server):
    stack = await make_stack()
    status, data, _ = await stack.chat(MODELS["ollama"], "只能執行一次的請求")
    assert status == 200
    assert len(stack.backends["ollama"].calls) == 1

    # 攻擊者在隧道上錄下了信封，也知道 API 金鑰（Cloudflare 看得到），把它原樣重送給閘道。
    (captured,) = stack.relay.posted_envelopes()
    status, data = await stack.post_envelope(captured)
    assert status == 409
    assert data["error"]["code"] == "duplicate_request"
    assert data["request_id"] == captured.header.request_id

    # 被入侵的閘道（這裡用假閘道扮演）繞過自己的檢查，直接把同一個信封送給同一個 worker 程序。
    fake = await _switch_worker_to(stack, aiohttp_server)
    client_pub = stack.client.public_key

    # 對照組：同一條連線上一筆全新的請求可以正常處理，證明 replayed 不是別的原因造成的。
    fresh, opener = _seal(stack)
    response = await fake.send_request(fresh, client_pub)
    assert response["ok"] is True
    assert opener.open(response["sealed"])["body"]["choices"][0]["message"]["content"] == DEFAULT_REPLY

    response = await fake.send_request(captured, client_pub)
    assert response == {"type": "response", "request_id": captured.header.request_id, "ok": False,
                        "error": "replayed"}
    assert len(stack.backends["ollama"].calls) == 2  # 重放的請求沒有到後端


async def test_replay_after_gateway_restart_is_still_stopped_by_worker(make_stack):
    stack = await make_stack()
    assert (await stack.chat(MODELS["ollama"]))[0] == 200
    (captured,) = stack.relay.posted_envelopes()

    # 整個節點重新啟動：資料庫與 gate 還在，但記憶體裡的待處理與結果表（閘道的去重依據）都沒了。
    gate = stack.gate
    await stack.restart_node()
    assert stack.gate == gate  # API 連結沒有因為重新啟動而改變
    await stack.wait_announced({stack.worker_keys[0]["key_id"]})  # worker 已經重新連上新的節點

    # 新節點不記得這個 request_id，信封仍在時間窗內，所以會轉給 worker；worker 的已見 ID 擋下它。
    status, data = await stack.post_envelope(captured)
    assert status == 409
    assert data["error"]["code"] == "replayed"
    assert len(stack.backends["ollama"].calls) == 1


# ---------------------------------------------------------------------------
# 金鑰輪替
# ---------------------------------------------------------------------------


async def _restart_worker(stack: Stack) -> None:
    """worker 只在啟動時讀 keys.json，所以 rotate-key 與 prune-keys 之後都要重新啟動。"""
    await stack.worker.stop()
    await stack.wait_online(False)
    stack.worker.start()


async def _chat_key_id(stack: Stack, proxy_url: str) -> str:
    """經過指定的代理送一筆請求，回傳這筆請求實際加密給哪一把 worker 金鑰（從隧道上的信封標頭看）。"""
    before = len(stack.relay.posted_envelopes())
    status, data, _ = await stack.chat(MODELS["ollama"], proxy_url=proxy_url)
    assert status == 200, data
    assert data["choices"][0]["message"]["content"] == DEFAULT_REPLY
    envelopes = stack.relay.posted_envelopes()
    assert len(envelopes) == before + 1
    return envelopes[-1].header.key_id


async def test_key_rotation(make_stack, capsys, aiohttp_server):
    stack = await make_stack()
    old = stack.worker_keys[0]
    old_proxy = stack.proxy_url  # 用戶端 A：釘選舊金鑰
    assert await _chat_key_id(stack, old_proxy) == old["key_id"]

    # llmop worker rotate-key：主控台印出新金鑰；重新啟動後兩把都公告。
    capsys.readouterr()
    assert worker_cli.main(["rotate-key"]) == 0
    (new,) = parse_key_blocks(capsys.readouterr().out)
    assert new["key_id"] != old["key_id"]
    await _restart_worker(stack)
    await stack.wait_announced({old["key_id"], new["key_id"]})
    info = await stack.console.worker(stack.worker_id)
    assert {k["key_id"] for k in info["keys"]} == {old["key_id"], new["key_id"]}
    capsys.readouterr()
    assert worker_cli.main(["show-key"]) == 0
    assert parse_key_blocks(capsys.readouterr().out) == [new, old]

    # 舊金鑰還保留著：釘選舊金鑰的用戶端照常可用；釘選新金鑰的用戶端也可用。
    new_proxy = await stack.start_proxy(pinned=[new["public_key"]])
    assert await _chat_key_id(stack, old_proxy) == old["key_id"]
    assert await _chat_key_id(stack, new_proxy) == new["key_id"]

    # llmop worker prune-keys：舊私鑰刪除，重新啟動後只公告新金鑰。
    assert worker_cli.main(["prune-keys", "--yes"]) == 0
    await _restart_worker(stack)
    await stack.wait_announced({new["key_id"]})
    assert await _chat_key_id(stack, new_proxy) == new["key_id"]

    # 釘選舊金鑰的用戶端：代理不會改用未釘選的金鑰，直接拒絕（沒有任何信封送出）。
    sent = len(stack.relay.posted_envelopes())
    status, data, _ = await stack.chat(MODELS["ollama"], proxy_url=old_proxy)
    assert status == 404
    assert data["error"]["code"] == "model_unavailable"
    assert len(stack.relay.posted_envelopes()) == sent

    # 就算有用戶端仍然把請求加密給舊金鑰：閘道回 unknown_key_id……
    status, data = await stack.post_envelope(_seal(stack, prompt="給舊金鑰", worker_public=old["public_key"])[0])
    assert status == 404
    assert data["error"]["code"] == "unknown_key_id"

    # ……繞過閘道直接送給 worker，worker 也回 unknown_key_id：舊私鑰真的不在了。
    fake = await _switch_worker_to(stack, aiohttp_server)
    assert [k["key_id"] for k in fake.hello["keys"]] == [new["key_id"]]
    response = await fake.send_request(_seal(stack, prompt="給舊金鑰", worker_public=old["public_key"])[0],
                                       stack.client.public_key)
    assert response["ok"] is False
    assert response["error"] == "unknown_key_id"


# ---------------------------------------------------------------------------
# worker 端釘選用戶端（pinned_clients）
# ---------------------------------------------------------------------------


async def test_worker_pinned_clients(make_stack):
    stack = await make_stack()
    config_path = stack.worker.home / "config.json"
    stranger = b64u_encode(crypto.public_raw(crypto.generate_signing_key()))

    async def pin(keys: list[str]) -> None:
        config = paths.read_json(config_path)
        config["pinned_clients"] = keys
        paths.write_json(config_path, config, secret=True)
        await _restart_worker(stack)
        await stack.wait_online(True)

    # 只釘選了別人：閘道照常轉送（它不知道 worker 的設定），worker 拒絕。
    await pin([stranger])
    status, data, _ = await stack.chat(MODELS["ollama"])
    assert status == 403
    assert data["error"]["code"] == "untrusted_client"
    assert stack.backends["ollama"].calls == []

    # 把這個用戶端 `llmop client keygen` 印出的公鑰加進去，就可以使用。
    await pin([stranger, stack.client.public_key])
    assert (await stack.chat(MODELS["ollama"]))[0] == 200


# ---------------------------------------------------------------------------
# 請求途中 worker 被殺掉
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["stop", "sever"], ids=["停止worker程序", "連線被切斷"])
async def test_worker_killed_mid_request_gives_worker_disconnected(make_stack, how):
    stack = await make_stack(sync_wait_seconds=30)
    backend = stack.backends["ollama"]
    backend.delay = 60  # 後端一直不回答，請求會掛在 worker 上

    call = asyncio.create_task(stack.chat(MODELS["ollama"], "很久的請求"))
    await wait_until(lambda: backend.calls, what="後端收到請求")
    if how == "stop":
        await stack.worker.stop()
    else:
        # 沒有 WebSocket 關閉訊框，和 worker 主機突然斷電、斷網一樣。
        assert stack.relay.sever_websockets() == 1

    status, data, _ = await asyncio.wait_for(call, 10)
    assert status == 502
    assert data["error"]["code"] == "worker_disconnected"
    assert data["error"]["type"] == "server_error"
    # 名額與租約都還回去了：排程器沒有卡住的請求。
    await wait_until(lambda: stack.state.scheduler.snapshot()["running"] == 0, what="名額歸還")
