"""Tests for llmop.scheduler (TIERS 7.1-7.4) using a fake clock."""

from __future__ import annotations

import asyncio
import random

import pytest

from llmop.scheduler import (
    DEFAULT_MAX_CONCURRENT,
    QueueFull,
    QueueTimeout,
    Scheduler,
    TIERS,
)


class FakeClock:
    """Injectable clock + sleep. Time only moves on ``advance``."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, fut))
        try:
            await fut
        finally:
            self._sleepers = [s for s in self._sleepers if s[1] is not fut]

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for deadline, fut in list(self._sleepers):
            if deadline <= self.now and not fut.done():
                fut.set_result(None)
        await settle()


async def settle(rounds: int = 30) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def make(clock: FakeClock, **kw) -> Scheduler:
    return Scheduler(clock=clock, sleep=clock.sleep, **kw)


class Holder:
    """Runs ``async with scheduler.slot(...)`` in a task until released."""

    def __init__(self, sched: Scheduler, account: str, tier: str = "normal",
                 zone: str = "local", log: list | None = None):
        self.sched = sched
        self.account = account
        self.tier = tier
        self.ctx = sched.slot(account, tier, zone)
        self.ticket = self.ctx.ticket
        self.entered = asyncio.Event()
        self._done = asyncio.Event()
        self.error: BaseException | None = None
        self.log = log
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        try:
            async with self.ctx:
                if self.log is not None:
                    self.log.append(self.account)
                self.entered.set()
                await self._done.wait()
        except (QueueFull, QueueTimeout) as e:
            self.error = e

    async def release(self) -> None:
        self._done.set()
        await settle()


def assert_empty(sched: Scheduler) -> None:
    assert sched.running == 0
    assert sched.waiting == 0
    assert sched._running == set()
    assert sched._running_by_account == {}
    assert sched._waiting_by_account == {}
    assert all(not q for q in sched._waiting.values())
    assert all(not r for r in sched._rotation.values())


# --------------------------------------------------------------- basic slots

async def test_immediate_grant_and_release():
    clock = FakeClock()
    s = make(clock)
    async with s.slot("alice", "normal", "public") as t:
        assert (t.account, t.tier, t.zone, t.state) == ("alice", "normal", "public", "running")
        assert s.running == 1
        assert t.position() == 0 and t.eta() == 0.0
        assert t.waited == 0.0
    assert t.state == "done"
    assert t.position() is None and t.eta() is None
    assert_empty(s)


async def test_global_slots_limit():
    clock = FakeClock()
    s = make(clock, slots=2)
    hs = [Holder(s, f"u{i}", "vip3") for i in range(3)]
    await settle()
    assert s.running == 2 and s.waiting == 1
    assert hs[2].ticket.state == "waiting"
    await clock.advance(5)
    await hs[0].release()
    assert hs[2].entered.is_set()
    assert hs[2].ticket.waited == 5.0
    for h in hs[1:]:
        await h.release()
    assert_empty(s)


async def test_body_exception_releases_slot():
    s = make(FakeClock(), slots=1)
    with pytest.raises(RuntimeError):
        async with s.slot("a", "normal", "local"):
            raise RuntimeError("boom")
    assert_empty(s)


async def test_invalid_tier_and_zone():
    s = make(FakeClock())
    with pytest.raises(ValueError):
        s.slot("a", "gold", "local")
    with pytest.raises(ValueError):
        s.slot("a", "normal", "lan")
    with pytest.raises(ValueError):
        Scheduler(slots=0)


# ------------------------------------------------------------- dequeue rules

async def test_tier_order():
    clock = FakeClock()
    s = make(clock, slots=1, fair_share_every=100)
    log: list[str] = []
    blocker = Holder(s, "blocker", "vip3")
    await settle()
    waiters = [Holder(s, tier, tier, log=log) for tier in reversed(TIERS)]
    await settle()
    assert s.waiting == 6
    await blocker.release()
    for h in waiters:
        # release whichever is running
        running = next(w for w in waiters if w.ticket.state == "running")
        await running.release()
    assert log == list(TIERS)
    assert_empty(s)


async def test_running_request_not_interrupted():
    s = make(FakeClock(), slots=1)
    low = Holder(s, "l", "low")
    await settle()
    vip = Holder(s, "v", "vip3")
    await settle()
    assert low.ticket.state == "running" and vip.ticket.state == "waiting"
    await low.release()
    assert vip.ticket.state == "running"
    await vip.release()
    assert_empty(s)


async def test_round_robin_within_tier_and_fifo_within_account():
    s = make(FakeClock(), slots=1, max_per_account=5)
    log: list[str] = []
    blocker = Holder(s, "blocker", "vip3")
    await settle()
    hs = []
    for name in ["A1", "A2", "A3", "B1", "B2", "C1"]:
        h = Holder(s, name[0], "vip3", log=log)
        h.name = name
        hs.append(h)
        await settle()
    # positions: round robin A, B, C, A, B, A
    pos = {h.name: h.ticket.position() for h in hs}
    assert pos == {"A1": 1, "B1": 2, "C1": 3, "A2": 4, "B2": 5, "A3": 6}
    order = []
    await blocker.release()
    for _ in hs:
        running = next(h for h in hs if h.ticket.state == "running")
        order.append(running.name)
        await running.release()
    assert order == ["A1", "B1", "C1", "A2", "B2", "A3"]
    assert_empty(s)


async def test_per_account_concurrency_limit():
    s = make(FakeClock(), slots=6, max_per_account=5)
    n1, n2 = Holder(s, "n", "normal"), Holder(s, "n", "normal")
    v = [Holder(s, "v", "vip3") for _ in range(4)]
    other = Holder(s, "m", "normal")
    await settle()
    assert n1.ticket.state == "running" and n2.ticket.state == "waiting"
    assert [h.ticket.state for h in v].count("running") == DEFAULT_MAX_CONCURRENT["vip3"]
    assert other.ticket.state == "running"
    assert s.running == 5  # one slot free but nobody eligible
    await n1.release()
    assert n2.ticket.state == "running"
    for h in [n2, other, *v]:
        if h.ticket.state == "running":
            await h.release()
    for h in v:
        if h.ticket.state == "running":
            await h.release()
    assert_empty(s)


async def test_max_concurrent_override():
    s = make(FakeClock(), slots=6, max_concurrent={"normal": 2})
    hs = [Holder(s, "n", "normal") for _ in range(2)]
    await settle()
    assert all(h.ticket.state == "running" for h in hs)
    for h in hs:
        await h.release()
    with pytest.raises(ValueError):
        Scheduler(max_concurrent={"gold": 1})


async def test_low_only_when_nobody_else_waits():
    s = make(FakeClock(), slots=1, max_per_account=5)
    blocker = Holder(s, "b", "vip3")
    await settle()
    low = Holder(s, "l", "low")
    others = [Holder(s, f"g{i}", "guest") for i in range(3)]
    await settle()
    await blocker.release()
    for h in others:
        assert low.ticket.state == "waiting"
        await h.release()
    assert low.ticket.state == "running"
    await low.release()
    assert_empty(s)


async def test_fair_share_every_six():
    s = make(FakeClock(), slots=1, max_per_account=5)
    blocker = Holder(s, "blocker", "vip3")
    await settle()
    vips = [Holder(s, f"v{i}", "vip3") for i in range(12)]
    await settle()
    low = Holder(s, "low", "low")
    guest = Holder(s, "g", "guest")       # waits longest of NORMAL/GUEST
    await settle()
    normal = Holder(s, "n", "normal")
    await settle()
    everyone = vips + [low, guest, normal]
    order = []
    await blocker.release()
    for _ in everyone:
        running = next(h for h in everyone if h.ticket.state == "running")
        order.append(running.account)
        await running.release()
    # 5 VIP grants, then the oldest NORMAL/GUEST (guest), 5 VIP, then normal ...
    assert order[:6] == ["v0", "v1", "v2", "v3", "v4", "g"]
    assert order[6:12] == ["v5", "v6", "v7", "v8", "v9", "n"]
    assert order[12:14] == ["v10", "v11"]
    assert order[-1] == "low"  # LOW gets no fair share
    assert_empty(s)


async def test_fair_share_counter_only_counts_while_fair_waiting():
    s = make(FakeClock(), slots=1, max_per_account=5)
    blocker = Holder(s, "blocker", "vip3")
    await settle()
    vips = [Holder(s, f"v{i}", "vip3") for i in range(14)]
    await settle()
    order = []
    await blocker.release()
    for _ in range(4):  # 4 VIP grants with no NORMAL waiting
        running = next(h for h in vips if h.ticket.state == "running")
        order.append(running.account)
        await running.release()
    normal = Holder(s, "n", "normal")
    await settle()
    everyone = vips + [normal]
    while any(h.ticket.state in ("waiting", "running") for h in everyone):
        running = next(h for h in everyone if h.ticket.state == "running")
        order.append(running.account)
        await running.release()
    assert order.index("n") == 4 + 1 + 5  # v3 ran + 5 more VIP grants, then normal
    assert_empty(s)


# -------------------------------------------------------------------- limits

async def test_max_length_queue_full():
    s = make(FakeClock(), slots=1, max_length=2)
    hs = [Holder(s, f"u{i}", "vip3") for i in range(3)]
    await settle()
    extra = Holder(s, "x", "vip3")
    await settle()
    assert isinstance(extra.error, QueueFull)
    assert extra.error.reason == "queue" and extra.error.retry_after >= 1
    assert extra.ticket.state == "rejected"
    assert s.waiting == 2
    for _ in hs:
        await next(h for h in hs if h.ticket.state == "running").release()
    assert_empty(s)


async def test_max_per_account_queue_full():
    s = make(FakeClock(), slots=1, max_per_account=2)
    blocker = Holder(s, "b", "vip3")
    hs = [Holder(s, "a", "vip3") for _ in range(2)]
    await settle()
    extra = Holder(s, "a", "vip3")
    await settle()
    assert isinstance(extra.error, QueueFull) and extra.error.reason == "account"
    assert extra.error.retry_after >= 1
    # another account is still accepted
    ok = Holder(s, "c", "vip3")
    await settle()
    assert ok.error is None and ok.ticket.state == "waiting"
    for _ in range(4):
        await next(h for h in [blocker, *hs, ok] if h.ticket.state == "running").release()
    assert_empty(s)


async def test_limits_do_not_apply_when_running_immediately():
    s = make(FakeClock(), slots=2, max_length=0, max_per_account=0)
    h = Holder(s, "a", "normal")
    await settle()
    assert h.ticket.state == "running"
    other = Holder(s, "b", "normal")
    await settle()
    assert other.ticket.state == "running"
    late = Holder(s, "c", "normal")
    await settle()
    assert isinstance(late.error, QueueFull)
    await h.release()
    await other.release()
    assert_empty(s)


@pytest.mark.parametrize("zone,limit", [("public", 60), ("local", 600)])
async def test_max_wait_timeout(zone, limit):
    clock = FakeClock()
    s = make(clock, slots=1)
    blocker = Holder(s, "b", "normal")
    await settle()
    w = Holder(s, "w", "normal", zone=zone)
    await settle()
    await clock.advance(1)
    ahead = Holder(s, "v", "vip3", zone=zone)   # later, but higher priority
    await settle()
    assert w.ticket.position() == 2
    await clock.advance(limit - 2)
    assert w.ticket.state == "waiting" and w.error is None
    await clock.advance(1)
    assert isinstance(w.error, QueueTimeout)
    assert w.error.position == 2 and w.error.retry_after >= 1
    assert w.ticket.state == "timeout"
    assert ahead.ticket.state == "waiting"
    await blocker.release()
    await ahead.release()
    assert_empty(s)


async def test_max_wait_override():
    clock = FakeClock()
    s = make(clock, slots=1, max_wait_seconds={"public": 5})
    assert s.max_wait_seconds == {"public": 5, "local": 600.0}
    blocker = Holder(s, "b", "normal")
    await settle()
    w = Holder(s, "w", "normal", zone="public")
    await settle()
    await clock.advance(5)
    assert isinstance(w.error, QueueTimeout)
    await blocker.release()
    assert_empty(s)


async def test_timeout_frees_queue_for_others():
    clock = FakeClock()
    s = make(clock, slots=1, max_wait_seconds={"public": 10})
    blocker = Holder(s, "b", "normal")
    await settle()
    pub = Holder(s, "p", "vip3", zone="public")
    loc = Holder(s, "l", "normal", zone="local")
    await settle()
    assert loc.ticket.position() == 2
    await clock.advance(10)
    assert isinstance(pub.error, QueueTimeout)
    assert loc.ticket.position() == 1
    await blocker.release()
    assert loc.ticket.state == "running"
    await loc.release()
    assert_empty(s)


# -------------------------------------------------------------- cancellation

async def test_cancel_waiter_does_not_leak():
    clock = FakeClock()
    s = make(clock, slots=1)
    blocker = Holder(s, "b", "normal")
    await settle()
    w = Holder(s, "w", "normal")
    nxt = Holder(s, "n", "normal")
    await settle()
    w.task.cancel()
    await settle()
    assert w.task.cancelled() and w.ticket.state == "cancelled"
    assert s.waiting == 1
    assert clock._sleepers.__len__() == 1  # w's timer is gone
    await blocker.release()
    assert nxt.ticket.state == "running"
    await nxt.release()
    assert_empty(s)


async def test_cancel_after_grant_before_resume_releases():
    s = make(FakeClock(), slots=1)
    blocker = Holder(s, "b", "normal")
    await settle()
    w = Holder(s, "w", "normal")
    nxt = Holder(s, "n", "normal")
    await settle()
    blocker._done.set()
    # Let the blocker exit (granting w) but cancel w before it resumes.
    while blocker.ticket.state == "running":
        await asyncio.sleep(0)
    assert w.ticket.state == "running" and not w.entered.is_set()
    w.task.cancel()
    await settle()
    assert w.task.cancelled() and w.ticket.state == "cancelled"
    assert nxt.ticket.state == "running"
    await nxt.release()
    assert_empty(s)


async def test_cancel_running_holder_releases():
    s = make(FakeClock(), slots=1)
    h = Holder(s, "a", "normal")
    await settle()
    h.task.cancel()
    await settle()
    assert h.ticket.state == "done"
    assert_empty(s)


# ------------------------------------------------------- position, eta, state

async def test_position_and_eta():
    clock = FakeClock()
    s = make(clock, slots=2, max_per_account=5)
    a, b = Holder(s, "a", "vip3"), Holder(s, "b", "vip3")
    await settle()
    await clock.advance(10)
    await a.release()
    c = Holder(s, "c", "vip3")
    await settle()
    await clock.advance(20)
    await b.release()   # durations 10 and 30 -> average 20
    await c.release()
    assert s.average_duration() == pytest.approx((10 + 30 + 20) / 3)
    s._durations.clear()
    s._durations.extend([10.0, 30.0])
    hs = [Holder(s, f"u{i}", "normal") for i in range(5)]
    await settle()
    waiting = [h for h in hs if h.ticket.state == "waiting"]
    assert [h.ticket.position() for h in waiting] == [1, 2, 3]
    assert waiting[2].ticket.eta() == pytest.approx(2 * 20 / 2)
    assert waiting[0].ticket.eta() == 0.0
    for _ in hs:
        await next(h for h in hs if h.ticket.state == "running").release()
    assert_empty(s)


async def test_eta_default_before_history():
    s = make(FakeClock(), slots=1)
    assert s.average_duration() > 0
    blocker = Holder(s, "b", "normal")
    hs = [Holder(s, f"u{i}", "normal") for i in range(2)]
    await settle()
    assert hs[1].ticket.eta() == pytest.approx(s.average_duration())
    for h in [blocker, *hs]:
        await h.release()
    assert_empty(s)


async def test_duration_history_keeps_last_20():
    clock = FakeClock()
    s = make(clock, slots=1)
    for i in range(25):
        async with s.slot("a", "normal", "local"):
            await clock.advance(100 if i < 5 else 1)
    assert s.average_duration() == 1.0


async def test_position_tier_ahead_counts():
    s = make(FakeClock(), slots=1, max_per_account=5)
    blocker = Holder(s, "b", "vip3")
    await settle()
    n = Holder(s, "n", "normal")
    await settle()
    assert n.ticket.position() == 1
    Holder(s, "v", "vip1")
    Holder(s, "v2", "vip2")
    await settle()
    assert n.ticket.position() == 3
    # clean-up: cancel everything still pending
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task():
            t.cancel()
    await settle()
    assert_empty(s)


async def test_ticket_position_before_entry_is_none():
    s = make(FakeClock())
    ctx = s.slot("a", "normal", "local")
    assert ctx.ticket.state == "new" and ctx.ticket.position() is None
    async with ctx as t:
        assert t is ctx.ticket
    with pytest.raises(RuntimeError):
        async with ctx:
            pass


async def test_snapshot():
    s = make(FakeClock(), slots=1, max_per_account=5)
    blocker = Holder(s, "b", "vip3")
    await settle()
    Holder(s, "a", "normal")
    Holder(s, "a", "normal")
    Holder(s, "g", "guest")
    await settle()
    snap = s.snapshot()
    assert snap["slots"] == 1 and snap["running"] == 1 and snap["waiting"] == 3
    assert snap["max_length"] == 30
    assert snap["by_tier"]["vip3"] == {"waiting": 0, "running": 1}
    assert snap["by_tier"]["normal"] == {"waiting": 2, "running": 0}
    assert snap["by_tier"]["guest"]["waiting"] == 1
    assert snap["accounts"]["a"]["waiting"] == 2
    assert snap["accounts"]["a"]["position"] == 1
    assert snap["accounts"]["g"]["position"] == 3
    assert snap["accounts"]["b"] == {"running": 1, "waiting": 0, "position": None, "eta": None}
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task():
            t.cancel()
    await settle()
    assert_empty(s)


# --------------------------------------------------------- randomized leak test

async def test_1000_random_operations_leave_no_leak():
    rng = random.Random(1234)
    clock = FakeClock()
    s = make(clock, slots=3, max_length=8, max_per_account=2,
             max_wait_seconds={"public": 30, "local": 90})
    accounts = [f"acct{i}" for i in range(7)]
    live: list[Holder] = []
    every: list[Holder] = []

    def check_invariants() -> None:
        assert 0 <= s.running <= s.slots
        assert s.waiting <= s.max_length
        for acct, n in s._running_by_account.items():
            caps = [s.max_concurrent[t.tier] for t in s._running if t.account == acct]
            assert n == len(caps) and n <= max(caps)
        assert sum(s._waiting_by_account.values()) == s.waiting
        # never idle slots while an eligible request waits
        if s.running < s.slots:
            assert s._pick_by_priority() is None

    for _ in range(1000):
        op = rng.random()
        if op < 0.45:
            live.append(Holder(s, rng.choice(accounts), rng.choice(TIERS),
                               rng.choice(["public", "local"])))
            every.append(live[-1])
        elif op < 0.70:
            running = [h for h in live if h.ticket.state == "running" and h.entered.is_set()]
            if running:
                await rng.choice(running).release()
        elif op < 0.85:
            if live:
                rng.choice(live).task.cancel()
        else:
            await clock.advance(rng.choice([1, 5, 15, 40]))
        await settle(5)
        check_invariants()
        live = [h for h in live if not h.task.done()]

    for h in live:
        h.task.cancel()
    await settle()
    assert_empty(s)
    assert clock._sleepers == []
    states = {h.ticket.state for h in every}
    assert {"done", "cancelled", "timeout", "rejected"} <= states
