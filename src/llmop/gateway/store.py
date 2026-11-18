"""SQLite 儲存：管理員密碼、API 金鑰、每日用量、worker 與其金鑰、模型。

只存雜湊、公鑰、上限與計數。API 金鑰、註冊權杖與 worker 權杖的明文只在建立時回傳一次，
從不寫進資料庫。查詢都很短且在本機，所以直接在事件迴圈上執行。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llmop import paths
from llmop.protocol import ProtocolError, b64u_encode

DB_NAME = "gateway.db"
REG_TOKEN_TTL_SECONDS = 3600

# scrypt 參數：約 16 MiB 記憶體，一次驗證約數十毫秒。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    sig_public_key TEXT,
    daily_request_limit INTEGER,
    daily_token_limit INTEGER,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS usage (
    api_key_id TEXT NOT NULL REFERENCES api_keys(id),
    day TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, day)
);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT,
    reg_token_hash TEXT UNIQUE,
    reg_expires_at INTEGER,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS worker_keys (
    worker_id TEXT NOT NULL REFERENCES workers(id),
    key_id TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_models (
    worker_id TEXT NOT NULL REFERENCES workers(id),
    model TEXT NOT NULL,
    PRIMARY KEY (worker_id, model)
);
"""


def utc_day(now: float) -> str:
    """Unix 秒數所在的 UTC 日期，例如 "2026-11-18"。"""
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def sha256_hex(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hash_password(password: str, salt: bytes) -> bytes:
    """以 scrypt 計算密碼雜湊。很耗 CPU，呼叫端可放到執行緒執行。"""
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN
    )


def new_api_key() -> str:
    return "llmop_sk_" + b64u_encode(secrets.token_bytes(32))


def new_reg_token() -> str:
    return "llmop_reg_" + b64u_encode(secrets.token_bytes(24))


def new_worker_token() -> str:
    return "llmop_wk_" + b64u_encode(secrets.token_bytes(32))


@dataclass(frozen=True)
class ApiKey:
    id: str
    name: str
    sig_public_key: str | None
    daily_request_limit: int | None
    daily_token_limit: int | None
    created_at: int
    revoked_at: int | None


@dataclass(frozen=True)
class Worker:
    id: str
    name: str
    created_at: int
    last_seen_at: int | None
    revoked_at: int | None
    reg_expires_at: int | None


_API_KEY_COLUMNS = "id, name, sig_public_key, daily_request_limit, daily_token_limit, created_at, revoked_at"
_WORKER_COLUMNS = "id, name, created_at, last_seen_at, revoked_at, reg_expires_at"


