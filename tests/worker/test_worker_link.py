"""透過假閘道與假後端的整合測試：註冊、hello、請求往返、重連、並行上限與紀錄內容。"""

import asyncio
import json
import logging
import os
import stat

import pytest
from worker_support import MODEL, PROMPT, REPLY, FakeLLM, sign_and_seal

from llmop import paths, protocol
from llmop.worker import cli
from llmop.worker.config import parse_config
from llmop.worker.keystore import load_state, rotate, save_keys
from llmop.worker.link import LinkStopped, serve


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def check_completion(body):
    assert body["object"] == "chat.completion"
    message = body["choices"][0]["message"]
    assert message["role"] == "assistant" and message["content"] == REPLY
    assert body["choices"][0]["finish_reason"] == "stop"


async def test_register_then_hello_on_first_run_and_hello_only_later(home, keystore, gateway, ollama, start_worker):
    task = start_worker(ollama)
    register = await gateway.next_frame()
    assert register == protocol.register("llmop_reg_test")
    hello = await gateway.next_frame()
    assert hello["type"] == "hello"
    assert hello["worker_id"] == gateway.worker_id and hello["worker_token"] == gateway.worker_token
    assert hello["keys"] == [keystore.current_key().announce()]
    assert hello["models"] == [MODEL]
    assert hello["max_concurrency"] == 2

    state = load_state(home / "state.json")
    assert (state.worker_id, state.worker_token) == (gateway.worker_id, gateway.worker_token)
    assert mode(home / "state.json") == 0o600
    assert mode(home / "keys.json") == 0o600

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # 第二次啟動：已有 state.json，直接 hello，不再需要註冊權杖。
    start_worker(ollama, reg_token=None)
    frame = await gateway.next_frame()
    assert frame["type"] == "hello" and frame["worker_token"] == gateway.worker_token


async def test_hello_lists_current_key_first(home, keystore, gateway, ollama, start_worker):
    rotated = rotate(keystore)
    save_keys(home / "keys.json", rotated)
    start_worker(ollama, store=rotated)
    hello = await gateway.next_frame("hello")
    assert [k["key_id"] for k in hello["keys"]] == [rotated.current, keystore.current]


@pytest.mark.parametrize("backend_name", ["ollama", "openai"])
async def test_happy_path_through_each_backend(keystore, signing_key, gateway, ollama, openai, start_worker,
                                               backend_name):
    backend = ollama if backend_name == "ollama" else openai
    start_worker(backend)
    await gateway.wait_hello()
    body = {"messages": [{"role": "user", "content": PROMPT}], "model": "evil", "max_tokens": 9999,
            "max_completion_tokens": 8888, "temperature": 0.5}
    envelope, opener = sign_and_seal(keystore.current_key(), signing_key, body=body, max_tokens=321)
    frame = await gateway.relay(envelope, signing_key)
    assert frame["ok"] is True
    reply = opener.open(frame["sealed"])
    assert reply["ok"] is True
    check_completion(reply["body"])
    assert frame["usage"] == {"prompt_tokens": 12, "completion_tokens": 7}
    assert (reply["body"]["usage"]["prompt_tokens"], reply["body"]["usage"]["completion_tokens"]) == (12, 7)

    # 標頭的 model 與 max_tokens 蓋過本文裡的值；一律非串流。
    sent = backend.calls[0]
    assert sent["model"] == MODEL
    assert sent["stream"] is False
    if backend_name == "ollama":
        assert sent["options"]["num_predict"] == 321
        assert sent["options"]["temperature"] == 0.5
        assert "max_tokens" not in sent
    else:
        assert sent["max_tokens"] == 321
        assert sent["temperature"] == 0.5
        assert "max_completion_tokens" not in sent
        assert "Authorization" not in backend.headers[0]


