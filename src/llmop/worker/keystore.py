"""worker 的長期 X25519 金鑰（keys.json）與註冊憑證（state.json）。

兩個檔案都以 0600 權限寫入（paths.write_json(secret=True)）。私鑰檔沒有加密：
worker 必須能無人值守地啟動，見 docs/SECURITY.md。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from llmop import paths
from llmop.crypto import generate_worker_key, load_worker_private, private_raw, public_raw, worker_key_id
from llmop.protocol import ProtocolError, b64u_decode, b64u_encode


class KeyStoreError(Exception):
    """keys.json 或 state.json 內容有問題；訊息是給操作者看的繁體中文說明。"""


@dataclass(frozen=True)
class WorkerKey:
    key_id: str
    private_key: X25519PrivateKey = field(repr=False)
    created_at: int  # UTC Unix 秒數

    @property
    def public_key(self) -> str:
        """公鑰的 b64u，也就是用戶端要釘選的值。"""
        return b64u_encode(public_raw(self.private_key))

    def announce(self) -> dict:
        """hello 訊框裡的一項：只有 key_id 與公鑰。"""
        return {"key_id": self.key_id, "public_key": self.public_key}


@dataclass(frozen=True)
class KeyStore:
    current: str
    keys: tuple[WorkerKey, ...]

    def current_key(self) -> WorkerKey:
        return next(k for k in self.keys if k.key_id == self.current)

    def ordered(self) -> list[WorkerKey]:
        """目前的金鑰在最前面，其餘依檔案中的順序。"""
        return [self.current_key()] + [k for k in self.keys if k.key_id != self.current]

    def private_key(self, key_id: str) -> X25519PrivateKey | None:
        for key in self.keys:
            if key.key_id == key_id:
                return key.private_key
        return None


@dataclass(frozen=True)
class WorkerState:
    worker_id: str
    worker_token: str = field(repr=False)


def new_key(now: int | None = None) -> WorkerKey:
    private = generate_worker_key()
    return WorkerKey(
        key_id=worker_key_id(public_raw(private)),
        private_key=private,
        created_at=int(time.time()) if now is None else now,
    )


def rotate(store: KeyStore) -> KeyStore:
    """加入一把新金鑰並設為目前金鑰；舊金鑰保留，直到 prune。"""
    key = new_key()
    return KeyStore(current=key.key_id, keys=(*store.keys, key))


def prune(store: KeyStore) -> KeyStore:
    """只留下目前的金鑰。"""
    return replace(store, keys=(store.current_key(),))


def _parse_key(obj: object) -> WorkerKey:
    if not isinstance(obj, dict) or obj.keys() != {"key_id", "private_key", "created_at"}:
        raise KeyStoreError("keys.json 的金鑰項目必須剛好有 key_id、private_key、created_at")
    try:
        raw = b64u_decode(obj["private_key"])
    except ProtocolError:
        raw = b""
    if len(raw) != 32:
        raise KeyStoreError("keys.json 的 private_key 必須是 32 位元組的 b64u")
    private = load_worker_private(raw)
    if worker_key_id(public_raw(private)) != obj["key_id"]:
        raise KeyStoreError(f"keys.json 的 key_id {obj['key_id']!r} 與私鑰不符")
    if type(obj["created_at"]) is not int:
        raise KeyStoreError("keys.json 的 created_at 必須是整數")
    return WorkerKey(key_id=obj["key_id"], private_key=private, created_at=obj["created_at"])


def load_keys(path: Path) -> KeyStore | None:
    """讀取 keys.json；檔案不存在回傳 None。"""
    if not path.exists():
        return None
    try:
        obj = paths.read_json(path)
    except (OSError, ValueError) as exc:
        raise KeyStoreError(f"無法讀取 {path}：{exc}") from exc
    if obj.keys() != {"current", "keys"} or not isinstance(obj["keys"], list) or not obj["keys"]:
        raise KeyStoreError(f"{path} 必須有 current 與非空的 keys")
    keys = tuple(_parse_key(item) for item in obj["keys"])
    if len({k.key_id for k in keys}) != len(keys):
        raise KeyStoreError(f"{path} 有重複的 key_id")
    if obj["current"] not in {k.key_id for k in keys}:
        raise KeyStoreError(f"{path} 的 current 不在 keys 裡")
    return KeyStore(current=obj["current"], keys=keys)


def save_keys(path: Path, store: KeyStore) -> None:
    data = {
        "current": store.current,
        "keys": [
            {"key_id": k.key_id, "private_key": b64u_encode(private_raw(k.private_key)), "created_at": k.created_at}
            for k in store.keys
        ],
    }
    paths.write_json(path, data, secret=True)


def init_keys(path: Path) -> tuple[KeyStore, bool]:
    """沒有 keys.json 就建立第一把金鑰。回傳 (金鑰庫, 是否新建立)。"""
    store = load_keys(path)
    if store is not None:
        return store, False
    key = new_key()
    store = KeyStore(current=key.key_id, keys=(key,))
    save_keys(path, store)
    return store, True


def load_state(path: Path) -> WorkerState | None:
    """讀取 state.json；檔案不存在（尚未註冊）回傳 None。"""
    if not path.exists():
        return None
    try:
        obj = paths.read_json(path)
    except (OSError, ValueError) as exc:
        raise KeyStoreError(f"無法讀取 {path}：{exc}") from exc
    if (
        obj.keys() != {"worker_id", "worker_token"}
        or not isinstance(obj["worker_id"], str)
        or not isinstance(obj["worker_token"], str)
        or not obj["worker_id"]
        or not obj["worker_token"]
    ):
        raise KeyStoreError(f"{path} 必須剛好有非空字串 worker_id 與 worker_token")
    return WorkerState(worker_id=obj["worker_id"], worker_token=obj["worker_token"])


def save_state(path: Path, state: WorkerState) -> None:
    paths.write_json(path, {"worker_id": state.worker_id, "worker_token": state.worker_token}, secret=True)
