"""端對端：完整路徑、註冊與重新連線、撤銷 worker、時鐘偏差、配額、撤銷 API 金鑰、慢後端走 202 輪詢。

全部經過真的統一節點（三個 uvicorn）、真的 worker、真的本機代理；閘道看到的位元組都經過錄音轉送器。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
import types

import pytest

from llmop import crypto
from llmop.protocol import b64u_decode
from llmop.worker.keystore import init_keys
from llmop.worker.link import LinkStopped

from .harness import DEFAULT_REPLY, FAKE_USAGE, MODELS, WorkerRunner, parse_sse


def _completion_parts(data: object, stream: bool) -> tuple[dict, str, dict]:
    """從非串流的 chat.completion 或串流的 SSE 文字取出 (助理訊息, finish_reason, usage)。"""
    if not stream:
        assert data["object"] == "chat.completion"
        choice = data["choices"][0]
        return choice["message"], choice["finish_reason"], data["usage"]
    events, _ = parse_sse(data)
    assert events[-1] == "[DONE]"
    content, final = events[0], events[-2]
    assert content["object"] == final["object"] == "chat.completion.chunk"
    return content["choices"][0]["delta"], final["choices"][0]["finish_reason"], final["usage"]


@pytest.mark.parametrize("stream", [False, True], ids=["非串流", "串流"])
@pytest.mark.parametrize("kind", ["ollama", "openai"])
async def test_full_path_through_each_backend(make_stack, kind, stream):
    stack = await make_stack()
    status, data, ctype = await stack.chat(MODELS[kind], "請用一句話介紹台北", stream=stream, temperature=0.3)

    assert status == 200
    assert ctype.startswith("text/event-stream" if stream else "application/json")
    message, finish_reason, usage = _completion_parts(data, stream)
    assert message["role"] == "assistant"
    assert message["content"] == DEFAULT_REPLY
    assert finish_reason == "stop"
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == FAKE_USAGE[kind]

    # 後端收到的是非串流請求；model 與 max_tokens 由 worker 依標頭強制指定。
    (call,) = stack.backends[kind].calls
    assert stack.backends[kind].paths == ["/api/chat" if kind == "ollama" else "/v1/chat/completions"]
    assert call["model"] == MODELS[kind]
    assert call["stream"] is False
    assert call["messages"] == [{"role": "user", "content": "請用一句話介紹台北"}]
    if kind == "ollama":
        assert call["options"]["num_predict"] == 77
        assert call["options"]["temperature"] == 0.3
    else:
        assert call["max_tokens"] == 77
        assert call["temperature"] == 0.3
    other = "openai" if kind == "ollama" else "ollama"
    assert stack.backends[other].calls == []

    # 閘道記下了這把金鑰的請求數與 worker 回報的 token 數；alice 在帳號網站看得到。
    prompt_tokens, completion_tokens = FAKE_USAGE[kind]
    portal = await stack.portal.portal()
    (key,) = portal["keys"]
    assert key["id"] == stack.client.api_key_id
    assert key["usage"]["all"] == {"requests": 1, "total": prompt_tokens + completion_tokens}
    assert portal["status"]["weekly"]["used"] == prompt_tokens + completion_tokens
    (row,) = stack.state.store.recent_usage("alice")
    assert row["key_id"] == stack.client.api_key_id
    assert (row["prompt_tokens"], row["completion_tokens"]) == FAKE_USAGE[kind]


async def test_proxy_lists_models_of_pinned_worker(make_stack):
    stack = await make_stack()
    async with stack.http.get(stack.proxy_url + "/v1/models") as resp:
        assert resp.status == 200
        data = await resp.json()
    assert data["object"] == "list"
    assert sorted(entry["id"] for entry in data["data"]) == sorted(MODELS.values())


async def test_worker_registration_and_reconnect_after_restart(make_stack):
    stack = await make_stack()

    # 註冊：worker 把閘道發的身分存進 state.json（0600），註冊碼已用掉。
    state = stack.worker.state
    assert state["worker_id"] == stack.worker_id
    assert state["worker_token"].startswith("llmop_wk_")
    assert stat.S_IMODE(os.stat(stack.worker.home / "state.json").st_mode) == 0o600
    info = await stack.console.worker(stack.worker_id)
    assert info["online"] is True
    assert info["reg_expires_at"] is None
    # 控制台顯示的公鑰（與指紋）與 worker 主控台印出的相同。
    assert [(k["key_id"], k["public_key"]) for k in info["keys"]] == [
        (stack.worker_keys[0]["key_id"], stack.worker_keys[0]["public_key"])]
    assert sorted(info["models"]) == sorted(MODELS.values())
    assert (await stack.chat(MODELS["ollama"]))[0] == 200

    # 重新啟動：不帶註冊碼，用 state.json 裡的 worker 權杖重新連上（同一個隧道、同一個 gate）。
    await stack.worker.stop()
    await stack.wait_online(False)
    stack.worker.start()
    await stack.wait_online(True)
    assert stack.worker.state == state
    status, data, _ = await stack.chat(MODELS["openai"])
    assert status == 200
    assert data["choices"][0]["message"]["content"] == DEFAULT_REPLY

    # 註冊碼只能用一次：另一台 worker 拿同一個註冊碼會被拒絕並停止。
    other_home = stack.home / "another-worker"
    other_home.mkdir()
    init_keys(other_home / "keys.json")
    shutil.copy(stack.worker.home / "config.json", other_home / "config.json")
    other = WorkerRunner(other_home)
    with pytest.raises(LinkStopped):
        await asyncio.wait_for(other.start(stack.reg_token), 10)
    assert not (other_home / "state.json").exists()
    assert [w["id"] for w in await stack.console.workers()] == [stack.worker_id]


async def test_revoked_worker_is_cut_off_and_stops(make_stack):
    stack = await make_stack()
    status, data = await stack.console.call("POST", f"/ui/api/admin/workers/{stack.worker_id}/revoke")
    assert status == 200, data
    assert data["worker"]["revoked_at"] is not None

    # 閘道立刻切斷連線；worker 重連時被拒絕（unauthorized），不再重試而是停止。
    with pytest.raises(LinkStopped):
        await asyncio.wait_for(stack.worker.task, 10)
    assert (await stack.console.worker(stack.worker_id))["online"] is False
    status, data, _ = await stack.chat(MODELS["ollama"])
    assert status == 404
    assert data["error"]["code"] == "model_unavailable"


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(-400, "stale_timestamp"), (400, "stale_timestamp"), (200, None), (-200, "stale_timestamp")],
    ids=["落後超過五分鐘", "超前超過五分鐘", "超前不到五分鐘", "落後不到五分鐘但worker剛啟動"],
)
async def test_client_clock_must_be_within_five_minutes(make_stack, monkeypatch, offset, expected):
    stack = await make_stack()
    real_time = time.time
    # 只讓用戶端封裝請求時用的時鐘偏掉（ts 由 llmop.crypto.seal_request 填入）。
    monkeypatch.setattr(crypto, "time", types.SimpleNamespace(time=lambda: real_time() + offset))

    status, data, _ = await stack.chat(MODELS["ollama"])
    if expected is None:
        assert status == 200
        return
    assert status == 400
    assert data["error"]["code"] == expected
    # 落後 200 秒：閘道的時間窗接受，但 worker 拒絕早於自己啟動時刻的 ts（重放防禦的開機門檻）。
    # 這是 docs/secure/SECURITY.md 記載的代價：worker 剛重啟後，時鐘落後的用戶端會暫時被拒絕。
    assert stack.backends["ollama"].calls == []


@pytest.mark.parametrize(
    "quota",
    [{"requests_per_day": 1}, {"tokens_per_day": sum(FAKE_USAGE["ollama"])}],
    ids=["每日請求數", "每日token數"],
)
async def test_quota_exceeded_in_openai_error_shape(make_stack, quota):
    stack = await make_stack(alice={"quota": quota})
    status, _, _ = await stack.chat(MODELS["ollama"], max_tokens=60)
    assert status == 200

    # 第二筆：請求數已達 1；或已用掉 16 個 token（= 每日上限，D5：事後記帳，用完才擋）。
    status, data, _ = await stack.chat(MODELS["ollama"], max_tokens=60)
    assert status == 429
    assert data.keys() == {"error"}
    assert data["error"]["code"] == "quota_exceeded"
    assert data["error"]["type"] == "invalid_request_error"
    assert isinstance(data["error"]["message"], str) and data["error"]["message"]
    assert len(stack.backends["ollama"].calls) == 1  # 被擋下的請求沒有送到 worker


async def test_revoked_api_key_is_unauthorized(make_stack):
    stack = await make_stack()
    assert (await stack.chat(MODELS["ollama"]))[0] == 200

    # alice 在帳號網站撤銷自己的金鑰。
    await stack.portal.revoke_key(stack.client.api_key_id)
    assert (await stack.portal.portal())["keys"] == []

    # 經過代理：OpenAI 錯誤形狀的 401。
    status, data, _ = await stack.chat(MODELS["ollama"])
    assert status == 401
    assert data["error"]["code"] == "unauthorized"
    assert data["error"]["type"] == "invalid_request_error"

    # 直接把一個簽好的新信封送給閘道，也是 unauthorized。
    envelope, _ = crypto.seal_request(
        {"messages": [{"role": "user", "content": "撤銷後"}]}, model=MODELS["ollama"], max_tokens=16,
        worker_public=b64u_decode(stack.worker_keys[0]["public_key"]), signing_key=stack.client.signing_key,
    )
    status, data = await stack.post_envelope(envelope)
    assert status == 401
    assert data["error"]["code"] == "unauthorized"
    assert data["request_id"] == envelope.header.request_id
    assert len(stack.backends["ollama"].calls) == 1


@pytest.mark.parametrize("stream", [False, True], ids=["非串流", "串流"])
async def test_slow_backend_completes_through_202_and_polling(make_stack, stream):
    stack = await make_stack(sync_wait_seconds=0.2)
    stack.backends["openai"].delay = 1.0  # 比同步等待時間長得多

    status, data, _ = await stack.chat(MODELS["openai"], stream=stream)
    assert status == 200
    message, _, _ = _completion_parts(data, stream)
    assert message["content"] == DEFAULT_REPLY

    # 隧道上看到的：POST 先得到 202，接著用同一個 request_id 輪詢，最後一次是 200。
    exchanges = stack.relay.http_exchanges()
    gate = stack.gate
    ((_, post_resp),) = [(q, r) for q, r in exchanges
                         if q.method_path == ("POST", f"/{gate}/e2e/v1/chat/completions")]
    assert post_resp.status == 202
    pending = json.loads(post_resp.body)
    request_id = pending["request_id"]
    assert pending["status"] == "pending"
    assert pending.keys() <= {"request_id", "status", "queue"}
    if "queue" in pending:  # 已在處理：位置 0（v0.2 的排隊資訊）
        assert pending["queue"]["position"] == 0
    polls = [r for q, r in exchanges if q.method_path == ("GET", f"/{gate}/e2e/v1/requests/{request_id}")]
    assert len(polls) >= 2
    assert [r.status for r in polls[:-1]] == [202] * (len(polls) - 1)
    assert polls[-1].status == 200
    assert json.loads(polls[-1].body)["status"] == "done"

    if stream:
        # 等待期間代理每隔一段時間送出註解行（有排隊資訊時是 ": queue …"），讓呼叫端知道連線還活著。
        _, comments = parse_sse(data)
        assert any(c == ": waiting" or c.startswith(": queue ") for c in comments), comments
