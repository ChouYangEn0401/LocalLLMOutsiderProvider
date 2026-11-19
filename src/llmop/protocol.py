"""線路協定（第 1 版）：常數、b64u、錯誤碼、Header、Envelope 與 WebSocket 訊框。

完整規格見 docs/PROTOCOL.md。本模組只做格式與型別的嚴格檢查，不碰任何金鑰；
密碼運算在 llmop.crypto。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, fields

# ---------------------------------------------------------------------------
# 上限與預設值
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 16 * 1024 * 1024  # HTTP 本文與單一 WebSocket 訊息的上限
MAX_MODEL_LEN = 200  # model 名稱最多幾個字元
MAX_TOKENS_RANGE = (1, 1_000_000)  # max_tokens 的允許範圍（含兩端）
CLOCK_SKEW_SECONDS = 300
PAD_BLOCK = 1024
SYNC_WAIT_SECONDS = 80  # 預設值
REQUEST_TIMEOUT_SECONDS = 1800  # 預設值
RESULT_TTL_SECONDS = 600  # 完成後結果保留的秒數
MAX_INFLIGHT = 150  # 預設值
WS_HEARTBEAT_SECONDS = 25
WS_FIRST_FRAME_SECONDS = 10

PROTOCOL_VERSION = 1
REQUEST_TYP = "llmop-request"

# ---------------------------------------------------------------------------
# 錯誤
# ---------------------------------------------------------------------------

HTTP_STATUS: dict[str, int] = {
    "bad_envelope": 400,
    "unsupported_version": 400,
    "payload_too_large": 413,
    "unauthorized": 401,
    "bad_signature": 403,
    "untrusted_client": 403,
    "stale_timestamp": 400,
    "duplicate_request": 409,
    "replayed": 409,
    "quota_exceeded": 429,
    "unknown_key_id": 404,
    "model_unavailable": 404,
    "worker_busy": 503,
    "gateway_busy": 503,
    "worker_disconnected": 502,
    "decrypt_failed": 400,
    "timeout": 504,
    "unknown_request": 404,
    "internal_error": 500,
}

# 握手失敗時 error 訊框可用的錯誤碼。key_conflict 只出現在握手，沒有對應的 HTTP 狀態。
_HANDSHAKE_ERROR_CODES = frozenset({"unauthorized", "unsupported_version", "bad_envelope", "key_conflict"})


class ProtocolError(Exception):
    """協定層錯誤。code 是契約（見 HTTP_STATUS），message 是給人看的繁體中文說明。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


def _bad(message: str) -> ProtocolError:
    return ProtocolError("bad_envelope", message)


# ---------------------------------------------------------------------------
# b64u：base64url、不補 '='、嚴格解碼
# ---------------------------------------------------------------------------

_B64U_RE = re.compile(r"[A-Za-z0-9_-]*")


