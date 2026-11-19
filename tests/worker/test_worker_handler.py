"""請求處理器：解密前的每一種拒絕回裸錯誤碼；解密後的一切都封裝送回。"""

import json
from dataclasses import replace

import pytest
from worker_support import MODEL, PROMPT, REPLY, request_frame, sign_and_seal

from llmop import protocol
from llmop.crypto import generate_signing_key, public_raw
from llmop.protocol import b64u_decode, b64u_encode
from llmop.worker import cli
from llmop.worker.backends import Backend, BackendError
from llmop.worker.handler import Handler
from llmop.worker.keystore import load_keys, new_key
from llmop.worker.replay import ReplayGuard

NOW = 1_800_000_000


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def completion(text=REPLY, prompt_tokens=11, completion_tokens=5):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": NOW,
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": 16},
    }


class RecordingBackend:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or completion()
        self.error = error

    async def list_models(self):
        return [MODEL]

    async def chat(self, body, model, max_tokens):
        self.calls.append((body, model, max_tokens))
        if self.error:
            raise self.error
        return self.result

    def backend(self):
        return Backend(name="fake", list_models=self.list_models, chat=self.chat)


def make_handler(store, fake=None, *, clock=None, pinned=frozenset()):
    fake = fake or RecordingBackend()
    return Handler(
        keys=store,
        replay=ReplayGuard(clock or Clock()),
        max_concurrency=2,
        pinned_clients=pinned,
        routes={MODEL: fake.backend()},
    )


def seal(store, signing_key, **kwargs):
    kwargs.setdefault("now", NOW)
    return sign_and_seal(store.current_key(), signing_key, **kwargs)


async def test_happy_path(keystore, signing_key):
    fake = RecordingBackend()
    handler = make_handler(keystore, fake)
    envelope, opener = seal(keystore, signing_key)
    frame = await handler.handle(request_frame(envelope, signing_key))
    protocol.parse_frame(json.dumps(frame))
    assert frame["ok"] is True
    assert frame["usage"] == {"prompt_tokens": 11, "completion_tokens": 5}
    reply = opener.open(frame["sealed"])
    assert reply == {"ok": True, "body": completion()}
    assert fake.calls[0][0]["messages"][0]["content"] == PROMPT


async def assert_bare(handler, frame, code):
    response = await handler.handle(frame)
    assert response == protocol.response_error(frame["request_id"], code)
    protocol.parse_frame(json.dumps(response))


async def test_unknown_key_id(keystore, signing_key):
    handler = make_handler(keystore)
    envelope, _ = sign_and_seal(new_key(), signing_key, now=NOW)
    await assert_bare(handler, request_frame(envelope, signing_key), "unknown_key_id")


async def test_sig_key_id_mismatch(keystore, signing_key):
    handler = make_handler(keystore)
    envelope, _ = seal(keystore, signing_key)
    other = public_raw(generate_signing_key())
    await assert_bare(handler, request_frame(envelope, signing_key, client_pub=other), "bad_signature")


async def test_bad_signature(keystore, signing_key):
    handler = make_handler(keystore)
    envelope, _ = seal(keystore, signing_key)
    sig = bytearray(b64u_decode(envelope.sig))
    sig[0] ^= 1
    forged = replace(envelope, sig=b64u_encode(bytes(sig)))
    await assert_bare(handler, request_frame(forged, signing_key), "bad_signature")


async def test_untrusted_client_with_pinned_clients(keystore, signing_key):
    pinned = frozenset({public_raw(generate_signing_key())})
    handler = make_handler(keystore, pinned=pinned)
    envelope, _ = seal(keystore, signing_key)
    await assert_bare(handler, request_frame(envelope, signing_key), "untrusted_client")

    allowed = make_handler(keystore, pinned=frozenset({public_raw(signing_key)}))
    assert (await allowed.handle(request_frame(envelope, signing_key)))["ok"] is True


@pytest.mark.parametrize("offset", [-301, 301])
async def test_stale_timestamp_both_directions(keystore, signing_key, offset):
    clock = Clock(NOW)
    handler = make_handler(keystore, clock=clock)
    clock.now = NOW + 1000  # 讓「過去」的時間戳不會先撞上開機門檻
    envelope, _ = seal(keystore, signing_key, now=NOW + 1000 + offset)
    await assert_bare(handler, request_frame(envelope, signing_key), "stale_timestamp")


async def test_boot_barrier(keystore, signing_key):
    handler = make_handler(keystore, clock=Clock(NOW))
    envelope, _ = seal(keystore, signing_key, now=NOW - 5)
    await assert_bare(handler, request_frame(envelope, signing_key), "stale_timestamp")


async def test_replayed_request(keystore, signing_key):
    fake = RecordingBackend()
    handler = make_handler(keystore, fake)
    envelope, _ = seal(keystore, signing_key)
    frame = request_frame(envelope, signing_key)
    assert (await handler.handle(frame))["ok"] is True
    await assert_bare(handler, frame, "replayed")
    assert len(fake.calls) == 1


