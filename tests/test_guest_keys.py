"""TIERS section 8: GUEST keys (no account, own tables, still visible after they die),
plus the per-key ``encryption`` flag and ``auth.key_allows_plaintext``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmop.auth import key_allows_plaintext
from llmop.quota import QuotaDenied, QuotaManager
from llmop.store import Store
from llmop.usage import UsageManager

T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def e(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("llmop.store.time.time", clock)  # key timestamps on the fake clock
    store = Store(":memory:")
    um = UsageManager(store, None, clock=clock)
    return SimpleNamespace(clock=clock, store=store, um=um,
                           qm=QuotaManager(store, clock=clock, tables=um))


def test_guest_key_record(e):
    plain, rec = e.store.create_guest_key("visitor", expires_at=T0 + 86400, encryption="required")
    assert plain.startswith("sk-llop-")
    assert rec.username is None and rec.is_guest and rec.tier == "guest"
    assert rec.label == "visitor" and rec.encryption == "required" and rec.reset is False
    got = e.store.find_api_key(plain)
    assert got.id == rec.id and got.username is None
    assert [k.id for k in e.store.list_guest_keys()] == [rec.id]
    assert e.store.list_api_keys("visitor") == []


def test_guest_tables_and_bucket_429_without_low(e):
    _, k = e.store.create_guest_key("g")
    st = e.um.status(key=k)
    assert st["tier"] == "guest" and st["bucket"]["capacity"] == 20_000
    assert st["guest"]["budget"] == 50_000 and st["guest"]["status"] == "active"
    e.um.charge(20_000, key=k)
    with pytest.raises(QuotaDenied) as ei:
        e.qm.acquire(None, k)
    assert ei.value.code == "rate_limit_exceeded" and ei.value.status == 429
    assert ei.value.retry_after >= 1
    assert e.um.status(key=k)["state"] == "NORMAL"  # GUEST never becomes LOW
    e.clock.t += 60
    lease = e.qm.acquire(None, k)
    assert lease.queue_tier == "guest"
    with pytest.raises(QuotaDenied, match="same time"):  # GUEST max_concurrent = 1
        e.qm.acquire(None, k)
    lease.release()
    e.qm.acquire(None, k).release()


def test_budget_used_up_kills_key_but_stays_visible(e):
    plain, k = e.store.create_guest_key("g", budget_tokens=1000)
    e.um.charge(400, key=k)
    e.clock.t += 7 * 86400  # no weekly reset by default
    e.um.charge(600, key=k)
    adm = e.um.check(key=k)
    assert not adm.allowed and adm.code == "key_exhausted" and adm.status == 401
    with pytest.raises(QuotaDenied) as ei:
        e.qm.acquire(None, k)
    assert ei.value.status == 401
    e.clock.t += 30 * 86400
    dead = e.store.lookup_api_key(plain)  # read-only dashboard can still find it
    st = e.um.status(key=dead)
    assert st["guest"]["status"] == "exhausted" and st["guest"]["since"] == T0 + 7 * 86400
    assert st["guest"]["used"] == 1000 and st["guest"]["remaining"] == 0


def test_resetting_guest_key_waits_for_reset_instead_of_dying(e):
    _, k = e.store.create_guest_key("g", budget_tokens=1000, reset=True)
    e.um.charge(1000, key=k)
    adm = e.um.check(key=k)
    assert not adm.allowed and adm.code == "insufficient_quota" and adm.status == 429
    st = e.um.status(key=k)
    assert st["guest"]["status"] == "used_up_until_reset"
    e.clock.t = st["guest"]["resets_at"]
    assert e.um.check(key=k).allowed
    assert e.um.status(key=k)["guest"]["used"] == 0


def test_expired_and_revoked_guest_keys(e):
    plain, k = e.store.create_guest_key("g", expires_at=T0 + 100)
    e.clock.t += 101
    assert e.store.find_api_key(plain) is None
    dead = e.store.lookup_api_key(plain)
    adm = e.um.check(key=dead)
    assert adm.code == "key_expired" and adm.status == 401
    st = e.um.status(key=dead)
    assert st["guest"]["status"] == "expired" and st["guest"]["since"] == T0 + 100
    plain2, k2 = e.store.create_guest_key("h")
    assert e.store.revoke_api_key(k2.id)
    assert e.um.status(key=e.store.lookup_api_key(plain2))["guest"]["status"] == "revoked"
    assert e.store.lookup_api_key("sk-llop-nope") is None


def test_encryption_flag_and_key_allows_plaintext(e):
    _, k = e.store.create_api_key("alice", "laptop")
    assert k.encryption == "optional" and key_allows_plaintext(k)
    upd = e.store.update_api_key(k.id, {"encryption": "required", "sig_public_key": "pk"})
    assert upd.encryption == "required" and upd.sig_public_key == "pk"
    assert not key_allows_plaintext(upd)
    assert not key_allows_plaintext(k, SimpleNamespace(disabled=True))
    assert e.store.update_api_key(k.id, {"label": "x"}, username="bob") is None  # not bob's
    with pytest.raises(ValueError):
        e.store.update_api_key(k.id, {"encryption": "maybe"})
    with pytest.raises(ValueError):
        e.store.update_api_key(k.id, {"username": "bob"})
    with pytest.raises(ValueError):
        e.store.create_api_key("alice", encryption="sometimes")
