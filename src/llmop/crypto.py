"""密碼運算：HPKE 封裝、Ed25519 簽章、金鑰 ID 與填充。

設計見 docs/SECURITY.md。規則：
- 每一次 HPKE 都是全新的單次 SUITE.encrypt / SUITE.decrypt，不快取、不重用任何東西。
- InvalidTag 與 InvalidSignature 一律轉成 ProtocolError，不外漏，也絕不換參數重試。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from llmop.protocol import (
    PAD_BLOCK,
    PROTOCOL_VERSION,
    REQUEST_TYP,
    Envelope,
    Header,
    ProtocolError,
    b64u_decode,
    b64u_encode,
    loads_strict,
    new_request_id,
)

SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)

_KEY_ID_X25519 = b"LLMOP/v1/key-id/x25519\x00"
_KEY_ID_ED25519 = b"LLMOP/v1/key-id/ed25519\x00"
_INFO_REQUEST = b"LLMOP/v1/hpke/request\x00"
_INFO_RESPONSE = b"LLMOP/v1/hpke/response\x00"

# ---------------------------------------------------------------------------
# 金鑰
# ---------------------------------------------------------------------------


def generate_worker_key() -> X25519PrivateKey:
    return X25519PrivateKey.generate()


def generate_signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_raw(key: X25519PrivateKey | X25519PublicKey | Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    """公鑰的原始 32 位元組。可傳私鑰或公鑰。"""
    if isinstance(key, (X25519PrivateKey, Ed25519PrivateKey)):
        key = key.public_key()
    return key.public_bytes_raw()


def private_raw(key: X25519PrivateKey | Ed25519PrivateKey) -> bytes:
    """私鑰的原始 32 位元組。"""
    return key.private_bytes_raw()


def load_worker_private(raw: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(raw)


def load_worker_public(raw: bytes) -> X25519PublicKey:
    return X25519PublicKey.from_public_bytes(raw)


def load_signing_private(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def load_signing_public(raw: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(raw)


def _key_id(prefix: bytes, raw: bytes) -> str:
    if len(raw) != 32:
        raise ValueError("公鑰必須是 32 位元組")
    return b64u_encode(hashlib.sha256(prefix + raw).digest()[:16])


def worker_key_id(public_raw: bytes) -> str:
    """worker 公鑰的 key_id，也是人工比對用的指紋。"""
    return _key_id(_KEY_ID_X25519, public_raw)


def signing_key_id(public_raw: bytes) -> str:
    """用戶端簽章公鑰的 sig_key_id。"""
    return _key_id(_KEY_ID_ED25519, public_raw)


# ---------------------------------------------------------------------------
# 填充與 info 字串
# ---------------------------------------------------------------------------


def pad(data: bytes) -> bytes:
    """在結尾補 ASCII 空白到 PAD_BLOCK 的倍數。JSON 允許結尾空白，所以收方不必去填充。"""
    return data + b" " * (-len(data) % PAD_BLOCK)


def _dumps(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _info_request(protected: str) -> bytes:
    return _INFO_REQUEST + hashlib.sha256(protected.encode("ascii")).digest()


def _info_response(request_hash: bytes) -> bytes:
    return _INFO_RESPONSE + request_hash


# ---------------------------------------------------------------------------
# 用戶端：封裝請求、開啟回覆
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplyOpener:
    """持有一次性回覆私鑰與 request_hash，只用來開啟這一筆請求的回覆。

    私鑰只存在記憶體；用完後呼叫端應丟掉這個物件的參照，絕不寫入磁碟。
    """

    reply_key: X25519PrivateKey = field(repr=False)
    request_hash: bytes

    def open(self, sealed: str) -> dict:
        """解開回覆並回傳明文物件（{"ok": true, "body": …} 或 {"ok": false, "error": …}）。

        解不開（不是 worker 發的，或不是這筆請求的回覆）丟 decrypt_failed；
        解開後格式不符丟 bad_envelope。
        """
        try:
            plaintext = SUITE.decrypt(b64u_decode(sealed), self.reply_key, _info_response(self.request_hash))
        except (ProtocolError, InvalidTag, ValueError):
            raise ProtocolError("decrypt_failed", "無法解開回覆") from None
        reply = loads_strict(plaintext)
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            raise ProtocolError("bad_envelope", "回覆明文格式錯誤")
        if reply["ok"]:
            if reply.keys() != {"ok", "body"} or not isinstance(reply["body"], dict):
                raise ProtocolError("bad_envelope", "回覆明文格式錯誤")
        else:
            err = reply.get("error")
            if (
                reply.keys() != {"ok", "error"}
                or not isinstance(err, dict)
                or err.keys() != {"code", "message"}
                or not isinstance(err["code"], str)
                or not isinstance(err["message"], str)
            ):
                raise ProtocolError("bad_envelope", "回覆明文格式錯誤")
        return reply


def seal_request(
    body: dict,
    *,
    model: str,
    max_tokens: int,
    worker_public: bytes,
    signing_key: Ed25519PrivateKey,
    now: int | None = None,
) -> tuple[Envelope, ReplyOpener]:
    """封裝並簽署一筆請求。回傳信封與只能開啟這筆回覆的 ReplyOpener。

    worker_public 必須是用戶端已釘選的 worker 公鑰（原始 32 位元組）。
    """
    reply_key = X25519PrivateKey.generate()
    header = Header(
        v=PROTOCOL_VERSION,
        typ=REQUEST_TYP,
        request_id=new_request_id(),
        ts=int(time.time()) if now is None else now,
        key_id=worker_key_id(worker_public),
        sig_key_id=signing_key_id(public_raw(signing_key)),
        model=model,
        max_tokens=max_tokens,
    )
    protected = header.encode()
    plaintext = pad(_dumps({"reply_key": b64u_encode(public_raw(reply_key)), "body": body}))
    sealed = b64u_encode(SUITE.encrypt(plaintext, load_worker_public(worker_public), _info_request(protected)))
    # signing_input 不含 sig，所以先放一個佔位簽章，簽完再換上真的。
    unsigned = Envelope(protected=protected, sealed=sealed, sig=b64u_encode(bytes(64)))
    envelope = replace(unsigned, sig=b64u_encode(signing_key.sign(unsigned.signing_input())))
    return envelope, ReplyOpener(reply_key=reply_key, request_hash=envelope.request_hash())


# ---------------------------------------------------------------------------
# worker：驗簽、開啟請求、封裝回覆
# ---------------------------------------------------------------------------


def verify_request(envelope: Envelope, client_sig_pub: bytes) -> Header:
    """確認 sig_key_id 對得上 client_sig_pub，且簽章有效。不檢查時間。

    失敗丟 bad_signature。閘道與 worker 都要呼叫；worker 不依賴閘道的檢查結果。
    """
    header = envelope.header
    if len(client_sig_pub) != 32 or signing_key_id(client_sig_pub) != header.sig_key_id:
        raise ProtocolError("bad_signature", "簽章公鑰與標頭的 sig_key_id 不符")
    try:
        load_signing_public(client_sig_pub).verify(b64u_decode(envelope.sig), envelope.signing_input())
    except (InvalidSignature, ValueError):
        raise ProtocolError("bad_signature", "簽章驗證失敗") from None
    return header


@dataclass(frozen=True)
class ReplySealer:
    """持有用戶端的一次性回覆公鑰與 request_hash，只能為這一筆請求封裝回覆。"""

    reply_key: X25519PublicKey
    request_hash: bytes

    def seal(self, payload: dict) -> str:
        """把回覆明文（{"ok": …}）填充後封裝，回傳 b64u。"""
        try:
            sealed = SUITE.encrypt(pad(_dumps(payload)), self.reply_key, _info_response(self.request_hash))
        except ValueError:
            # 回覆公鑰是低階點之類無法使用的值：只有送出請求的用戶端自己會受影響。
            raise ProtocolError("bad_envelope", "用戶端提供的回覆公鑰無法使用") from None
        return b64u_encode(sealed)


def open_request(envelope: Envelope, worker_key: X25519PrivateKey) -> tuple[dict, ReplySealer]:
    """解開請求，回傳 (body, ReplySealer)。

    呼叫前必須已經用 verify_request 驗過簽章：未經驗證的密文不得進入解密流程。
    worker_key 是依 header.key_id 選出的 worker 私鑰。
    解不開丟 decrypt_failed；明文格式錯誤或 reply_key 不是 32 位元組丟 bad_envelope。
    """
    try:
        plaintext = SUITE.decrypt(b64u_decode(envelope.sealed), worker_key, _info_request(envelope.protected))
    except (InvalidTag, ValueError):
        raise ProtocolError("decrypt_failed", "無法解開請求") from None
    obj = loads_strict(plaintext)
    if not isinstance(obj, dict) or obj.keys() != {"reply_key", "body"} or not isinstance(obj["body"], dict):
        raise ProtocolError("bad_envelope", "請求明文格式錯誤")
    reply_raw = b64u_decode(obj["reply_key"]) if isinstance(obj["reply_key"], str) else b""
    if len(reply_raw) != 32:
        raise ProtocolError("bad_envelope", "reply_key 必須是 32 位元組")
    return obj["body"], ReplySealer(reply_key=load_worker_public(reply_raw), request_hash=envelope.request_hash())
