"""線路協定的格式檢查測試。"""

import base64
import hashlib
import json

import pytest

from llmop import protocol
from llmop.protocol import (
    CLOCK_SKEW_SECONDS,
    HTTP_STATUS,
    Envelope,
    Header,
    ProtocolError,
    b64u_decode,
    b64u_encode,
    check_timestamp,
    new_request_id,
    parse_frame,
)

KEY_ID = b64u_encode(bytes(range(16)))
SIG_KEY_ID = b64u_encode(bytes(range(16, 32)))
REQUEST_ID = b64u_encode(b"0123456789abcdef")


def header_obj(**overrides):
    obj = {
        "v": 1,
        "typ": "llmop-request",
        "request_id": REQUEST_ID,
        "ts": 1795000000,
        "key_id": KEY_ID,
        "sig_key_id": SIG_KEY_ID,
        "model": "qwen3:8b",
        "max_tokens": 1024,
    }
    obj.update(overrides)
    return obj


def encode_obj(obj):
    return b64u_encode(json.dumps(obj).encode("utf-8"))


def make_envelope(**header_overrides):
    return Envelope(
        protected=encode_obj(header_obj(**header_overrides)),
        sealed=b64u_encode(bytes(48 + 1024)),
        sig=b64u_encode(bytes(64)),
    )


def assert_code(code, fn, *args):
    with pytest.raises(ProtocolError) as info:
        fn(*args)
    assert info.value.code == code
    return info.value


# --- b64u ---------------------------------------------------------------


@pytest.mark.parametrize("length", range(0, 40))
def test_b64u_round_trip(length):
    data = bytes((i * 37 + 11) % 256 for i in range(length))
    text = b64u_encode(data)
    assert "=" not in text
    assert b64u_decode(text) == data


def test_b64u_uses_url_alphabet():
    assert b64u_encode(b"\xfb\xff") == "-_8"


@pytest.mark.parametrize(
    "text",
    ["AAA=", "AA==", "+AAA", "/AAA", "AA AA", "AAAA\n", " AAAA", "AAAAA", "A", "-_8\t", "AAé"],
)
def test_b64u_decoder_rejects(text):
    err = assert_code("bad_envelope", b64u_decode, text)
    assert err.message


def test_b64u_rejects_non_canonical_trailing_bits():
    assert b64u_decode("AA") == b"\x00"
    assert_code("bad_envelope", b64u_decode, "AB")


# --- 常數與錯誤 --------------------------------------------------------


def test_limits():
    assert protocol.MAX_BODY_BYTES == 16 * 1024 * 1024
    assert protocol.MAX_MODEL_LEN == 200
    assert protocol.MAX_TOKENS_RANGE == (1, 1_000_000)
    assert protocol.CLOCK_SKEW_SECONDS == 300
    assert protocol.PAD_BLOCK == 1024
    assert protocol.SYNC_WAIT_SECONDS == 80
    assert protocol.REQUEST_TIMEOUT_SECONDS == 1800
    assert protocol.RESULT_TTL_SECONDS == 600
    assert protocol.MAX_INFLIGHT == 150
    assert protocol.WS_HEARTBEAT_SECONDS == 25
    assert protocol.WS_FIRST_FRAME_SECONDS == 10


def test_http_status_table():
    assert len(HTTP_STATUS) == 19
    assert HTTP_STATUS["bad_envelope"] == 400
    assert HTTP_STATUS["payload_too_large"] == 413
    assert HTTP_STATUS["unauthorized"] == 401
    assert HTTP_STATUS["untrusted_client"] == 403
    assert HTTP_STATUS["duplicate_request"] == 409
    assert HTTP_STATUS["quota_exceeded"] == 429
    assert HTTP_STATUS["worker_disconnected"] == 502
    assert HTTP_STATUS["gateway_busy"] == 503
    assert HTTP_STATUS["timeout"] == 504
    assert HTTP_STATUS["internal_error"] == 500


def test_protocol_error_attributes():
    err = ProtocolError("replayed", "重複的請求")
    assert err.code == "replayed"
    assert err.message == "重複的請求"


def test_new_request_id():
    a, b = new_request_id(), new_request_id()
    assert a != b
    assert len(a) == 22
    assert len(b64u_decode(a)) == 16


# --- Header -------------------------------------------------------------


def test_header_parse_and_encode_round_trip():
    header = Header.parse(encode_obj(header_obj()))
    assert header.model == "qwen3:8b"
    assert header.max_tokens == 1024
    assert Header.parse(header.encode()) == header
    # encode 產生緊湊 JSON
    assert b" " not in b64u_decode(header.encode())


@pytest.mark.parametrize("field", list(header_obj()))
def test_header_rejects_missing_field(field):
    obj = header_obj()
    del obj[field]
    assert_code("bad_envelope", Header.parse, encode_obj(obj))


def test_header_rejects_extra_field():
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(extra="x")))


