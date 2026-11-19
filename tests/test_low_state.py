"""TIERS sections 3 and 4: NORMAL <-> LOW, VIP I drop to the NORMAL queue, VIP II/III
never blocked, and QuotaManager.acquire judging by tier and the two tables."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmop import security
from llmop.config import parse_config
from llmop.quota import QuotaDenied, QuotaManager
from llmop.store import Store
from llmop.usage import UsageManager

FAST_HASH = security.hash_password("pw", n=2**10)
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def make(low_mode="wait", tier="normal", legacy=None):
    cfg = parse_config({
        "backends": [{"name": "ollama", "type": "ollama"}],
        "default_quota": legacy or {},
        "accounts": {"alice": {"password_hash": FAST_HASH}},
    })
    acc = cfg.accounts["alice"]
    acc.tier = tier  # set directly so the test does not depend on the config parser
    acc.limits = None  # ...and resolve the tier's limits from the defaults
    usage_cfg = SimpleNamespace(usage={"low_mode": low_mode})
    clock = Clock()
    store = Store(":memory:")
    um = UsageManager(store, usage_cfg, clock=clock)
    qm = QuotaManager(store, clock=clock, tables=um)
    return SimpleNamespace(acc=acc, clock=clock, store=store, um=um, qm=qm)


def test_normal_becomes_low_when_bucket_hits_zero():
    e = make()
    e.um.charge(79_999, e.acc)
    assert e.um.check(e.acc).state == "NORMAL"
    e.um.charge(1, e.acc)  # exactly 0 -> LOW
    adm = e.um.check(e.acc)
    assert adm.state == "LOW" and adm.queue_tier == "low" and adm.allowed


def test_low_stays_until_25_percent_hysteresis():
    e = make()
    e.um.charge(80_000, e.acc)
    e.clock.t += 600  # ~11% refilled: still LOW even though the bucket is positive
    st = e.um.status(e.acc)
    assert st["bucket"]["level"] > 0 and st["state"] == "LOW"
    assert st["bucket"]["seconds_to_recover"] == pytest.approx(1350 - 600, abs=1)
    e.clock.t += 750
    assert e.um.status(e.acc)["state"] == "NORMAL"
    e.um.charge(10_000, e.acc)  # back down to ~12.5% but > 0: stays NORMAL
    assert e.um.status(e.acc)["state"] == "NORMAL"


def test_low_mode_wait_admits_in_last_queue_position():
    e = make("wait")
    e.um.charge(80_000, e.acc)
    lease = e.qm.acquire(e.acc)
    assert lease.queue_tier == "low" and lease.state == "LOW" and lease.tier == "normal"
    lease.release()


def test_low_mode_block_returns_429_with_retry_after():
    e = make("block")
    e.um.charge(80_000, e.acc)
    with pytest.raises(QuotaDenied) as ei:
        e.qm.acquire(e.acc)
    assert ei.value.code == "rate_limit_exceeded" and ei.value.status == 429
    assert ei.value.retry_after == pytest.approx(1350, abs=2)
    assert "LOW" in ei.value.message


def test_weekly_used_up_means_low_until_reset():
    e = make("block")
    e.um.charge(1_000_000, e.acc)
    e.clock.t += 2 * 3600  # bucket full again, weekly still used up
    st = e.um.status(e.acc)
    assert st["bucket"]["percent"] == 100 and st["state"] == "LOW"
    with pytest.raises(QuotaDenied) as ei:
        e.qm.acquire(e.acc)
    assert ei.value.retry_after == pytest.approx(st["weekly"]["resets_at"] - e.clock.t, abs=2)


def test_vip1_empty_bucket_queues_as_normal_not_low():
    e = make("block", tier="vip1")
    e.um.charge(240_000, e.acc)
    lease = e.qm.acquire(e.acc)
    assert lease.state == "NORMAL" and lease.queue_tier == "normal" and lease.tier == "vip1"
    lease.release()
    e.clock.t += 0.25 * 5400  # back to 25% -> VIP I position again
    assert e.um.check(e.acc).queue_tier == "vip1"


def test_vip1_weekly_used_up_gets_low_treatment():
    e = make("wait", tier="vip1")
    e.um.charge(3_000_000, e.acc)
    assert e.um.check(e.acc).queue_tier == "low"


@pytest.mark.parametrize("tier", ["vip2", "vip3"])
def test_vip2_vip3_never_blocked_but_usage_shown(tier):
    e = make("block", tier=tier)
    e.um.charge(50_000_000, e.acc)
    lease = e.qm.acquire(e.acc)
    assert lease.queue_tier == tier and lease.state == "NORMAL"
    lease.release()
    st = e.um.status(e.acc)
    assert st["bucket"] is None and st["weekly"]["limit"] is None
    assert st["weekly"]["used"] == 50_000_000


def test_admin_fill_returns_to_normal():
    e = make()
    e.um.charge(80_000, e.acc)
    st = e.um.fill(e.acc)
    assert st["state"] == "NORMAL" and st["bucket"]["level"] == 80_000


def test_settle_charges_tables_and_legacy_limits_still_apply():
    e = make(legacy={"requests_per_minute": 1})
    lease = e.qm.acquire(e.acc)
    lease.release()
    e.qm.settle(1234, e.acc)
    assert e.um.status(e.acc)["weekly"]["used"] == 1234
    with pytest.raises(QuotaDenied) as ei:  # A's rpm safety cap is still enforced
        e.qm.acquire(e.acc)
    assert ei.value.code == "rate_limit_exceeded"


def test_without_tables_acquire_behaves_as_before():
    e = make()
    qm = QuotaManager(e.store, clock=e.clock)
    lease = qm.acquire(e.acc)
    assert lease.tier is None and lease.queue_tier is None
    lease.release()
    assert qm.settle(10, e.acc) is None
