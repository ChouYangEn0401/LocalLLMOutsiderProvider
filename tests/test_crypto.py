"""密碼模組測試：往返、竄改、網域分離與 RFC 9180 互通。"""

import hashlib
import json
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from llmop.crypto import (
    SUITE,
    ReplyOpener,
    ReplySealer,
    generate_signing_key,
    generate_worker_key,
    load_signing_private,
    load_signing_public,
    load_worker_private,
    load_worker_public,
    open_request,
    pad,
    private_raw,
    public_raw,
    seal_request,
    signing_key_id,
    verify_request,
    worker_key_id,
)
from llmop.protocol import PAD_BLOCK, Envelope, Header, ProtocolError, b64u_decode, b64u_encode

BODY = {
    "messages": [{"role": "user", "content": "你好，請介紹你自己。"}],
    "temperature": 0.7,
    "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
}
REPLY = {
    "ok": True,
    "body": {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "我是本機模型。"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    },
}
NOW = 1_795_000_000


@pytest.fixture
def keys():
    return generate_worker_key(), generate_signing_key()


def seal(worker_key, signing_key, body=BODY, **kw):
    return seal_request(
        body,
        model=kw.pop("model", "qwen3:8b"),
        max_tokens=kw.pop("max_tokens", 1024),
        worker_public=public_raw(worker_key),
        signing_key=signing_key,
        now=kw.pop("now", NOW),
    )


def resign(envelope, signing_key):
    unsigned = replace(envelope, sig=b64u_encode(bytes(64)))
    return replace(unsigned, sig=b64u_encode(signing_key.sign(unsigned.signing_input())))


def tamper(envelope, **changes):
    header = replace(envelope.header, **changes)
    return replace(envelope, protected=header.encode())


def code_of(fn, *args):
    with pytest.raises(ProtocolError) as info:
        fn(*args)
    return info.value.code


# --- 套件與金鑰 ---------------------------------------------------------


def test_suite_is_native_hpke():
    assert isinstance(SUITE, hpke.Suite)
    sk = X25519PrivateKey.generate()
    sealed = SUITE.encrypt(b"hello", sk.public_key(), b"info")
    assert len(sealed) == 32 + 5 + 16
    assert SUITE.decrypt(sealed, sk, b"info") == b"hello"


def test_key_round_trips():
    wk, sk = generate_worker_key(), generate_signing_key()
    assert isinstance(wk, X25519PrivateKey)
    assert isinstance(sk, Ed25519PrivateKey)
    for raw in (public_raw(wk), private_raw(wk), public_raw(sk), private_raw(sk)):
        assert len(raw) == 32
    assert private_raw(load_worker_private(private_raw(wk))) == private_raw(wk)
    assert public_raw(load_worker_public(public_raw(wk))) == public_raw(wk)
    assert private_raw(load_signing_private(private_raw(sk))) == private_raw(sk)
    assert public_raw(load_signing_public(public_raw(sk))) == public_raw(sk)
    # public_raw 也接受公鑰物件
    assert public_raw(wk.public_key()) == public_raw(wk)


def test_key_ids_follow_formula():
    raw = bytes(range(32))
    expect_w = b64u_encode(hashlib.sha256(b"LLMOP/v1/key-id/x25519\x00" + raw).digest()[:16])
    expect_s = b64u_encode(hashlib.sha256(b"LLMOP/v1/key-id/ed25519\x00" + raw).digest()[:16])
    assert worker_key_id(raw) == expect_w
    assert signing_key_id(raw) == expect_s
    assert len(expect_w) == 22


def test_worker_and_signing_key_ids_differ_for_same_bytes():
    raw = public_raw(generate_worker_key())
    assert worker_key_id(raw) != signing_key_id(raw)


def test_pad():
    assert pad(b"") == b""
    assert pad(b"x") == b"x" + b" " * (PAD_BLOCK - 1)
    assert len(pad(b"x" * PAD_BLOCK)) == PAD_BLOCK
    assert len(pad(b"x" * (PAD_BLOCK + 1))) == 2 * PAD_BLOCK