def b64u_encode(data: bytes) -> str:
    """base64url 編碼，去掉結尾的 '='。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """嚴格的 base64url 解碼：拒絕 '='、字母表以外的字元，以及長度除以 4 餘 1。

    標準函式庫的 urlsafe_b64decode 很寬鬆，所以這裡自己先檢查。
    """
    if not isinstance(text, str) or not _B64U_RE.fullmatch(text):
        raise _bad("b64u 字串含有不允許的字元")
    if len(text) % 4 == 1:
        raise _bad("b64u 字串長度不合法")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _bad("b64u 解碼失敗") from exc
    # 拒絕非正規編碼（最後一個字元帶有多餘位元），確保每段資料只有一種寫法。
    if b64u_encode(data) != text:
        raise _bad("b64u 字串不是正規編碼")
    return data


# ---------------------------------------------------------------------------
# 嚴格 JSON 與型別檢查的小工具
# ---------------------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise _bad(f"JSON 物件有重複欄位 {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> object:
    raise _bad(f"JSON 不允許 {name}")


def loads_strict(data: str | bytes) -> object:
    """解析 JSON；拒絕重複欄位與 NaN/Infinity。失敗時丟 bad_envelope。"""
    try:
        return json.loads(data, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:  # 巢狀過深也是格式錯誤
        raise _bad("JSON 格式錯誤") from exc


def _check_keys(obj: object, required: set[str], what: str) -> dict:
    if not isinstance(obj, dict):
        raise _bad(f"{what} 必須是 JSON 物件")
    missing = required - obj.keys()
    if missing:
        raise _bad(f"{what} 缺少欄位：{', '.join(sorted(missing))}")
    extra = obj.keys() - required
    if extra:
        raise _bad(f"{what} 有不認得的欄位：{', '.join(sorted(extra))}")
    return obj


def _is_int(value: object) -> bool:
    # bool 是 int 的子類別，必須排除。
    return type(value) is int


def _check_int(value: object, name: str, minimum: int = 0) -> None:
    if not _is_int(value) or value < minimum:
        raise _bad(f"{name} 必須是 >= {minimum} 的整數")


def _check_str(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise _bad(f"{name} 必須是非空字串")


def _check_b64u_len(value: object, name: str, length: int) -> None:
    _check_str(value, name)
    if len(b64u_decode(value)) != length:
        raise _bad(f"{name} 必須是 {length} 位元組的 b64u")


def _check_model(value: object, name: str = "model") -> None:
    _check_str(value, name)
    if len(value) > MAX_MODEL_LEN:
        raise _bad(f"{name} 超過 {MAX_MODEL_LEN} 個字元")


def _check_version(value: object, name: str = "v") -> None:
    if not _is_int(value):
        raise _bad(f"{name} 必須是整數")
    if value != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_version", f"不支援的協定版本 {value}")


# ---------------------------------------------------------------------------
# Header 與 Envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    """請求的明文中繼資料（protected）。建立時即做完整的型別與範圍檢查。"""

    v: int
    typ: str
    request_id: str
    ts: int
    key_id: str
    sig_key_id: str
    model: str
    max_tokens: int

    def __post_init__(self) -> None:
        _check_version(self.v)
        if self.typ != REQUEST_TYP:
            raise _bad(f"typ 必須是 {REQUEST_TYP!r}")
        _check_request_id(self.request_id)
        _check_int(self.ts, "ts")
        _check_b64u_len(self.key_id, "key_id", 16)
        _check_b64u_len(self.sig_key_id, "sig_key_id", 16)
        _check_model(self.model)
        low, high = MAX_TOKENS_RANGE
        if not _is_int(self.max_tokens) or not low <= self.max_tokens <= high:
            raise _bad(f"max_tokens 必須是 {low} 到 {high} 的整數")

    @classmethod
    def parse(cls, protected: str) -> Header:
        """解析 protected 字串。缺欄位、多欄位、型別錯誤、超出範圍都會被拒絕。"""
        obj = loads_strict(b64u_decode(protected))
        # 版本不對時優先回報 unsupported_version：其他版本的欄位本來就可能不同。
        if isinstance(obj, dict) and "v" in obj:
            _check_version(obj["v"])
        _check_keys(obj, {f.name for f in fields(cls)}, "header")
        return cls(**obj)

    def encode(self) -> str:
        """編碼成 protected：緊湊的 UTF-8 JSON，再做 b64u。"""
        obj = {f.name: getattr(self, f.name) for f in fields(self)}
        text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        return b64u_encode(text.encode("utf-8"))


_SIG_PREFIX = b"LLMOP/v1/sig/request\x00"


@dataclass(frozen=True)
class Envelope:
    """請求信封。protected、sealed、sig 都是傳輸時的 b64u 字串原樣。"""

    protected: str
    sealed: str
    sig: str

    def __post_init__(self) -> None:
        _check_str(self.protected, "protected")
        _check_sealed(self.sealed)
        _check_b64u_len(self.sig, "sig", 64)
        Header.parse(self.protected)

    @classmethod
    def from_json(cls, obj: object) -> Envelope:
        """從已解析的 JSON 物件建立信封；多欄位或缺欄位都會被拒絕。"""
        return cls(**_check_keys(obj, {"protected", "sealed", "sig"}, "envelope"))

    def to_json(self) -> dict:
        return {"protected": self.protected, "sealed": self.sealed, "sig": self.sig}

    @property
    def header(self) -> Header:
        return Header.parse(self.protected)

    def signing_input(self) -> bytes:
        """簽章輸入："LLMOP/v1/sig/request" ‖ 0x00 ‖ protected ‖ "." ‖ sealed。"""
        return _SIG_PREFIX + self.protected.encode("ascii") + b"." + self.sealed.encode("ascii")

    def request_hash(self) -> bytes:
        """SHA-256(signing_input)，用來把回覆綁定到這一筆請求。"""
        return hashlib.sha256(self.signing_input()).digest()


def check_timestamp(ts: int, now: int, skew: int = CLOCK_SKEW_SECONDS) -> None:
    """|now − ts| 超過 skew 秒就丟 stale_timestamp。"""
    if abs(now - ts) > skew:
        raise ProtocolError("stale_timestamp", f"時間戳與本機時鐘相差超過 {skew} 秒")


def new_request_id() -> str:
    """16 個隨機位元組的 b64u（22 個字元）。"""
    return b64u_encode(secrets.token_bytes(16))


# ---------------------------------------------------------------------------
# WebSocket 訊框
# ---------------------------------------------------------------------------


def _check_request_id(value: object, name: str = "request_id") -> None:
    _check_b64u_len(value, name, 16)


def _check_sealed(value: object, name: str = "sealed") -> None:
    _check_str(value, name)
    # enc（32 位元組）+ AEAD 標籤（16 位元組）是密文的最小長度。
    if len(b64u_decode(value)) < 48:
        raise _bad(f"{name} 太短")


def _check_model_list(value: object, name: str) -> None:
    if not isinstance(value, list):
        raise _bad(f"{name} 必須是陣列")
    for item in value:
        _check_model(item, f"{name} 的項目")


def _check_key_list(value: object, name: str) -> None:
    if not isinstance(value, list) or not value:
        raise _bad(f"{name} 必須是非空陣列")
    for item in value:
        _check_keys(item, {"key_id", "public_key"}, f"{name} 的項目")
        _check_b64u_len(item["key_id"], "key_id", 16)
        _check_b64u_len(item["public_key"], "public_key", 32)


def _check_concurrency(value: object, name: str) -> None:
    _check_int(value, name, 1)


def _check_envelope(value: object, name: str) -> None:
    Envelope.from_json(value)


def _check_client_sig_pub(value: object, name: str) -> None:
    _check_b64u_len(value, name, 32)


def _check_usage(value: object, name: str) -> None:
    _check_keys(value, {"prompt_tokens", "completion_tokens"}, name)
    _check_int(value["prompt_tokens"], "prompt_tokens")
    _check_int(value["completion_tokens"], "completion_tokens")


def _check_ok(value: object, name: str) -> None:
    if type(value) is not bool:
        raise _bad(f"{name} 必須是布林值")


def _check_response_code(value: object, name: str) -> None:
    if not isinstance(value, str) or value not in HTTP_STATUS:
        raise _bad(f"{name} 必須是協定定義的錯誤碼")


def _check_handshake_code(value: object, name: str) -> None:
    if not isinstance(value, str) or value not in _HANDSHAKE_ERROR_CODES:
        raise _bad(f"{name} 必須是握手錯誤碼")


# 每種訊框的欄位與檢查函式（value, name）。response 依 ok 分成兩種形狀。
_FRAME_CHECKS = {
    "register": {"v": _check_version, "reg_token": _check_str},
    "registered": {"worker_id": _check_str, "worker_token": _check_str},
    "hello": {
        "v": _check_version,
        "worker_id": _check_str,
        "worker_token": _check_str,
        "keys": _check_key_list,
        "models": _check_model_list,
        "max_concurrency": _check_concurrency,
    },
    "ready": {},
    "error": {"code": _check_handshake_code},
    "models": {"models": _check_model_list},
    "request": {
        "request_id": _check_request_id,
        "envelope": _check_envelope,
        "client_sig_pub": _check_client_sig_pub,
    },
}
_RESPONSE_OK_CHECKS = {"request_id": _check_request_id, "ok": _check_ok, "sealed": _check_sealed, "usage": _check_usage}
_RESPONSE_ERROR_CHECKS = {"request_id": _check_request_id, "ok": _check_ok, "error": _check_response_code}


def parse_frame(text: str) -> dict:
    """解析並嚴格檢查一個 WebSocket 文字訊框，回傳 dict。

    格式錯誤丟 bad_envelope；register/hello 的 v 不是 1 則丟 unsupported_version。
    request 訊框的 request_id 必須與信封標頭裡的一致。
    """
    obj = loads_strict(text)
    if not isinstance(obj, dict):
        raise _bad("訊框必須是 JSON 物件")
    frame_type = obj.get("type")
    if frame_type == "response":
        checks = _RESPONSE_OK_CHECKS if obj.get("ok") is True else _RESPONSE_ERROR_CHECKS
    elif isinstance(frame_type, str) and frame_type in _FRAME_CHECKS:
        checks = _FRAME_CHECKS[frame_type]
    else:
        raise _bad("不認得的訊框類型")
    if "v" in checks and "v" in obj:
        _check_version(obj["v"])
    _check_keys(obj, {"type", *checks}, f"{frame_type} 訊框")
    for name, check in checks.items():
        check(obj[name], name)
    if frame_type == "request" and Header.parse(obj["envelope"]["protected"]).request_id != obj["request_id"]:
        raise _bad("request_id 與信封標頭不一致")
    return obj


def register(reg_token: str) -> dict:
    return {"type": "register", "v": PROTOCOL_VERSION, "reg_token": reg_token}


def registered(worker_id: str, worker_token: str) -> dict:
    return {"type": "registered", "worker_id": worker_id, "worker_token": worker_token}


def hello(worker_id: str, worker_token: str, keys: list[dict], models: list[str], max_concurrency: int) -> dict:
    """keys 的每一項是 {"key_id": ..., "public_key": ...}，第一項是目前的金鑰。"""
    return {
        "type": "hello",
        "v": PROTOCOL_VERSION,
        "worker_id": worker_id,
        "worker_token": worker_token,
        "keys": [{"key_id": k["key_id"], "public_key": k["public_key"]} for k in keys],
        "models": list(models),
        "max_concurrency": max_concurrency,
    }


def ready() -> dict:
    return {"type": "ready"}


def error(code: str) -> dict:
    return {"type": "error", "code": code}


def models(models: list[str]) -> dict:
    return {"type": "models", "models": list(models)}


def request(request_id: str, envelope: Envelope, client_sig_pub: str) -> dict:
    """client_sig_pub 是該 API 金鑰登記的 Ed25519 公鑰（b64u）。"""
    return {
        "type": "request",
        "request_id": request_id,
        "envelope": envelope.to_json(),
        "client_sig_pub": client_sig_pub,
    }


def response_ok(request_id: str, sealed: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """封裝好的回覆（可能本身是封裝起來的錯誤，此時 token 數為 0）。"""
    return {
        "type": "response",
        "request_id": request_id,
        "ok": True,
        "sealed": sealed,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def response_error(request_id: str, code: str) -> dict:
    """只帶裸錯誤碼，絕不夾帶後端文字或請求內容。"""
    return {"type": "response", "request_id": request_id, "ok": False, "error": code}
