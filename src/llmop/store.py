"""SQLite storage for what changes at runtime: API keys, login sessions and usage.

Accounts and quotas live in the YAML config (edited by the operator); this file is
the "backend record" of who used how many tokens. Everything is in one file,
``<data_dir>/llmop.sqlite3``.

All methods are synchronous and short; they are safe to call from async handlers.
Timestamps are Unix seconds (float, UTC); the worker tables (copied from B's
gateway store) keep B's integer seconds.

Schema versions (``PRAGMA user_version``):

* 0/1 -- A v0.1 (api_keys.username NOT NULL, no key flags, no workers/buckets).
* 2   -- v0.2: api_keys gains ``sig_public_key, encryption, tier, budget_tokens,
  reset, label`` and ``username`` may be NULL (GUEST key); ``sessions.origin``;
  new tables ``workers, worker_keys, worker_models, buckets, api_link``.
  Older files are upgraded in place when opened (see :meth:`Store._migrate`).
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import security
from .protocol import ProtocolError

SCHEMA_VERSION = 2
ENCRYPTION_MODES = ("optional", "required")
REG_TOKEN_TTL_SECONDS = 3600

# {table} is filled in so the same DDL serves the in-place rebuild during migration.
API_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id             TEXT PRIMARY KEY,
    username       TEXT,                -- NULL = GUEST key (belongs to no account)
    name           TEXT NOT NULL DEFAULT '',
    key_hash       TEXT NOT NULL UNIQUE,
    display        TEXT NOT NULL,
    created_at     REAL NOT NULL,
    last_used_at   REAL,
    expires_at     REAL,
    revoked_at     REAL,
    sig_public_key TEXT,                -- client signing key (encrypted mode)
    encryption     TEXT NOT NULL DEFAULT 'optional',  -- optional | required
    tier           TEXT,                -- NULL = the account's tier; 'guest' for GUEST keys
    budget_tokens  INTEGER,             -- GUEST total budget (NULL = tier default)
    reset          INTEGER NOT NULL DEFAULT 0,        -- GUEST: budget resets weekly
    label          TEXT NOT NULL DEFAULT ''
)"""
API_KEY_COLUMNS = (
    "id", "username", "name", "key_hash", "display", "created_at", "last_used_at",
    "expires_at", "revoked_at", "sig_public_key", "encryption", "tier", "budget_tokens",
    "reset", "label",
)