# --- 往返 ---------------------------------------------------------------


def test_round_trip(keys):
    worker_key, signing_key = keys
    envelope, opener = seal(worker_key, signing_key)

    # 經過 JSON 傳輸後仍然一樣
    envelope = Envelope.from_json(json.loads(json.dumps(envelope.to_json())))
    header = verify_request(envelope, public_raw(signing_key))
    assert header == envelope.header
    assert header.key_id == worker_key_id(public_raw(worker_key))
    assert header.sig_key_id == signing_key_id(public_raw(signing_key))
    assert header.ts == NOW
    assert (header.model, header.max_tokens) == ("qwen3:8b", 1024)

    body, sealer = open_request(envelope, worker_key)
    assert body == BODY
    assert isinstance(sealer, ReplySealer)
    sealed_reply = sealer.seal(REPLY)
    assert opener.open(sealed_reply) == REPLY

    error_reply = {"ok": False, "error": {"code": "backend_error", "message": "後端失敗"}}
    assert opener.open(sealer.seal(error_reply)) == error_reply


def test_seal_request_uses_current_time(keys):
    import time

    before = int(time.time())
    envelope, _ = seal_request(
        BODY, model="m", max_tokens=1, worker_public=public_raw(keys[0]), signing_key=keys[1]
    )
    assert before <= envelope.header.ts <= int(time.time())


def test_opener_does_not_expose_private_key_in_repr(keys):
    _, opener = seal(*keys)
    assert isinstance(opener, ReplyOpener)
    assert "reply_key" not in repr(opener)


@pytest.mark.parametrize("size", [0, 500, 1024, 5000])
def test_sealed_lengths(keys, size):
    worker_key, signing_key = keys
    body = {"messages": [{"role": "user", "content": "a" * size}]}
    envelope, opener = seal(worker_key, signing_key, body)
    for sealed in (envelope.sealed, open_request(envelope, worker_key)[1].seal({"ok": True, "body": body})):
        n = len(b64u_decode(sealed)) - 32 - 16
        assert n > 0 and n % PAD_BLOCK == 0


def test_seal_request_validates_header(keys):
    worker_key, signing_key = keys
    with pytest.raises(ProtocolError):
        seal(worker_key, signing_key, max_tokens=0)
    with pytest.raises(ProtocolError):
        seal(worker_key, signing_key, model="m" * 201)


# --- 竄改與重簽 ---------------------------------------------------------

TAMPERS = {
    "request_id": lambda h: b64u_encode(bytes(16)),
    "ts": lambda h: h.ts + 1,
    "key_id": lambda h: b64u_encode(bytes(16)),
    "sig_key_id": lambda h: b64u_encode(bytes(16)),
    "model": lambda h: h.model + "-x",
    "max_tokens": lambda h: h.max_tokens + 1,
}


def test_tampers_cover_every_mutable_field():
    # v 與 typ 只有一個合法值，改了就在解析時被拒絕。
    assert set(TAMPERS) | {"v", "typ"} == {f for f in Header.__dataclass_fields__}


@pytest.mark.parametrize("field", list(TAMPERS))
def test_tampered_header_fails_verify(keys, field):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    tampered = tamper(envelope, **{field: TAMPERS[field](envelope.header)})
    assert code_of(verify_request, tampered, public_raw(signing_key)) == "bad_signature"


@pytest.mark.parametrize("field", [f for f in TAMPERS if f != "sig_key_id"])
def test_resigned_tampered_header_fails_decrypt(keys, field):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    tampered = resign(tamper(envelope, **{field: TAMPERS[field](envelope.header)}), signing_key)
    verify_request(tampered, public_raw(signing_key))
    assert code_of(open_request, tampered, worker_key) == "decrypt_failed"