class Store:
    """閘道的 SQLite 資料庫。只在事件迴圈所在的執行緒使用。"""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        # isolation_level=None：自己用 BEGIN IMMEDIATE / COMMIT 控制交易。
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)
        os.chmod(self.path, 0o600)

    @classmethod
    def open_default(cls) -> Store:
        return cls(paths.home("gateway") / DB_NAME)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # ------------------------------------------------------------------
    # 管理員密碼
    # ------------------------------------------------------------------

    def set_admin_password(self, password: str, now: int | None = None) -> None:
        salt = secrets.token_bytes(16)
        digest = hash_password(password, salt)
        self.db.execute(
            "INSERT INTO admin (id, password_hash, salt, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, "
            "salt = excluded.salt, updated_at = excluded.updated_at",
            (digest.hex(), salt.hex(), _now(now)),
        )

    def admin_password_record(self) -> tuple[bytes, bytes] | None:
        """回傳 (salt, hash)；尚未設定密碼則回傳 None。"""
        row = self.db.execute("SELECT salt, password_hash FROM admin WHERE id = 1").fetchone()
        return (bytes.fromhex(row[0]), bytes.fromhex(row[1])) if row else None

    # ------------------------------------------------------------------
    # API 金鑰
    # ------------------------------------------------------------------

    def create_api_key(
        self,
        name: str,
        sig_public_key: str | None = None,
        daily_request_limit: int | None = None,
        daily_token_limit: int | None = None,
        now: int | None = None,
    ) -> tuple[ApiKey, str]:
        """建立 API 金鑰，回傳 (紀錄, 明文金鑰)。明文只在這裡出現一次。"""
        secret = new_api_key()
        key_id = "k_" + secrets.token_hex(6)
        self.db.execute(
            "INSERT INTO api_keys (id, name, key_hash, sig_public_key, daily_request_limit, "
            "daily_token_limit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key_id, name, sha256_hex(secret), sig_public_key, daily_request_limit, daily_token_limit, _now(now)),
        )
        key = self.get_api_key(key_id)
        assert key is not None
        return key, secret

    def get_api_key(self, key_id: str) -> ApiKey | None:
        row = self.db.execute(f"SELECT {_API_KEY_COLUMNS} FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return ApiKey(*row) if row else None

    def find_api_key(self, secret: str) -> ApiKey | None:
        """用明文金鑰找出未撤銷的 API 金鑰；找不到或已撤銷回傳 None。"""
        digest = sha256_hex(secret)
        row = self.db.execute(
            f"SELECT key_hash, {_API_KEY_COLUMNS} FROM api_keys WHERE key_hash = ?", (digest,)
        ).fetchone()
        if row is None or not hmac.compare_digest(row[0], digest):
            return None
        key = ApiKey(*row[1:])
        return None if key.revoked_at is not None else key

    def list_api_keys(self, day: str) -> list[tuple[ApiKey, dict]]:
        """所有 API 金鑰，以及每一把在 day 這天的用量。"""
        rows = self.db.execute(
            "SELECT k.id, k.name, k.sig_public_key, k.daily_request_limit, k.daily_token_limit, "
            "k.created_at, k.revoked_at, "
            "COALESCE(u.requests, 0), COALESCE(u.prompt_tokens, 0), COALESCE(u.completion_tokens, 0) "
            "FROM api_keys k LEFT JOIN usage u ON u.api_key_id = k.id AND u.day = ? ORDER BY k.created_at, k.id",
            (day,),
        ).fetchall()
        return [
            (ApiKey(*row[:7]), {"requests": row[7], "prompt_tokens": row[8], "completion_tokens": row[9]})
            for row in rows
        ]

    def update_api_key(self, key_id: str, changes: dict) -> ApiKey | None:
        """更新 sig_public_key、daily_request_limit、daily_token_limit 其中幾項。"""
        allowed = {"sig_public_key", "daily_request_limit", "daily_token_limit"}
        if not changes.keys() <= allowed:
            raise ValueError("不允許更新的欄位")
        if changes:
            assignments = ", ".join(f"{name} = ?" for name in changes)
            self.db.execute(f"UPDATE api_keys SET {assignments} WHERE id = ?", (*changes.values(), key_id))
        return self.get_api_key(key_id)

    def revoke_api_key(self, key_id: str, now: int | None = None) -> ApiKey | None:
        self.db.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_now(now), key_id)
        )
        return self.get_api_key(key_id)

    # ------------------------------------------------------------------
    # 配額與用量
    # ------------------------------------------------------------------

    @contextmanager
    def admit(self, api_key_id: str, max_tokens: int, day: str) -> Iterator[None]:
        """在同一個交易裡檢查配額、把請求數加一，並「保留」max_tokens。

        超過配額丟 quota_exceeded。with 區塊正常結束才計數、保留並提交；區塊內丟出例外就回滾，
        不計數也不保留。區塊內不可以 await，否則其他請求可能在交易中途插進來。

        reserved_tokens 是還在進行中、尚未結算的請求各自的 max_tokens 總和：同時有好幾筆
        請求在等 worker 回覆時，光看「已完成的用量」會讓它們全部通過檢查，合計超過上限
        （例如上限 100、兩筆都還在跑的 max_tokens=60 都會通過）。所以配額檢查要看
        「已完成用量 + 保留中 + 這一筆的 max_tokens」。工作結束時（不管成功、失敗、逾時或
        斷線）一定要呼叫 release_reservation 把保留的這一份還回去，見 hub.Hub.finish。
        """
        with self._tx():
            limits = self.db.execute(
                "SELECT daily_request_limit, daily_token_limit FROM api_keys WHERE id = ?", (api_key_id,)
            ).fetchone()
            used = self.db.execute(
                "SELECT requests, prompt_tokens + completion_tokens, reserved_tokens "
                "FROM usage WHERE api_key_id = ? AND day = ?",
                (api_key_id, day),
            ).fetchone() or (0, 0, 0)
            request_limit, token_limit = limits
            if request_limit is not None and used[0] >= request_limit:
                raise ProtocolError("quota_exceeded", "今天的請求數已達上限")
            if token_limit is not None and used[1] + used[2] + max_tokens > token_limit:
                raise ProtocolError("quota_exceeded", "今天的 token 用量加上進行中的保留與這筆的 max_tokens 會超過上限")
            yield
            self.db.execute(
                "INSERT INTO usage (api_key_id, day, requests, reserved_tokens) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(api_key_id, day) DO UPDATE SET "
                "requests = requests + 1, reserved_tokens = reserved_tokens + excluded.reserved_tokens",
                (api_key_id, day, max_tokens),
            )

    def release_reservation(self, api_key_id: str, day: str, max_tokens: int) -> None:
        """工作結束時釋放 admit 保留的 max_tokens（不管結果是成功、失敗、逾時還是斷線都要呼叫一次）。

        用 MAX(…, 0) 夾住下限，單純求穩：正常情況下每筆保留只會被釋放一次，不會變負的。
        """
        if max_tokens <= 0:
            return
        self.db.execute(
            "UPDATE usage SET reserved_tokens = MAX(reserved_tokens - ?, 0) WHERE api_key_id = ? AND day = ?",
            (max_tokens, api_key_id, day),
        )

    def add_tokens(self, api_key_id: str, day: str, prompt_tokens: int, completion_tokens: int) -> None:
        """把 worker 回報的 token 數加進那一天的用量（跟保留的釋放是分開呼叫的兩件事，見 release_reservation）。"""
        self.db.execute(
            "INSERT INTO usage (api_key_id, day, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(api_key_id, day) DO UPDATE SET prompt_tokens = prompt_tokens + excluded.prompt_tokens, "
            "completion_tokens = completion_tokens + excluded.completion_tokens",
            (api_key_id, day, prompt_tokens, completion_tokens),
        )

    def usage_for(self, api_key_id: str, day: str) -> dict:
        row = self.db.execute(
            "SELECT requests, prompt_tokens, completion_tokens FROM usage WHERE api_key_id = ? AND day = ?",
            (api_key_id, day),
        ).fetchone() or (0, 0, 0)
        return {"requests": row[0], "prompt_tokens": row[1], "completion_tokens": row[2]}

    def usage_since(self, first_day: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT api_key_id, day, requests, prompt_tokens, completion_tokens FROM usage "
            "WHERE day >= ? ORDER BY day DESC, api_key_id",
            (first_day,),
        ).fetchall()
        names = ("api_key_id", "day", "requests", "prompt_tokens", "completion_tokens")
        return [dict(zip(names, row, strict=True)) for row in rows]

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------

    def create_worker(self, name: str, now: int | None = None) -> tuple[Worker, str]:
        """建立 worker 並發出註冊權杖，回傳 (紀錄, 明文註冊權杖)。"""
        worker_id = "w_" + secrets.token_hex(6)
        reg_token = new_reg_token()
        now = _now(now)
        self.db.execute(
            "INSERT INTO workers (id, name, reg_token_hash, reg_expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (worker_id, name, sha256_hex(reg_token), now + REG_TOKEN_TTL_SECONDS, now),
        )
        worker = self.get_worker(worker_id)
        assert worker is not None
        return worker, reg_token

    def reissue_reg_token(self, worker_id: str, now: int | None = None) -> tuple[Worker, str] | None:
        """為未撤銷的 worker 重新發出註冊權杖（舊的註冊權杖隨之失效）。"""
        reg_token = new_reg_token()
        cursor = self.db.execute(
            "UPDATE workers SET reg_token_hash = ?, reg_expires_at = ? WHERE id = ? AND revoked_at IS NULL",
            (sha256_hex(reg_token), _now(now) + REG_TOKEN_TTL_SECONDS, worker_id),
        )
        if cursor.rowcount == 0:
            return None
        worker = self.get_worker(worker_id)
        assert worker is not None
        return worker, reg_token

    def get_worker(self, worker_id: str) -> Worker | None:
        row = self.db.execute(f"SELECT {_WORKER_COLUMNS} FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return Worker(*row) if row else None

    def list_workers(self) -> list[Worker]:
        rows = self.db.execute(f"SELECT {_WORKER_COLUMNS} FROM workers ORDER BY created_at, id").fetchall()
        return [Worker(*row) for row in rows]

    def revoke_worker(self, worker_id: str, now: int | None = None) -> Worker | None:
        self.db.execute(
            "UPDATE workers SET revoked_at = ?, reg_token_hash = NULL, reg_expires_at = NULL "
            "WHERE id = ? AND revoked_at IS NULL",
            (_now(now), worker_id),
        )
        return self.get_worker(worker_id)

    def register_worker(self, reg_token: str, now: int | None = None) -> tuple[str, str]:
        """用註冊權杖完成註冊，回傳 (worker_id, 明文 worker 權杖)。

        權杖只能用一次，過期、已用過或 worker 已撤銷都丟 unauthorized。
        新的 worker 權杖會取代這台 worker 之前的權杖。
        """
        digest = sha256_hex(reg_token)
        now = _now(now)
        with self._tx():
            row = self.db.execute(
                "SELECT id, reg_token_hash, reg_expires_at, revoked_at FROM workers WHERE reg_token_hash = ?",
                (digest,),
            ).fetchone()
            if (
                row is None
                or not hmac.compare_digest(row[1], digest)
                or row[3] is not None
                or row[2] is None
                or row[2] <= now
            ):
                raise ProtocolError("unauthorized", "註冊權杖無效、已使用或已過期")
            worker_token = new_worker_token()
            self.db.execute(
                "UPDATE workers SET token_hash = ?, reg_token_hash = NULL, reg_expires_at = NULL WHERE id = ?",
                (sha256_hex(worker_token), row[0]),
            )
        return row[0], worker_token

    def authenticate_worker(self, worker_id: str, worker_token: str) -> Worker:
        """確認 worker 權杖正確且 worker 未撤銷，否則丟 unauthorized。"""
        row = self.db.execute(
            f"SELECT token_hash, {_WORKER_COLUMNS} FROM workers WHERE id = ?", (worker_id,)
        ).fetchone()
        if row is None or row[0] is None or not hmac.compare_digest(row[0], sha256_hex(worker_token)):
            raise ProtocolError("unauthorized", "worker 身分驗證失敗")
        worker = Worker(*row[1:])
        if worker.revoked_at is not None:
            raise ProtocolError("unauthorized", "worker 已被撤銷")
        return worker

    def record_worker_keys(self, worker_id: str, keys: dict[str, str], now: int | None = None) -> None:
        """記錄 worker 公告的金鑰（key_id → public_key）。

        任何一個 key_id 已經屬於別台 worker 就整批拒絕（key_conflict）；沒看過的金鑰記下 first_seen_at。
        """
        now = _now(now)
        with self._tx():
            for key_id, public_key in keys.items():
                row = self.db.execute(
                    "SELECT worker_id, public_key FROM worker_keys WHERE key_id = ?", (key_id,)
                ).fetchone()
                if row is None:
                    self.db.execute(
                        "INSERT INTO worker_keys (worker_id, key_id, public_key, first_seen_at) VALUES (?, ?, ?, ?)",
                        (worker_id, key_id, public_key, now),
                    )
                elif row[0] != worker_id or row[1] != public_key:
                    raise ProtocolError("key_conflict", "key_id 已登記在另一台 worker 名下")

    def worker_keys(self, worker_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT key_id, public_key, first_seen_at FROM worker_keys "
            "WHERE worker_id = ? ORDER BY first_seen_at, rowid",
            (worker_id,),
        ).fetchall()
        return [{"key_id": r[0], "public_key": r[1], "first_seen_at": r[2]} for r in rows]

    def set_worker_models(self, worker_id: str, models: list[str]) -> None:
        with self._tx():
            self.db.execute("DELETE FROM worker_models WHERE worker_id = ?", (worker_id,))
            self.db.executemany(
                "INSERT INTO worker_models (worker_id, model) VALUES (?, ?)", [(worker_id, m) for m in models]
            )

    def worker_models(self, worker_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT model FROM worker_models WHERE worker_id = ? ORDER BY model", (worker_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def touch_worker(self, worker_id: str, now: int | None = None) -> None:
        self.db.execute("UPDATE workers SET last_seen_at = ? WHERE id = ?", (_now(now), worker_id))


def first_day_of_window(today: str, days: int) -> str:
    """包含 today 在內、往回 days 天的第一天。"""
    start = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d")


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now