SCHEMA = API_KEYS_DDL.format(table="api_keys") + """;
CREATE INDEX IF NOT EXISTS ix_api_keys_user ON api_keys(username);

CREATE TABLE IF NOT EXISTS sessions (
    id_hash      TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    csrf         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    ip           TEXT NOT NULL DEFAULT '',
    user_agent   TEXT NOT NULL DEFAULT '',
    origin       TEXT NOT NULL DEFAULT ''  -- which surface issued it (local | public)
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

-- Usage buckets (TIERS 2): one row per subject ("acct:<username>" or "key:<key id>").
CREATE TABLE IF NOT EXISTS buckets (
    subject      TEXT PRIMARY KEY,
    level        REAL NOT NULL,           -- 1.5-hour bucket level (may be negative)
    updated_at   REAL NOT NULL,           -- when ``level`` was last brought up to date
    capacity     REAL NOT NULL DEFAULT 0, -- capacity the level refers to (rescaled on change)
    low          INTEGER NOT NULL DEFAULT 0,  -- bucket ran dry; cleared at recover_at
    period_used  INTEGER NOT NULL DEFAULT 0,  -- weekly table (GUEST: budget used)
    period_start REAL NOT NULL DEFAULT 0,     -- reset boundary period_used counts from
    exhausted_at REAL                          -- GUEST: when the budget ran out
);

-- The current and previous public API gate (only hashes are stored).
CREATE TABLE IF NOT EXISTS api_link (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    current_gate_hash  TEXT NOT NULL,
    previous_gate_hash TEXT,
    grace_until        REAL,
    updated_at         REAL NOT NULL
);

-- Workers (copied from B's gateway store).
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


@dataclass
class ApiKey:
    id: str
    username: str | None  # None = GUEST key
    name: str
    display: str
    created_at: float
    last_used_at: float | None
    expires_at: float | None
    revoked_at: float | None
    sig_public_key: str | None = None
    encryption: str = "optional"  # optional | required
    tier: str | None = None  # None = the account's tier
    budget_tokens: int | None = None
    reset: bool = False
    label: str = ""

    @property
    def active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > time.time()

    @property
    def is_guest(self) -> bool:
        return self.username is None or self.tier == "guest"


@dataclass
class Session:
    username: str
    csrf: str
    created_at: float
    expires_at: float
    origin: str = ""


@dataclass(frozen=True)
class Worker:
    id: str
    name: str
    created_at: int
    last_seen_at: int | None
    revoked_at: int | None
    reg_expires_at: int | None


_WORKER_COLUMNS = "id, name, created_at, last_seen_at, revoked_at, reg_expires_at"
_BUCKET_FIELDS = ("level", "updated_at", "capacity", "low", "period_used", "period_start",
                  "exhausted_at")


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
            self._migrate()

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

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """One write transaction; the lock keeps other threads out until COMMIT."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # ---------------------------------------------------------------- schema
    def _columns(self, table: str) -> dict[str, sqlite3.Row]:
        return {r["name"]: r for r in self._db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Create missing tables and upgrade an older file in place (idempotent).

        Existing rows are copied unchanged; new columns get their defaults
        (``encryption='optional'``, ``reset=0``, ``label=''``, ``origin=''``).
        """
        with self._lock:
            old_keys = self._columns("api_keys")
            if old_keys and (
                set(API_KEY_COLUMNS) - set(old_keys) or old_keys["username"]["notnull"]
            ):
                # SQLite cannot drop NOT NULL with ALTER: rebuild the table.
                shared = [c for c in API_KEY_COLUMNS if c in old_keys]
                cols = ", ".join(shared)
                with self._tx():
                    self._db.execute("DROP TABLE IF EXISTS api_keys__new")
                    self._db.execute(API_KEYS_DDL.format(table="api_keys__new"))
                    self._db.execute(
                        f"INSERT INTO api_keys__new ({cols}) SELECT {cols} FROM api_keys"
                    )
                    self._db.execute("DROP TABLE api_keys")
                    self._db.execute("ALTER TABLE api_keys__new RENAME TO api_keys")
            sess = self._columns("sessions")
            if sess and "origin" not in sess:
                self._db.execute(
                    "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT ''"
                )
            self._db.executescript(SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def schema_version(self) -> int:
        return int(self._one("PRAGMA user_version")[0])

    # ---------------------------------------------------------------- API keys
    def create_api_key(
        self, username: str | None, name: str = "", expires_at: float | None = None, *,
        sig_public_key: str | None = None, encryption: str = "optional",
        tier: str | None = None, budget_tokens: int | None = None, reset: bool = False,
        label: str = "",
    ) -> tuple[str, ApiKey]:
        """Returns ``(plaintext_key, record)``. The plaintext is never stored.

        ``username=None`` makes a GUEST key (use :meth:`create_guest_key`)."""
        if encryption not in ENCRYPTION_MODES:
            raise ValueError(f"encryption must be one of {ENCRYPTION_MODES}")
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
            sig_public_key=sig_public_key or None,
            encryption=encryption,
            tier=tier,
            budget_tokens=None if budget_tokens is None else int(budget_tokens),
            reset=bool(reset),
            label=label.strip()[:60],
        )
        self._exec(
            "INSERT INTO api_keys (id, username, name, key_hash, display, created_at, expires_at,"
            " sig_public_key, encryption, tier, budget_tokens, reset, label)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, username, rec.name, security.sha256_hex(key), rec.display,
             rec.created_at, expires_at, rec.sig_public_key, rec.encryption, rec.tier,
             rec.budget_tokens, int(rec.reset), rec.label),
        )
        return key, rec

    def create_guest_key(
        self, label: str = "", *, budget_tokens: int | None = None,
        expires_at: float | None = None, encryption: str = "optional", reset: bool = False,
        sig_public_key: str | None = None,
    ) -> tuple[str, ApiKey]:
        """A GUEST key (TIERS 8): no account; ``budget_tokens=None`` = tier default."""
        return self.create_api_key(
            None, label, expires_at, sig_public_key=sig_public_key, encryption=encryption,
            tier="guest", budget_tokens=budget_tokens, reset=reset, label=label,
        )

    @staticmethod
    def _key_row(row: sqlite3.Row) -> ApiKey:
        return ApiKey(
            id=row["id"], username=row["username"], name=row["name"], display=row["display"],
            created_at=row["created_at"], last_used_at=row["last_used_at"],
            expires_at=row["expires_at"], revoked_at=row["revoked_at"],
            sig_public_key=row["sig_public_key"], encryption=row["encryption"],
            tier=row["tier"], budget_tokens=row["budget_tokens"], reset=bool(row["reset"]),
            label=row["label"],
        )

    def get_api_key(self, key_id: str) -> ApiKey | None:
        """A key by id, whatever its state (revoked / expired included)."""
        row = self._one("SELECT * FROM api_keys WHERE id = ?", (key_id,))
        return self._key_row(row) if row else None

    def lookup_api_key(self, plaintext: str) -> ApiKey | None:
        """Like :meth:`find_api_key` but also returns revoked / expired keys and does not
        touch ``last_used_at`` (so a dead GUEST key can still show why it is dead)."""
        digest = security.sha256_hex(plaintext)
        row = self._one("SELECT * FROM api_keys WHERE key_hash = ?", (digest,))
        if row is None or not hmac.compare_digest(row["key_hash"], digest):
            return None
        return self._key_row(row)

    def list_guest_keys(self, include_revoked: bool = True) -> list[ApiKey]:
        sql = "SELECT * FROM api_keys WHERE (username IS NULL OR tier = 'guest')"
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        return [self._key_row(r) for r in self._all(sql)]

    def update_api_key(
        self, key_id: str, changes: dict, username: str | None = None
    ) -> ApiKey | None:
        """Change ``sig_public_key``, ``encryption`` or ``label``. When ``username`` is
        given the key must belong to that user. Returns the record, or None."""
        allowed = {"sig_public_key", "encryption", "label"}
        if not changes.keys() <= allowed:
            raise ValueError("field not allowed")
        if "encryption" in changes and changes["encryption"] not in ENCRYPTION_MODES:
            raise ValueError(f"encryption must be one of {ENCRYPTION_MODES}")
        rec = self.get_api_key(key_id)
        if rec is None or (username is not None and rec.username != username):
            return None
        if changes:
            assignments = ", ".join(f"{name} = ?" for name in changes)
            self._exec(f"UPDATE api_keys SET {assignments} WHERE id = ?",
                       (*changes.values(), key_id))
        return self.get_api_key(key_id)

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
        self, username: str, ttl_seconds: float, ip: str = "", user_agent: str = "",
        origin: str = "",
    ) -> tuple[str, Session]:
        sid = security.new_session_id()
        now = time.time()
        sess = Session(username=username, csrf=security.new_csrf_token(),
                       created_at=now, expires_at=now + ttl_seconds, origin=origin[:16])
        self._exec(
            "INSERT INTO sessions (id_hash, username, csrf, created_at, expires_at, last_seen_at,"
            " ip, user_agent, origin) VALUES (?,?,?,?,?,?,?,?,?)",
            (security.sha256_hex(sid), username, sess.csrf, now, sess.expires_at, now,
             ip[:64], user_agent[:200], sess.origin),
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
                       created_at=row["created_at"], expires_at=row["expires_at"],
                       origin=row["origin"])

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

    # ---------------------------------------------------------------- buckets
    def get_bucket(self, subject: str) -> dict | None:
        row = self._one("SELECT * FROM buckets WHERE subject = ?", (subject,))
        return dict(row) if row else None

    def save_bucket(self, subject: str, **fields) -> None:
        """Insert or replace a bucket row; missing fields keep their defaults."""
        if not fields.keys() <= set(_BUCKET_FIELDS) or "level" not in fields \
                or "updated_at" not in fields:
            raise ValueError("bad bucket fields")
        names = ["subject", *fields]
        self._exec(
            f"INSERT OR REPLACE INTO buckets ({', '.join(names)})"
            f" VALUES ({', '.join('?' * len(names))})",
            (subject, *fields.values()),
        )

    def list_buckets(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM buckets ORDER BY subject")]

    # ---------------------------------------------------------------- api link
    def get_api_link(self) -> dict | None:
        row = self._one("SELECT current_gate_hash, previous_gate_hash, grace_until, updated_at"
                        " FROM api_link WHERE id = 1")
        return dict(row) if row else None

    def set_api_link(
        self, current_gate_hash: str, previous_gate_hash: str | None = None,
        grace_until: float | None = None, now: float | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO api_link (id, current_gate_hash, previous_gate_hash, grace_until,"
            " updated_at) VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
            " current_gate_hash = excluded.current_gate_hash,"
            " previous_gate_hash = excluded.previous_gate_hash,"
            " grace_until = excluded.grace_until, updated_at = excluded.updated_at",
            (current_gate_hash, previous_gate_hash, grace_until,
             time.time() if now is None else now),
        )

    # ---------------------------------------------------------------- workers
    # Copied from B's ``gateway/store.py`` (same semantics and error codes).
    def create_worker(self, name: str, now: int | None = None) -> tuple[Worker, str]:
        """Create a worker and issue an enrolment token: ``(record, plaintext token)``."""
        worker_id = "w_" + secrets.token_hex(6)
        reg_token = security.new_reg_token()
        now = _now(now)
        self._exec(
            "INSERT INTO workers (id, name, reg_token_hash, reg_expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (worker_id, name, security.sha256_hex(reg_token), now + REG_TOKEN_TTL_SECONDS, now),
        )
        worker = self.get_worker(worker_id)
        assert worker is not None
        return worker, reg_token

    def reissue_reg_token(self, worker_id: str, now: int | None = None) -> tuple[Worker, str] | None:
        """New enrolment token for a non-revoked worker (the old one stops working)."""
        reg_token = security.new_reg_token()
        cursor = self._exec(
            "UPDATE workers SET reg_token_hash = ?, reg_expires_at = ?"
            " WHERE id = ? AND revoked_at IS NULL",
            (security.sha256_hex(reg_token), _now(now) + REG_TOKEN_TTL_SECONDS, worker_id),
        )
        if cursor.rowcount == 0:
            return None
        worker = self.get_worker(worker_id)
        assert worker is not None
        return worker, reg_token

    def get_worker(self, worker_id: str) -> Worker | None:
        row = self._one(f"SELECT {_WORKER_COLUMNS} FROM workers WHERE id = ?", (worker_id,))
        return Worker(*row) if row else None

    def list_workers(self) -> list[Worker]:
        rows = self._all(f"SELECT {_WORKER_COLUMNS} FROM workers ORDER BY created_at, id")
        return [Worker(*row) for row in rows]

    def revoke_worker(self, worker_id: str, now: int | None = None) -> Worker | None:
        self._exec(
            "UPDATE workers SET revoked_at = ?, reg_token_hash = NULL, reg_expires_at = NULL"
            " WHERE id = ? AND revoked_at IS NULL",
            (_now(now), worker_id),
        )
        return self.get_worker(worker_id)

    def register_worker(self, reg_token: str, now: int | None = None) -> tuple[str, str]:
        """Redeem an enrolment token: ``(worker_id, plaintext worker token)``.

        Single use; expired, used or revoked -> ``ProtocolError("unauthorized")``.
        The new worker token replaces the worker's previous one."""
        digest = security.sha256_hex(reg_token)
        now = _now(now)
        with self._tx():
            row = self._db.execute(
                "SELECT id, reg_token_hash, reg_expires_at, revoked_at FROM workers"
                " WHERE reg_token_hash = ?",
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
            worker_token = security.new_worker_token()
            self._db.execute(
                "UPDATE workers SET token_hash = ?, reg_token_hash = NULL, reg_expires_at = NULL"
                " WHERE id = ?",
                (security.sha256_hex(worker_token), row[0]),
            )
        return row[0], worker_token

    def authenticate_worker(self, worker_id: str, worker_token: str) -> Worker:
        """Worker token must match and the worker must not be revoked (else unauthorized)."""
        row = self._one(
            f"SELECT token_hash, {_WORKER_COLUMNS} FROM workers WHERE id = ?", (worker_id,)
        )
        if row is None or row[0] is None or not hmac.compare_digest(
            row[0], security.sha256_hex(worker_token)
        ):
            raise ProtocolError("unauthorized", "worker 身分驗證失敗")
        worker = Worker(*tuple(row)[1:])
        if worker.revoked_at is not None:
            raise ProtocolError("unauthorized", "worker 已被撤銷")
        return worker

    def record_worker_keys(self, worker_id: str, keys: dict[str, str], now: int | None = None) -> None:
        """Remember the worker's announced keys (key_id -> public_key).

        A key_id already owned by another worker (or with another public key) rejects
        the whole batch with ``key_conflict``."""
        now = _now(now)
        with self._tx():
            for key_id, public_key in keys.items():
                row = self._db.execute(
                    "SELECT worker_id, public_key FROM worker_keys WHERE key_id = ?", (key_id,)
                ).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO worker_keys (worker_id, key_id, public_key, first_seen_at)"
                        " VALUES (?, ?, ?, ?)",
                        (worker_id, key_id, public_key, now),
                    )
                elif row[0] != worker_id or row[1] != public_key:
                    raise ProtocolError("key_conflict", "key_id 已登記在另一台 worker 名下")

    def worker_keys(self, worker_id: str) -> list[dict]:
        rows = self._all(
            "SELECT key_id, public_key, first_seen_at FROM worker_keys"
            " WHERE worker_id = ? ORDER BY first_seen_at, rowid",
            (worker_id,),
        )
        return [{"key_id": r[0], "public_key": r[1], "first_seen_at": r[2]} for r in rows]

    def set_worker_models(self, worker_id: str, models: list[str]) -> None:
        with self._tx():
            self._db.execute("DELETE FROM worker_models WHERE worker_id = ?", (worker_id,))
            self._db.executemany(
                "INSERT INTO worker_models (worker_id, model) VALUES (?, ?)",
                [(worker_id, m) for m in models],
            )

    def worker_models(self, worker_id: str) -> list[str]:
        rows = self._all(
            "SELECT model FROM worker_models WHERE worker_id = ? ORDER BY model", (worker_id,)
        )
        return [r[0] for r in rows]

    def touch_worker(self, worker_id: str, now: int | None = None) -> None:
        self._exec("UPDATE workers SET last_seen_at = ? WHERE id = ?", (_now(now), worker_id))


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now