def test_tampered_sealed_fails_verify(keys):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    raw = bytearray(b64u_decode(envelope.sealed))
    raw[40] ^= 1
    tampered = replace(envelope, sealed=b64u_encode(bytes(raw)))
    assert code_of(verify_request, tampered, public_raw(signing_key)) == "bad_signature"
    # 即使有人重簽，密文也解不開
    assert code_of(open_request, resign(tampered, signing_key), worker_key) == "decrypt_failed"


def test_resign_with_other_key_fails_open(keys):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    attacker = generate_signing_key()
    stolen = resign(tamper(envelope, sig_key_id=signing_key_id(public_raw(attacker))), attacker)
    # 攻擊者的簽章本身有效……
    assert verify_request(stolen, public_raw(attacker)).sig_key_id == signing_key_id(public_raw(attacker))
    # ……但 protected 不同，info 不同，worker 解不開
    assert code_of(open_request, stolen, worker_key) == "decrypt_failed"


def test_wrong_worker_key(keys):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    assert code_of(open_request, envelope, generate_worker_key()) == "decrypt_failed"


def test_wrong_client_public_key(keys):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    other = public_raw(generate_signing_key())
    assert code_of(verify_request, envelope, other) == "bad_signature"
    assert code_of(verify_request, envelope, b"\x00" * 31) == "bad_signature"


def test_signature_from_other_key_with_matching_sig_key_id_fails(keys):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    forged = replace(envelope, sig=b64u_encode(generate_signing_key().sign(envelope.signing_input())))
    assert code_of(verify_request, forged, public_raw(signing_key)) == "bad_signature"


# --- 回覆的綁定與網域分離 -----------------------------------------------


def test_reply_for_a_does_not_open_with_opener_of_b(keys):
    worker_key, signing_key = keys
    env_a, opener_a = seal(worker_key, signing_key)
    env_b, opener_b = seal(worker_key, signing_key)
    _, sealer_a = open_request(env_a, worker_key)
    reply_a = sealer_a.seal(REPLY)
    assert opener_a.open(reply_a) == REPLY
    assert code_of(opener_b.open, reply_a) == "decrypt_failed"


def test_reply_bound_to_request_hash(keys):
    worker_key, signing_key = keys
    envelope, opener = seal(worker_key, signing_key)
    _, sealer = open_request(envelope, worker_key)
    # 同一把回覆公鑰，但 request_hash 不同（例如閘道調換回覆）
    other = ReplySealer(reply_key=sealer.reply_key, request_hash=hashlib.sha256(b"other").digest())
    assert code_of(opener.open, other.seal(REPLY)) == "decrypt_failed"


def test_reply_sealed_with_request_info_does_not_open(keys):
    worker_key, signing_key = keys
    envelope, opener = seal(worker_key, signing_key)
    _, sealer = open_request(envelope, worker_key)
    request_info = b"LLMOP/v1/hpke/request\x00" + hashlib.sha256(envelope.protected.encode()).digest()
    wrong = SUITE.encrypt(pad(json.dumps(REPLY).encode()), sealer.reply_key, request_info)
    assert code_of(opener.open, b64u_encode(wrong)) == "decrypt_failed"


def test_request_sealed_with_response_info_does_not_open(keys):
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    plaintext = json.dumps({"reply_key": b64u_encode(bytes(32)), "body": {}}).encode()
    response_info = b"LLMOP/v1/hpke/response\x00" + envelope.request_hash()
    sealed = b64u_encode(SUITE.encrypt(pad(plaintext), worker_key.public_key(), response_info))
    forged = resign(replace(envelope, sealed=sealed), signing_key)
    assert code_of(open_request, forged, worker_key) == "decrypt_failed"


def test_opener_rejects_garbage(keys):
    _, opener = seal(*keys)
    assert code_of(opener.open, "not b64u!") == "decrypt_failed"
    assert code_of(opener.open, b64u_encode(bytes(100))) == "decrypt_failed"


