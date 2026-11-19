"""TIERS_AND_USAGE_RULES.md sections 2, 5.4 and 11: the 1.5-hour bucket and the weekly
table, driven by a fake clock."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from llmop.store import Store
from llmop.usage import (
    UsageManager,
    UsageSettings,
    last_week_reset,
    next_week_reset,
    parse_settings,
    resolve_tier,
)

TPE = ZoneInfo("Asia/Taipei")
# Thursday 2026-11-19 12:00 Taipei; the next reset is Sunday 2026-11-22 04:00.
T0 = datetime(2026, 11, 19, 12, 0, tzinfo=TPE).timestamp()
SUN_0400 = datetime(2026, 11, 22, 4, 0, tzinfo=TPE).timestamp()


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def acct(name="alice", tier="normal", **kw):
    return SimpleNamespace(username=name, tier=tier, disabled=False, **kw)


CFG = SimpleNamespace(usage={"week_reset": {"weekday": "sun", "time": "04:00",
                                            "timezone": "Asia/Taipei"}})


@pytest.fixture
def env():
    clock = Clock()
    store = Store(":memory:")
    return SimpleNamespace(clock=clock, store=store, um=UsageManager(store, CFG, clock=clock))


def first_low(env, a, tokens: int, every: float, limit_s: float) -> float:
    """Send ``tokens`` every ``every`` seconds; return the time the account turns LOW."""
    start = env.clock.t
    while env.clock.t - start < limit_s:
        st = env.um.charge(tokens, a)
        if st["state"] == "LOW":
            return env.clock.t - start + every  # the bucket ran dry within this request
        env.clock.t += every
    raise AssertionError("never became LOW")


# ------------------------------------------------------------------ TIERS 5.4
def test_tiers_5_4_chatting_lasts_about_8_hours(env):
    # One message every 108 s at 1,900 tokens = 17.6 tokens/s against a 14.8 refill.
    t = first_low(env, acct(), 1900, 108, 12 * 3600)
    assert 7.5 * 3600 <= t <= 8.5 * 3600, t / 3600


def test_tiers_5_4_one_busy_slot_empties_in_15_minutes(env):
    # A program holding one slot: 104 tokens/s, i.e. 1,900 tokens every 18.27 s.
    t = first_low(env, acct(), 1900, 1900 / 104, 3600)
    assert 14 * 60 <= t <= 16 * 60, t / 60


def test_tiers_5_4_low_back_to_normal_after_22_5_minutes(env):
    a = acct()
    env.um.charge(80_000, a)  # exactly empty
    assert env.um.status(a)["state"] == "LOW"
    env.clock.t += 22.5 * 60 - 2
    assert env.um.status(a)["state"] == "LOW"
    env.clock.t += 2
    st = env.um.status(a)
    assert st["state"] == "NORMAL" and st["bucket"]["percent"] == pytest.approx(25, abs=0.1)
    env.clock.t += 90 * 60 - 22.5 * 60  # full after 90 minutes
    assert env.um.status(a)["bucket"]["level"] == 80_000


# ------------------------------------------------------------------ bucket
def test_refill_rate_and_cap(env):
    a = acct()
    st = env.um.status(a)
    assert st["bucket"]["capacity"] == 80_000
    assert st["bucket"]["refill_per_second"] == pytest.approx(80_000 / 5400, abs=0.001)
    env.um.charge(10_000, a)
    env.clock.t += 100
    assert env.um.status(a)["bucket"]["level"] == pytest.approx(70_000 + 100 * 80_000 / 5400, abs=0.1)
    env.clock.t += 10_000
    assert env.um.status(a)["bucket"]["level"] == 80_000  # never above C


def test_debt_floor_and_percent(env):
    a = acct()
    env.um.charge(500_000, a)
    st = env.um.status(a)
    assert st["bucket"]["level"] == -20_000  # -25% of C at most
    assert st["bucket"]["percent"] == 0
    assert st["weekly"]["used"] == 500_000  # the weekly table counts all of it


def test_restart_refills_by_elapsed_time(env):
    a = acct()
    env.um.charge(40_000, a)
    env.clock.t += 540  # 1/10 of the window
    um2 = UsageManager(env.store, CFG, clock=env.clock)  # "restart"
    assert um2.status(a)["bucket"]["level"] == pytest.approx(48_000, abs=0.1)


def test_clock_moved_back_refills_nothing(env):
    a = acct()
    env.um.charge(40_000, a)
    env.clock.t -= 3600
    assert env.um.status(a)["bucket"]["level"] == 40_000
    env.um.charge(0, a)
    env.clock.t += 3600  # back to the original time: still no refill counted twice
    assert env.um.status(a)["bucket"]["level"] == 40_000


def test_tier_change_rescales_bucket(env):
    a = acct()
    env.um.charge(60_000, a)  # 25% left
    a.tier = "vip1"
    st = env.um.status(a)
    assert st["bucket"]["capacity"] == 240_000
    assert st["bucket"]["level"] == pytest.approx(60_000)
    env.um.charge(0, a)
    a.tier = "normal"
    assert env.um.status(a)["bucket"]["level"] == pytest.approx(20_000)


def test_same_account_many_keys_share_tables(env):
    a = acct()
    k1 = env.store.create_api_key("alice", "one")[1]
    k2 = env.store.create_api_key("alice", "two")[1]
    env.um.charge(1000, a, k1)
    env.um.charge(2000, a, k2)
    assert env.um.status(a)["weekly"]["used"] == 3000


# ------------------------------------------------------------------ weekly
def test_week_reset_boundaries():
    st = parse_settings(CFG.usage)
    assert next_week_reset(T0, st) == SUN_0400
    assert last_week_reset(SUN_0400, st) == SUN_0400
    assert last_week_reset(SUN_0400 - 1, st) == SUN_0400 - 7 * 86400


def test_weekly_resets_sunday_0400(env):
    a = acct()
    env.um.charge(5000, a)
    env.clock.t = SUN_0400 - 60
    assert env.um.status(a)["weekly"]["used"] == 5000
    env.clock.t = SUN_0400
    st = env.um.status(a)
    assert st["weekly"]["used"] == 0
    assert st["weekly"]["resets_at"] == SUN_0400 + 7 * 86400


def test_request_settled_after_reset_counts_in_new_week(env):
    a = acct()
    env.um.charge(5000, a)
    env.clock.t = SUN_0400 + 5  # a request that started before the reset ends now
    env.um.charge(700, a)
    assert env.um.status(a)["weekly"]["used"] == 700


def test_clock_back_does_not_repeat_reset(env):
    a = acct()
    env.clock.t = SUN_0400 + 10
    env.um.charge(3000, a)
    env.clock.t = SUN_0400 - 10  # host clock moved back across the boundary
    env.um.charge(1000, a)
    env.clock.t = SUN_0400 + 20
    assert env.um.status(a)["weekly"]["used"] == 4000


# ------------------------------------------------------------------ config parsing
def test_parse_settings_defaults_and_object():
    assert parse_settings(None) == UsageSettings()
    obj = SimpleNamespace(window_seconds=3600, recover_at=0.5, debt_floor=0.1, low_mode="block",
                          week_reset=SimpleNamespace(weekday=0, time="09:30", timezone="local"))
    st = parse_settings(obj)
    assert (st.window_seconds, st.recover_at, st.debt_floor, st.low_mode) == (3600, 0.5, 0.1, "block")
    assert (st.weekday, st.hour, st.minute, st.timezone) == (0, 9, 30, "local")


def test_resolve_tier_defaults_config_and_account_override():
    assert resolve_tier("normal").bucket == 80_000
    assert resolve_tier("vip3").bucket is None and resolve_tier("vip3").weekly is None
    assert resolve_tier("vip1").on_bucket_empty == "as_normal"
    assert resolve_tier("bogus").name == "normal"
    tiers = {"normal": {"priority": 4, "max_concurrent": 1, "bucket": 1000}}
    lim = resolve_tier("normal", tiers)
    assert lim.bucket == 1000 and lim.weekly is None  # written tier without weekly = unlimited
    assert resolve_tier("normal", tiers, acct(bucket=300_000)).bucket == 300_000


def test_window_seconds_from_config():
    clock = Clock()
    um = UsageManager(Store(":memory:"), lambda: {"usage": {"window_seconds": 100}}, clock=clock)
    a = acct()
    um.charge(80_000, a)
    clock.t += 50
    assert um.status(a)["bucket"]["level"] == pytest.approx(40_000)


def test_account_effective_limits_win_and_zero_means_unlimited():
    # Shape of config.TierSettings: 0 for bucket / weekly / budget = not limited.
    limits = SimpleNamespace(priority=4, max_concurrent=1, bucket=300_000, weekly=0, budget=0,
                             reset=False, on_bucket_empty="low")
    lim = resolve_tier("normal", None, acct(limits=limits))
    assert lim.bucket == 300_000 and lim.weekly is None
    tiers = {"vip3": SimpleNamespace(priority=1, max_concurrent=3, bucket=0, weekly=0, budget=0,
                                     reset=False, on_bucket_empty="low")}
    assert resolve_tier("vip3", tiers).bucket is None