async def test_openai_api_key_is_sent_when_set(keystore, signing_key, gateway, openai, start_worker):
    start_worker({"kind": "openai", "base_url": openai.base_url, "api_key": "lm-secret"})
    await gateway.wait_hello()
    envelope, _ = sign_and_seal(keystore.current_key(), signing_key)
    await gateway.relay(envelope, signing_key)
    assert openai.headers[0]["Authorization"] == "Bearer lm-secret"


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_backend_failure_and_timeout_come_back_sealed(keystore, signing_key, gateway, ollama, start_worker,
                                                            failure):
    if failure == "error":
        ollama.status = 500
    else:
        ollama.delay = 1.0
    start_worker(ollama, request_timeout=0.2)
    await gateway.wait_hello()
    envelope, opener = sign_and_seal(keystore.current_key(), signing_key)
    frame = await gateway.relay(envelope, signing_key)
    assert frame["ok"] is True
    assert frame["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
    assert set(frame) == {"type", "request_id", "ok", "sealed", "usage"}
    reply = opener.open(frame["sealed"])
    assert reply["ok"] is False
    assert reply["error"]["code"] == ("backend_error" if failure == "error" else "backend_timeout")
    if failure == "error":
        assert "HTTP 500" in reply["error"]["message"]


async def test_pre_decryption_rejection_through_link(keystore, signing_key, gateway, ollama, start_worker):
    start_worker(ollama)
    await gateway.wait_hello()
    envelope, _ = sign_and_seal(keystore.current_key(), signing_key, model="not-served")
    frame = await gateway.relay(envelope, signing_key)
    assert frame == protocol.response_error(envelope.header.request_id, "model_unavailable")


async def test_reconnects_after_gateway_drops(keystore, signing_key, gateway, ollama, start_worker):
    gateway.drop_after_ready = True
    start_worker(ollama)
    await gateway.next_frame("hello")
    await gateway.wait_hello()  # 第二條連線
    assert gateway.connections == 2
    envelope, opener = sign_and_seal(keystore.current_key(), signing_key)
    frame = await gateway.relay(envelope, signing_key)
    assert opener.open(frame["sealed"])["ok"] is True


async def test_reconnects_when_gateway_is_down_at_first(home, keystore, ollama, unused_tcp_port):
    # 一開始閘道還沒起來：連線被拒，worker 持續重試，不會結束。
    port = unused_tcp_port
    config = parse_config(
        {
            "gateway_url": f"ws://127.0.0.1:{port}/worker/ws",
            "backends": [{"kind": "ollama", "base_url": ollama.base_url}],
        }
    )
    task = asyncio.create_task(serve(config, keystore, home, "llmop_reg_test", min_backoff=0.01, max_backoff=0.02))
    await asyncio.sleep(0.3)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_unauthorized_stops_the_worker(keystore, gateway, ollama, start_worker):
    gateway.hello_reply = protocol.error("unauthorized")
    task = start_worker(ollama)
    with pytest.raises(LinkStopped):
        await asyncio.wait_for(task, 5)
    assert gateway.connections == 1


async def test_other_handshake_errors_are_retried(keystore, gateway, ollama, start_worker):
    gateway.hello_reply = protocol.error("key_conflict")
    task = start_worker(ollama)
    await gateway.next_frame("hello")
    await gateway.next_frame("hello")
    assert not task.done()


async def test_cli_run_exits_nonzero_on_unauthorized(home, keystore, gateway, ollama, capsys):
    paths.write_json(home / "config.json", {"gateway_url": gateway.url,
                                            "backends": [{"kind": "ollama", "base_url": ollama.base_url}]})
    gateway.hello_reply = protocol.error("unauthorized")
    code = await asyncio.wait_for(asyncio.to_thread(cli.main, ["run", "--reg-token", "llmop_reg_x"]), 10)
    assert code == 1
    captured = capsys.readouterr()
    assert keystore.current_key().public_key in captured.out
    assert "worker 已停止" in captured.err


async def test_max_concurrency_one_serialises_backend_calls(keystore, signing_key, gateway, ollama, start_worker):
    ollama.delay = 0.2
    start_worker(ollama, max_concurrency=1)
    hello = await gateway.wait_hello()
    assert hello["max_concurrency"] == 1
    first, opener1 = sign_and_seal(keystore.current_key(), signing_key)
    second, opener2 = sign_and_seal(keystore.current_key(), signing_key)
    frames = await asyncio.gather(gateway.relay(first, signing_key), gateway.relay(second, signing_key))
    assert opener1.open(frames[0]["sealed"])["ok"] and opener2.open(frames[1]["sealed"])["ok"]
    assert ollama.max_active == 1
    (start_a, end_a), (start_b, _) = sorted(ollama.spans)
    assert start_b >= end_a


async def test_requests_run_concurrently_up_to_the_limit(keystore, signing_key, gateway, ollama, start_worker):
    ollama.delay = 0.2
    start_worker(ollama, max_concurrency=2)
    await gateway.wait_hello()
    envelopes = [sign_and_seal(keystore.current_key(), signing_key)[0] for _ in range(2)]
    await asyncio.gather(*(gateway.relay(e, signing_key) for e in envelopes))
    assert ollama.max_active == 2


async def test_models_refresh_sends_update_on_change(gateway, ollama, start_worker):
    start_worker(ollama, link_options={"refresh_seconds": 0.05})
    hello = await gateway.wait_hello()
    assert hello["models"] == [MODEL]
    ollama.models = [MODEL, "llama3.2:3b"]
    update = await gateway.next_frame("models")
    assert update == protocol.models(["llama3.2:3b", MODEL])


async def test_expose_models_filters_the_list(gateway, ollama, start_worker):
    ollama.models = [MODEL, "secret-model"]
    start_worker(ollama, expose_models=[MODEL])
    hello = await gateway.wait_hello()
    assert hello["models"] == [MODEL]


async def test_duplicate_model_first_backend_wins(keystore, signing_key, gateway, ollama, openai, start_worker,
                                                  caplog):
    caplog.set_level(logging.WARNING)
    start_worker(ollama, openai)
    hello = await gateway.wait_hello()
    assert hello["models"] == [MODEL]
    assert any("同時由" in r.getMessage() for r in caplog.records)
    envelope, _ = sign_and_seal(keystore.current_key(), signing_key)
    await gateway.relay(envelope, signing_key)
    assert len(ollama.calls) == 1 and openai.calls == []


async def test_unreachable_backend_is_skipped(unused_tcp_port, gateway, ollama, start_worker):
    dead = {"kind": "openai", "base_url": f"http://127.0.0.1:{unused_tcp_port}"}
    start_worker(dead, ollama)
    hello = await gateway.wait_hello()
    assert hello["models"] == [MODEL]


async def test_malformed_request_frame_gets_bare_error(keystore, signing_key, gateway, ollama, start_worker):
    start_worker(ollama)
    await gateway.wait_hello()
    envelope, _ = sign_and_seal(keystore.current_key(), signing_key)
    frame = protocol.request(envelope.header.request_id, envelope, "not-a-key")
    await gateway.ws.send_str(json.dumps(frame))
    response = await gateway.next_frame("response")
    assert response == protocol.response_error(envelope.header.request_id, "bad_envelope")


async def test_logs_never_contain_plaintext(keystore, signing_key, gateway, ollama, start_worker, caplog):
    caplog.set_level(logging.DEBUG)
    start_worker(ollama)
    await gateway.wait_hello()
    envelope, opener = sign_and_seal(keystore.current_key(), signing_key)
    frame = await gateway.relay(envelope, signing_key)
    assert opener.open(frame["sealed"])["body"]["choices"][0]["message"]["content"] == REPLY

    ollama.status = 500  # 錯誤訊息裡帶著提示內容
    envelope, opener = sign_and_seal(keystore.current_key(), signing_key)
    frame = await gateway.relay(envelope, signing_key)
    assert PROMPT in opener.open(frame["sealed"])["error"]["message"]

    text = caplog.text
    assert envelope.header.request_id in text  # 紀錄確實有寫進來
    for secret in (PROMPT, REPLY, "春眠", "請幫我", gateway.worker_token, keystore.current_key().public_key,
                   envelope.sealed, frame["sealed"]):
        assert secret not in text


async def test_worker_opens_no_listening_socket(home, keystore, gateway, ollama, start_worker):
    def listening_inodes():
        inodes = set()
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table) as fh:
                    rows = fh.read().splitlines()[1:]
            except OSError:
                continue
            inodes |= {row.split()[9] for row in rows if row.split()[3] == "0A"}  # 0A = LISTEN
        return inodes

    def own_socket_inodes():
        found = set()
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                found.add(target[8:-1])
        return found

    if not os.path.exists("/proc/net/tcp"):
        pytest.skip("需要 /proc")
    before = own_socket_inodes() & listening_inodes()  # 假伺服器自己的監聽 socket
    start_worker(ollama)
    await gateway.wait_hello()
    await asyncio.sleep(0.05)
    assert own_socket_inodes() & listening_inodes() == before