WRONG_TYPES = {
    "v": ["1", 1.0, True, None],
    "typ": [1, None, ["llmop-request"]],
    "request_id": [1, None, {}],
    "ts": ["1795000000", 1795000000.0, True, None],
    "key_id": [1, None, []],
    "sig_key_id": [1, None, []],
    "model": [1, None, ["qwen3:8b"]],
    "max_tokens": ["1024", 1024.0, True, None],
}


@pytest.mark.parametrize(
    "field,value", [(field, value) for field, values in WRONG_TYPES.items() for value in values]
)
def test_header_rejects_wrong_type(field, value):
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(**{field: value})))


def test_header_wrong_types_cover_every_field():
    assert set(WRONG_TYPES) == set(header_obj())


@pytest.mark.parametrize("v", [0, 2])
def test_header_rejects_other_versions(v):
    assert_code("unsupported_version", Header.parse, encode_obj(header_obj(v=v)))


def test_header_unsupported_version_wins_over_unknown_fields():
    obj = {"v": 2, "something": "new"}
    assert_code("unsupported_version", Header.parse, encode_obj(obj))


def test_header_rejects_wrong_typ():
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(typ="llmop-response")))


@pytest.mark.parametrize("max_tokens", [0, 1_000_001, -1])
def test_header_rejects_max_tokens_out_of_range(max_tokens):
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(max_tokens=max_tokens)))


def test_header_accepts_max_tokens_bounds():
    assert Header.parse(encode_obj(header_obj(max_tokens=1))).max_tokens == 1
    assert Header.parse(encode_obj(header_obj(max_tokens=1_000_000))).max_tokens == 1_000_000


def test_header_model_length():
    assert Header.parse(encode_obj(header_obj(model="m" * 200))).model == "m" * 200
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(model="m" * 201)))
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(model="")))


@pytest.mark.parametrize("field", ["request_id", "key_id", "sig_key_id"])
def test_header_rejects_malformed_identifiers(field):
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(**{field: "short"})))
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(**{field: KEY_ID + "="})))


def test_header_rejects_negative_ts():
    assert_code("bad_envelope", Header.parse, encode_obj(header_obj(ts=-1)))


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"not json",
        b'{"v":1,"v":1}',
        b"\xff\xfe",
        json.dumps(header_obj()).replace("1024", "NaN").encode(),
    ],
)
def test_header_rejects_bad_json(raw):
    assert_code("bad_envelope", Header.parse, b64u_encode(raw))


def test_header_rejects_non_b64u_protected():
    protected = encode_obj(header_obj())
    assert_code("bad_envelope", Header.parse, protected + "=")
    assert_code("bad_envelope", Header.parse, protected.replace("e", "+", 1))
    assert_code("bad_envelope", Header.parse, base64.b64encode(json.dumps(header_obj(model="qwen?>")).encode()).decode())


# --- Envelope -----------------------------------------------------------


def test_envelope_round_trip():
    env = make_envelope()
    again = Envelope.from_json(env.to_json())
    assert again == env
    assert again.header.request_id == REQUEST_ID


@pytest.mark.parametrize("field", ["protected", "sealed", "sig"])
def test_envelope_rejects_missing_field(field):
    obj = make_envelope().to_json()
    del obj[field]
    assert_code("bad_envelope", Envelope.from_json, obj)


def test_envelope_rejects_extra_field():
    obj = make_envelope().to_json()
    obj["kid"] = "x"
    assert_code("bad_envelope", Envelope.from_json, obj)


@pytest.mark.parametrize("field", ["protected", "sealed", "sig"])
def test_envelope_rejects_wrong_type(field):
    obj = make_envelope().to_json()
    obj[field] = 1
    assert_code("bad_envelope", Envelope.from_json, obj)


def test_envelope_rejects_non_object():
    assert_code("bad_envelope", Envelope.from_json, ["a", "b", "c"])


def test_envelope_rejects_bad_sig_and_sealed_lengths():
    obj = make_envelope().to_json()
    assert_code("bad_envelope", Envelope.from_json, {**obj, "sig": b64u_encode(bytes(63))})
    assert_code("bad_envelope", Envelope.from_json, {**obj, "sealed": b64u_encode(bytes(47))})


def test_envelope_parses_header():
    obj = make_envelope().to_json()
    assert_code("unsupported_version", Envelope.from_json, {**obj, "protected": encode_obj(header_obj(v=2))})


def test_signing_input_and_request_hash_match_formulas():
    env = make_envelope()
    expected = b"LLMOP/v1/sig/request" + b"\x00" + env.protected.encode("ascii") + b"." + env.sealed.encode("ascii")
    assert env.signing_input() == expected
    assert env.request_hash() == hashlib.sha256(expected).digest()


# --- 時間 ---------------------------------------------------------------