async def test_model_not_served(keystore, signing_key):
    handler = make_handler(keystore)
    envelope, _ = seal(keystore, signing_key, model="llama-3.3-70b")
    await assert_bare(handler, request_frame(envelope, signing_key), "model_unavailable")


async def test_tampered_ciphertext_is_decrypt_failed(keystore, signing_key):
    fake = RecordingBackend()
    handler = make_handler(keystore, fake)
    envelope, _ = seal(keystore, signing_key)
    sealed = bytearray(b64u_decode(envelope.sealed))
    sealed[40] ^= 1
    tampered = replace(envelope, sealed=b64u_encode(bytes(sealed)))
    # 重新簽章，讓竄改的密文通過簽章檢查、一路走到解密。
    tampered = replace(tampered, sig=b64u_encode(signing_key.sign(tampered.signing_input())))
    await assert_bare(handler, request_frame(tampered, signing_key), "decrypt_failed")
    assert fake.calls == []


async def test_older_key_works_until_pruned(home, keystore, signing_key):
    old_key = keystore.current_key()
    assert cli.main(["rotate-key"]) == 0
    rotated = load_keys(home / "keys.json")
    assert rotated.current != old_key.key_id

    envelope, opener = sign_and_seal(old_key, signing_key, now=NOW)
    frame = await make_handler(rotated).handle(request_frame(envelope, signing_key))
    assert opener.open(frame["sealed"])["ok"] is True

    assert cli.main(["prune-keys", "--yes"]) == 0
    pruned = load_keys(home / "keys.json")
    envelope, _ = sign_and_seal(old_key, signing_key, now=NOW)
    await assert_bare(make_handler(pruned), request_frame(envelope, signing_key), "unknown_key_id")


@pytest.mark.parametrize("stream", [True, "yes", None])
async def test_stream_is_sealed_bad_request(keystore, signing_key, stream):
    fake = RecordingBackend()
    handler = make_handler(keystore, fake)
    body = {"messages": [{"role": "user", "content": PROMPT}], "stream": stream}
    envelope, opener = seal(keystore, signing_key, body=body)
    frame = await handler.handle(request_frame(envelope, signing_key))
    assert frame["ok"] is True and frame["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
    reply = opener.open(frame["sealed"])
    assert reply["ok"] is False and reply["error"]["code"] == "bad_request"
    assert fake.calls == []


async def test_stream_false_is_allowed(keystore, signing_key):
    handler = make_handler(keystore)
    body = {"messages": [{"role": "user", "content": PROMPT}], "stream": False}
    envelope, opener = seal(keystore, signing_key, body=body)
    frame = await handler.handle(request_frame(envelope, signing_key))
    assert opener.open(frame["sealed"])["ok"] is True


@pytest.mark.parametrize(
    "error, code",
    [
        (BackendError("backend_error", f"boom {PROMPT}"), "backend_error"),
        (BackendError("backend_timeout", "slow"), "backend_timeout"),
        (KeyError(PROMPT), "backend_error"),
    ],
)
async def test_backend_failures_are_sealed(keystore, signing_key, error, code):
    handler = make_handler(keystore, RecordingBackend(error=error))
    envelope, opener = seal(keystore, signing_key)
    frame = await handler.handle(request_frame(envelope, signing_key))
    assert frame["ok"] is True
    assert frame["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
    assert PROMPT not in json.dumps(frame, ensure_ascii=False)
    reply = opener.open(frame["sealed"])
    assert reply["ok"] is False and reply["error"]["code"] == code


async def test_header_model_and_max_tokens_reach_backend(keystore, signing_key):
    fake = RecordingBackend()
    handler = make_handler(keystore, fake)
    body = {"messages": [{"role": "user", "content": PROMPT}], "model": "other", "max_tokens": 9999}
    envelope, _ = seal(keystore, signing_key, body=body, max_tokens=77)
    await handler.handle(request_frame(envelope, signing_key))
    _, model, max_tokens = fake.calls[0]
    assert (model, max_tokens) == (MODEL, 77)


async def test_unusable_reply_key_gives_bare_error(keystore, signing_key, monkeypatch):
    handler = make_handler(keystore)
    envelope, _ = seal(keystore, signing_key)

    def broken_seal(self, payload):
        raise protocol.ProtocolError("bad_envelope", "x")

    monkeypatch.setattr("llmop.crypto.ReplySealer.seal", broken_seal)
    await assert_bare(handler, request_frame(envelope, signing_key), "bad_envelope")


async def test_malformed_usage_counts_as_zero(keystore, signing_key):
    result = completion()
    result["usage"] = {"prompt_tokens": -1, "completion_tokens": True}
    handler = make_handler(keystore, RecordingBackend(result=result))
    envelope, _ = seal(keystore, signing_key)
    frame = await handler.handle(request_frame(envelope, signing_key))
    assert frame["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
