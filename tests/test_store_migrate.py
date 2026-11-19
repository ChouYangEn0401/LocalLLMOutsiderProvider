"""Automatic upgrade of an A v0.1 SQLite file, plus the new v0.2 tables
(workers copied from B's gateway store, buckets, api_link, sessions.origin)."""

from __future__ import annotations

import sqlite3

import pytest

from llmop import security
from llmop.protocol import ProtocolError
from llmop.store import SCHEMA_VERSION, Store, UsageEvent

# The schema exactly as A v0.1 created it (src/llmop/store.py before v0.2).
V01_SCHEMA = """
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
    key_id            TEXT,
    endpoint          TEXT NOT NULL,
    model             TEXT NOT NULL,
    backend           TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    usage_source      TEXT NOT NULL,
    status            TEXT NOT NULL,
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

OLD_KEY = "sk-llop-" + "A" * 43
SID = "session-id-from-v01"


def _dump(path, table):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    db.close()
    return rows


@pytest.fixture
def v01_file(tmp_path):
    path = tmp_path / "llmop.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(V01_SCHEMA)
    db.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,?)",
               ("key_1", "alice", "laptop", security.sha256_hex(OLD_KEY), "sk-llop-...AAAA",
                1000.5, 1200.0, None, None))
    db.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,?)",
               ("key_2", "bob", "", security.sha256_hex("sk-llop-old2"), "sk-llop-...old2",
                1001.0, None, 5000.0, 3000.0))
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
               (security.sha256_hex(SID), "alice", "csrf1", 1000.0, 9e12, 1000.0, "1.2.3.4", "UA"))
    db.execute("INSERT INTO usage_events (ts, username, key_id, endpoint, model, backend,"
               " prompt_tokens, completion_tokens, total_tokens, usage_source, status)"
               " VALUES (1100, 'alice', 'key_1', 'chat.completions', 'm', 'b', 10, 5, 15,"
               " 'backend', 'ok')")
    db.execute("INSERT INTO kv VALUES ('public_url', 'https://x.example', 1)")
    db.commit()
    db.close()
    return path


def test_v01_file_upgrades_without_changing_data(v01_file):
    before = {t: _dump(v01_file, t) for t in ("api_keys", "sessions", "usage_events", "kv")}
    s = Store(v01_file)
    assert s.schema_version() == SCHEMA_VERSION
    s.close()
    after = {t: _dump(v01_file, t) for t in before}
    for table, rows in before.items():
        assert len(after[table]) == len(rows)
        for old, new in zip(rows, after[table], strict=True):
            assert {k: new[k] for k in old} == old, table  # every old value kept
    k1 = after["api_keys"][0]
    assert (k1["encryption"], k1["reset"], k1["label"], k1["tier"], k1["sig_public_key"],
            k1["budget_tokens"]) == ("optional", 0, "", None, None, None)
    assert after["sessions"][0]["origin"] == ""


def test_upgraded_file_still_works_and_reopens(v01_file):
    s = Store(v01_file)
    rec = s.find_api_key(OLD_KEY)
    assert rec.id == "key_1" and rec.username == "alice" and rec.encryption == "optional"
    assert s.get_session(SID).username == "alice"
    assert s.tokens_since("alice", 0) == 15
    assert s.kv_get("public_url")[0] == "https://x.example"
    assert [k.id for k in s.list_api_keys("bob", include_revoked=True)] == ["key_2"]
    _, guest = s.create_guest_key("visitor")  # username may now be NULL
    assert s.get_api_key(guest.id).username is None
    s.record_usage(UsageEvent(username="alice", endpoint="e", model="m", backend="b",
                              prompt_tokens=1))
    s.close()
    s2 = Store(v01_file)  # second open: nothing to migrate, nothing lost
    assert s2.get_api_key(guest.id).is_guest
    assert s2.find_api_key(OLD_KEY).id == "key_1"
    assert s2.tokens_since("alice", 0) == 16
    cols = {r[1]: r for r in s2._db.execute("PRAGMA table_info(api_keys)")}
    assert cols["username"][3] == 0  # notnull flag cleared
    indexes = {r[1] for r in s2._db.execute("PRAGMA index_list(api_keys)")}
    assert "ix_api_keys_user" in indexes
    s2.close()


def test_new_file_has_all_tables(tmp_path):
    s = Store(tmp_path / "new.sqlite3")
    names = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"api_keys", "sessions", "usage_events", "kv", "workers", "worker_keys",
            "worker_models", "buckets", "api_link"} <= names
    assert s.schema_version() == SCHEMA_VERSION


def test_session_origin():
    s = Store(":memory:")
    sid, sess = s.create_session("alice", 60, origin="public")
    assert sess.origin == "public" and s.get_session(sid).origin == "public"
    sid2, _ = s.create_session("alice", 60)
    assert s.get_session(sid2).origin == ""


def test_new_key_columns_roundtrip():
    s = Store(":memory:")
    plain, rec = s.create_api_key("alice", "k", sig_public_key="pub", encryption="required",
                                  label="lbl")
    got = s.find_api_key(plain)
    assert (got.sig_public_key, got.encryption, got.label, got.tier, got.is_guest) == \
        ("pub", "required", "lbl", None, False)
    assert s.get_api_key(rec.id) == got


def test_buckets_and_api_link_tables():
    s = Store(":memory:")
    assert s.get_bucket("acct:a") is None
    s.save_bucket("acct:a", level=5.0, updated_at=10.0, capacity=80.0)
    row = s.get_bucket("acct:a")
    assert (row["level"], row["updated_at"], row["low"], row["period_used"]) == (5.0, 10.0, 0, 0)
    with pytest.raises(ValueError):
        s.save_bucket("acct:a", level=1.0)
    assert s.get_api_link() is None
    s.set_api_link("hash1", now=1.0)
    s.set_api_link("hash2", "hash1", grace_until=100.0, now=2.0)
    assert s.get_api_link() == {"current_gate_hash": "hash2", "previous_gate_hash": "hash1",
                                "grace_until": 100.0, "updated_at": 2.0}


# ------------------------------------------------------------------ workers (from B)
def test_worker_enrolment_lifecycle():
    s = Store(":memory:")
    w, reg = s.create_worker("pc", now=100)
    assert reg.startswith("llmop_reg_") and w.reg_expires_at == 100 + 3600
    with pytest.raises(ProtocolError) as ei:
        s.register_worker(reg, now=100 + 3600)  # expired
    assert ei.value.code == "unauthorized"
    _, reg = s.reissue_reg_token(w.id, now=200)
    wid, token = s.register_worker(reg, now=201)
    assert wid == w.id and token.startswith("llmop_wk_")
    with pytest.raises(ProtocolError):
        s.register_worker(reg, now=202)  # single use
    assert s.authenticate_worker(w.id, token).id == w.id
    with pytest.raises(ProtocolError):
        s.authenticate_worker(w.id, "llmop_wk_wrong")
    s.touch_worker(w.id, now=300)
    assert s.get_worker(w.id).last_seen_at == 300
    assert [x.id for x in s.list_workers()] == [w.id]
    assert s.revoke_worker(w.id, now=400).revoked_at == 400
    with pytest.raises(ProtocolError):
        s.authenticate_worker(w.id, token)
    assert s.reissue_reg_token(w.id) is None


def test_worker_keys_and_models():
    s = Store(":memory:")
    a, _ = s.create_worker("a")
    b, _ = s.create_worker("b")
    s.record_worker_keys(a.id, {"k1": "pub1"}, now=5)
    s.record_worker_keys(a.id, {"k1": "pub1", "k2": "pub2"}, now=6)  # repeat is fine
    assert [k["key_id"] for k in s.worker_keys(a.id)] == ["k1", "k2"]
    with pytest.raises(ProtocolError) as ei:
        s.record_worker_keys(b.id, {"k3": "pub3", "k1": "pub1"})
    assert ei.value.code == "key_conflict"
    assert s.worker_keys(b.id) == []  # whole batch rolled back
    s.set_worker_models(a.id, ["m2", "m1"])
    assert s.worker_models(a.id) == ["m1", "m2"]
    s.set_worker_models(a.id, ["m3"])
    assert s.worker_models(a.id) == ["m3"]