def test_check_timestamp():
    now = 1_795_000_000
    for delta in (-CLOCK_SKEW_SECONDS, 0, CLOCK_SKEW_SECONDS):
        check_timestamp(now + delta, now)
    for delta in (-CLOCK_SKEW_SECONDS - 1, CLOCK_SKEW_SECONDS + 1):
        assert_code("stale_timestamp", check_timestamp, now + delta, now)


def test_check_timestamp_custom_skew():
    check_timestamp(110, 100, skew=10)
    assert_code("stale_timestamp", check_timestamp, 111, 100, 10)


# --- WebSocket 訊框 ------------------------------------------------------

PUBLIC_KEY = b64u_encode(bytes(range(32)))


def valid_frames():
    env = make_envelope()
    return {
        "register": protocol.register("llmop_reg_abc"),
        "registered": protocol.registered("w_3f9a00000000", "llmop_wk_abc"),
        "hello": protocol.hello(
            "w_3f9a00000000",
            "llmop_wk_abc",
            [{"key_id": KEY_ID, "public_key": PUBLIC_KEY}],
            ["qwen3:8b", "llama-3.3-70b"],
            2,
        ),
        "ready": protocol.ready(),
        "error": protocol.error("key_conflict"),
        "models": protocol.models(["qwen3:8b"]),
        "request": protocol.request(REQUEST_ID, env, PUBLIC_KEY),
        "response_ok": protocol.response_ok(REQUEST_ID, b64u_encode(bytes(48 + 1024)), 812, 240),
        "response_error": protocol.response_error(REQUEST_ID, "decrypt_failed"),
    }


@pytest.mark.parametrize("name", list(valid_frames()))
def test_parse_frame_accepts_builders(name):
    frame = valid_frames()[name]
    assert parse_frame(json.dumps(frame)) == frame


def test_frame_builder_shapes():
    frames = valid_frames()
    assert frames["register"] == {"type": "register", "v": 1, "reg_token": "llmop_reg_abc"}
    assert frames["ready"] == {"type": "ready"}
    assert frames["response_ok"]["usage"] == {"prompt_tokens": 812, "completion_tokens": 240}
    assert frames["response_ok"]["ok"] is True
    assert frames["response_error"] == {
        "type": "response",
        "request_id": REQUEST_ID,
        "ok": False,
        "error": "decrypt_failed",
    }
    assert frames["request"]["envelope"] == make_envelope().to_json()


def test_parse_frame_rejects_unknown_type():
    assert_code("bad_envelope", parse_frame, json.dumps({"type": "hack"}))
    assert_code("bad_envelope", parse_frame, json.dumps({"models": []}))
    assert_code("bad_envelope", parse_frame, json.dumps(["ready"]))


@pytest.mark.parametrize("name", list(valid_frames()))
def test_parse_frame_rejects_extra_field(name):
    frame = {**valid_frames()[name], "extra": 1}
    assert_code("bad_envelope", parse_frame, json.dumps(frame))


@pytest.mark.parametrize(
    "name,field",
    [(name, field) for name, frame in valid_frames().items() for field in frame if field != "type"],
)
def test_parse_frame_rejects_missing_field(name, field):
    frame = dict(valid_frames()[name])
    del frame[field]
    with pytest.raises(ProtocolError) as info:
        parse_frame(json.dumps(frame))
    assert info.value.code == "bad_envelope"


def test_parse_frame_version():
    frame = {**valid_frames()["hello"], "v": 2}
    assert_code("unsupported_version", parse_frame, json.dumps(frame))
    frame = {**valid_frames()["register"], "v": "1"}
    assert_code("bad_envelope", parse_frame, json.dumps(frame))


def test_parse_frame_field_checks():
    frames = valid_frames()
    bad = [
        {**frames["hello"], "keys": []},
        {**frames["hello"], "keys": [{"key_id": KEY_ID}]},
        {**frames["hello"], "max_concurrency": 0},
        {**frames["hello"], "max_concurrency": True},
        {**frames["models"], "models": "qwen3:8b"},
        {**frames["models"], "models": ["m" * 201]},
        {**frames["response_ok"], "usage": {"prompt_tokens": 1}},
        {**frames["response_ok"], "usage": {"prompt_tokens": -1, "completion_tokens": 0}},
        {**frames["response_ok"], "ok": "true"},
        {**frames["response_error"], "error": "backend said: secret prompt"},
        {**frames["error"], "code": "something_else"},
        {**frames["error"], "code": ["unauthorized"]},
        {**frames["response_error"], "error": ["timeout"]},
        {**frames["request"], "request_id": new_request_id()},
        {**frames["request"], "client_sig_pub": b64u_encode(bytes(31))},
        {**frames["request"], "envelope": {**make_envelope().to_json(), "x": 1}},
    ]
    for frame in bad:
        assert_code("bad_envelope", parse_frame, json.dumps(frame))


def test_parse_frame_rejects_bad_json():
    assert_code("bad_envelope", parse_frame, "{")
    assert_code("bad_envelope", parse_frame, '{"type":"ready","type":"ready"}')