def test_two_seals_differ(keys):
    worker_key, signing_key = keys
    env_1, _ = seal(worker_key, signing_key)
    env_2, _ = seal(worker_key, signing_key)
    assert env_1.sealed != env_2.sealed
    assert env_1.header.request_id != env_2.header.request_id


# --- 請求明文格式 -------------------------------------------------------


def _forge_plaintext(worker_key, signing_key, plaintext: bytes) -> Envelope:
    """以合法的標頭與簽章，封裝任意明文（模擬寫錯的用戶端）。"""
    envelope, _ = seal(worker_key, signing_key)
    info = b"LLMOP/v1/hpke/request\x00" + hashlib.sha256(envelope.protected.encode()).digest()
    sealed = b64u_encode(SUITE.encrypt(pad(plaintext), worker_key.public_key(), info))
    return resign(replace(envelope, sealed=sealed), signing_key)


@pytest.mark.parametrize(
    "plaintext",
    [
        b"not json",
        b"[]",
        json.dumps({"body": {}}).encode(),
        json.dumps({"reply_key": b64u_encode(bytes(32))}).encode(),
        json.dumps({"reply_key": b64u_encode(bytes(31)), "body": {}}).encode(),
        json.dumps({"reply_key": 1, "body": {}}).encode(),
        json.dumps({"reply_key": b64u_encode(bytes(32)), "body": []}).encode(),
        json.dumps({"reply_key": b64u_encode(bytes(32)), "body": {}, "x": 1}).encode(),
    ],
)
def test_open_request_rejects_bad_plaintext(keys, plaintext):
    worker_key, signing_key = keys
    envelope = _forge_plaintext(worker_key, signing_key, plaintext)
    assert code_of(open_request, envelope, worker_key) == "bad_envelope"


def test_sealer_rejects_unusable_reply_key(keys):
    worker_key, signing_key = keys
    plaintext = json.dumps({"reply_key": b64u_encode(bytes(32)), "body": {}}).encode()
    envelope = _forge_plaintext(worker_key, signing_key, plaintext)
    _, sealer = open_request(envelope, worker_key)
    assert code_of(sealer.seal, REPLY) == "bad_envelope"


# --- 與 pyhpke（RFC 9180 參考實作）互通 ---------------------------------


def _pyhpke_suite():
    pyhpke = pytest.importorskip("pyhpke")
    suite = pyhpke.CipherSuite.new(
        pyhpke.KEMId.DHKEM_X25519_HKDF_SHA256, pyhpke.KDFId.HKDF_SHA256, pyhpke.AEADId.CHACHA20_POLY1305
    )
    return pyhpke, suite


def test_pyhpke_opens_sealed_request(keys):
    pyhpke, suite = _pyhpke_suite()
    worker_key, signing_key = keys
    envelope, _ = seal(worker_key, signing_key)
    raw = b64u_decode(envelope.sealed)
    enc, ct = raw[:32], raw[32:]
    info = b"LLMOP/v1/hpke/request\x00" + hashlib.sha256(envelope.protected.encode("ascii")).digest()
    skr = suite.kem.deserialize_private_key(private_raw(worker_key))
    plaintext = suite.create_recipient_context(enc, skr, info=info).open(ct, aad=b"")
    assert json.loads(plaintext)["body"] == BODY


def test_reply_opener_opens_pyhpke_reply(keys):
    pyhpke, suite = _pyhpke_suite()
    worker_key, signing_key = keys
    envelope, opener = seal(worker_key, signing_key)
    _, sealer = open_request(envelope, worker_key)
    pkr = suite.kem.deserialize_public_key(public_raw(sealer.reply_key))
    info = b"LLMOP/v1/hpke/response\x00" + envelope.request_hash()
    enc, ctx = suite.create_sender_context(pkr, info=info)
    ct = ctx.seal(pad(json.dumps(REPLY).encode()), aad=b"")
    assert opener.open(b64u_encode(enc + ct)) == REPLY
