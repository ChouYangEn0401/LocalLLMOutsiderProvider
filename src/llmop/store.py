"""SQLite storage for what changes at runtime: API keys, login sessions and usage.

Accounts and quotas live in the YAML config (edited by the operator); this file is
the "backend record" of who used how many tokens. Everything is in one file,
``<data_dir>/llmop.sqlite3``.

All methods are synchronous and short; they are safe to call from async handlers.
Timestamps are Unix seconds (float, UTC).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import security

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    key_hash     TEXT NOT NULL UNIQUE,
    display      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    expires_at   REAL,
    revoked_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_api_keys_user ON api_keys(username);

CREATE TABLE IF NOT EXISTS sessions (
    id_hash      TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    csrf         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    ip           TEXT NOT NULL DEFAULT '',
    user_agent   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(username);

CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    username          TEXT NOT NULL,
    key_id            TEXT,               -- NULL when used from the web playground
    endpoint          TEXT NOT NULL,      -- chat.completions | completions | embeddings | ...
    model             TEXT NOT NULL,
    backend           TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    usage_source      TEXT NOT NULL,      -- backend | estimate | none
    status            TEXT NOT NULL,      -- ok | error | cancelled
    stream            INTEGER NOT NULL DEFAULT 0,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    client_ip         TEXT NOT NULL DEFAULT '',
    error             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_usage_user_ts ON usage_events(username, ts);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass
class ApiKey:
    id: str
    username: str
    name: str
    display: str
    created_at: float
    last_used_at: float | None
    expires_at: float | None
    revoked_at: float | None

    @property
    def active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > time.time()


@dataclass
class Session:
    username: str
    csrf: str
    created_at: float
    expires_at: float


@dataclass
class UsageEvent:
    username: str
    endpoint: str
    model: str
    backend: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_source: str = "backend"
    status: str = "ok"
    stream: bool = False
    duration_ms: int = 0
    key_id: str | None = None
    client_ip: str = ""
    error: str = ""
    ts: float | None = None

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # ---------------------------------------------------------------- API keys
    def create_api_key(
        self, username: str, name: str = "", expires_at: float | None = None
    ) -> tuple[str, ApiKey]:
        """Returns ``(plaintext_key, record)``. The plaintext is never stored."""
        key = security.new_api_key()
        rec = ApiKey(
            id=security.new_id("key"),
            username=username,
            name=name.strip()[:60],
            display=security.key_display(key),
            created_at=time.time(),
            last_used_at=None,
            expires_at=expires_at,
            revoked_at=None,
        )
        self._exec(
            "INSERT INTO api_keys (id, username, name, key_hash, display, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (rec.id, username, rec.name, security.sha256_hex(key), rec.display,
             rec.created_at, expires_at),
        )
        return key, rec

    @staticmethod
    def _key_row(row: sqlite3.Row) -> ApiKey:
        return ApiKey(
            id=row["id"], username=row["username"], name=row["name"], display=row["display"],
            created_at=row["created_at"], last_used_at=row["last_used_at"],
            expires_at=row["expires_at"], revoked_at=row["revoked_at"],
        )

    def find_api_key(self, plaintext: str) -> ApiKey | None:
        """Look up an active key by its plaintext; also bumps ``last_used_at``."""
        row = self._one(
            "SELECT * FROM api_keys WHERE key_hash = ?", (security.sha256_hex(plaintext),)
        )
        if row is None:
            return None
        rec = self._key_row(row)
        if not rec.active:
            return None
        now = time.time()
        if rec.last_used_at is None or now - rec.last_used_at > 30:
            self._exec("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, rec.id))
            rec.last_used_at = now
        return rec

    def list_api_keys(self, username: str | None = None, include_revoked: bool = False) -> list[ApiKey]:
        sql = "SELECT * FROM api_keys"
        where, params = [], []
        if username is not None:
            where.append("username = ?")
            params.append(username)
        if not include_revoked:
            where.append("revoked_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        return [self._key_row(r) for r in self._all(sql, tuple(params))]

    def count_active_keys(self, username: str) -> int:
        return sum(1 for k in self.list_api_keys(username) if k.active)

    def revoke_api_key(self, key_id: str, username: str | None = None) -> bool:
        """Revoke a key. When ``username`` is given the key must belong to that user."""
        sql = "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"
        params: tuple = (time.time(), key_id)
        if username is not None:
            sql += " AND username = ?"
            params += (username,)
        return self._exec(sql, params).rowcount > 0

    # ---------------------------------------------------------------- sessions
    def create_session(
        self, username: str, ttl_seconds: float, ip: str = "", user_agent: str = ""
    ) -> tuple[str, Session]:
        sid = security.new_session_id()
        now = time.time()
        sess = Session(username=username, csrf=security.new_csrf_token(),
                       created_at=now, expires_at=now + ttl_seconds)
        self._exec(
            "INSERT INTO sessions (id_hash, username, csrf, created_at, expires_at, last_seen_at,"
            " ip, user_agent) VALUES (?,?,?,?,?,?,?,?)",
            (security.sha256_hex(sid), username, sess.csrf, now, sess.expires_at, now,
             ip[:64], user_agent[:200]),
        )
        return sid, sess

    def get_session(self, sid: str) -> Session | None:
        if not sid:
            return None
        row = self._one("SELECT * FROM sessions WHERE id_hash = ?", (security.sha256_hex(sid),))
        if row is None:
            return None
        now = time.time()
        if row["expires_at"] <= now:
            self.delete_session(sid)
            return None
        if now - row["last_seen_at"] > 60:
            self._exec("UPDATE sessions SET last_seen_at = ? WHERE id_hash = ?",
                       (now, row["id_hash"]))
        return Session(username=row["username"], csrf=row["csrf"],
                       created_at=row["created_at"], expires_at=row["expires_at"])

    def delete_session(self, sid: str) -> None:
        self._exec("DELETE FROM sessions WHERE id_hash = ?", (security.sha256_hex(sid),))

    def delete_user_sessions(self, username: str) -> int:
        return self._exec("DELETE FROM sessions WHERE username = ?", (username,)).rowcount

    def purge_expired_sessions(self) -> int:
        return self._exec("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),)).rowcount

    # ---------------------------------------------------------------- usage
    def record_usage(self, ev: UsageEvent) -> None:
        self._exec(
            "INSERT INTO usage_events (ts, username, key_id, endpoint, model, backend,"
            " prompt_tokens, completion_tokens, total_tokens, usage_source, status, stream,"
            " duration_ms, client_ip, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.ts or time.time(), ev.username, ev.key_id, ev.endpoint, ev.model, ev.backend,
             int(ev.prompt_tokens), int(ev.completion_tokens), ev.total_tokens,
             ev.usage_source, ev.status, int(bool(ev.stream)), int(ev.duration_ms),
             ev.client_ip[:64], ev.error[:500]),
        )

    def tokens_since(self, username: str, since_ts: float) -> int:
        row = self._one(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM usage_events"
            " WHERE username = ? AND ts >= ?", (username, since_ts),
        )
        return int(row["t"]) if row else 0

    def requests_since(self, username: str, since_ts: float) -> int:
        row = self._one(
            "SELECT COUNT(*) AS c FROM usage_events WHERE username = ? AND ts >= ?",
            (username, since_ts),
        )
        return int(row["c"]) if row else 0

    def usage_summary(self, username: str | None, since_ts: float) -> dict:
        """Totals since ``since_ts`` (all users when ``username`` is None)."""
        sql = ("SELECT COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt,"
               " COALESCE(SUM(completion_tokens),0) AS completion,"
               " COALESCE(SUM(total_tokens),0) AS total FROM usage_events WHERE ts >= ?")
        params: tuple = (since_ts,)
        if username is not None:
            sql += " AND username = ?"
            params += (username,)
        row = self._one(sql, params)
        return {k: int(row[k]) for k in ("requests", "prompt", "completion", "total")}

    def usage_by_day(self, username: str | None, since_ts: float, utc_offset_s: int = 0) -> list[dict]:
        """Per-day totals (day boundaries shifted by ``utc_offset_s``), oldest first."""
        sql = ("SELECT date(ts + ?, 'unixepoch') AS day, COUNT(*) AS requests,"
               " SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion,"
               " SUM(total_tokens) AS total FROM usage_events WHERE ts >= ?")
        params: tuple = (utc_offset_s, since_ts)
        if username is not None:
            sql += " AND username = ?"
            params += (username,)
        sql += " GROUP BY day ORDER BY day"
        return [dict(r) for r in self._all(sql, params)]

    def usage_by_model(self, username: str | None, since_ts: float) -> list[dict]:
        sql = ("SELECT model, COUNT(*) AS requests, SUM(total_tokens) AS total"
               " FROM usage_events WHERE ts >= ?")
        params: tuple = (since_ts,)
        if username is not None:
            sql += " AND username = ?"
            params += (username,)
        sql += " GROUP BY model ORDER BY total DESC"
        return [dict(r) for r in self._all(sql, params)]

    def usage_by_user(self, since_ts: float) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT username, COUNT(*) AS requests, SUM(total_tokens) AS total"
            " FROM usage_events WHERE ts >= ? GROUP BY username ORDER BY total DESC",
            (since_ts,),
        )]

    def recent_usage(self, username: str | None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM usage_events"
        params: tuple = ()
        if username is not None:
            sql += " WHERE username = ?"
            params = (username,)
        sql += " ORDER BY ts DESC LIMIT ?"
        params += (int(limit),)
        return [dict(r) for r in self._all(sql, params)]

    # ---------------------------------------------------------------- kv
    def kv_set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv (k, v, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
            (key, value, time.time()),
        )

    def kv_get(self, key: str) -> tuple[str, float] | None:
        row = self._one("SELECT v, updated_at FROM kv WHERE k = ?", (key,))
        return (row["v"], row["updated_at"]) if row else None
