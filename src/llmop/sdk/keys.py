"""用戶端的 Ed25519 簽章金鑰：在這台電腦產生、存在設定檔資料夾、永遠不上傳私鑰。

檔案格式跟 v0.2 的 ``llmop client keygen`` 相同（private_key、public_key、sig_key_id，都是 b64u）。
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from llmop import crypto, paths
from llmop.protocol import ProtocolError, b64u_decode, b64u_encode

from .errors import SdkError
from .perms import restrict


def key_info(key: Ed25519PrivateKey) -> tuple[str, str]:
    """(公鑰 b64u, sig_key_id)。"""
    raw = crypto.public_raw(key)
    return b64u_encode(raw), crypto.signing_key_id(raw)


def load(path: Path) -> Ed25519PrivateKey:
    if not path.exists():
        raise SdkError("profile_incomplete", "這個設定檔沒有簽章金鑰（加密模式需要；請重新 enroll）")
    try:
        data = paths.read_json(path)
        key = crypto.load_signing_private(b64u_decode(data["private_key"]))
    except (ValueError, KeyError, TypeError, ProtocolError):
        raise SdkError("bad_request", "簽章金鑰檔的內容不正確") from None
    if data.get("public_key") != key_info(key)[0]:
        raise SdkError("bad_request", "簽章金鑰檔裡的公鑰與私鑰不相符")
    return key


def write(path: Path, key: Ed25519PrivateKey) -> None:
    public_key, sig_key_id = key_info(key)
    paths.write_json(path, {
        "private_key": b64u_encode(crypto.private_raw(key)),
        "public_key": public_key,
        "sig_key_id": sig_key_id,
    }, secret=True)
    restrict(path)


def ensure(path: Path) -> tuple[Ed25519PrivateKey, bool]:
    """有就沿用（絕不覆寫），沒有就產生一把。回傳 (金鑰, 是否新建)。"""
    if path.exists():
        return load(path), False
    key = crypto.generate_signing_key()
    write(path, key)
    return key, True
