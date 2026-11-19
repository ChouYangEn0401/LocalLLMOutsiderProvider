"""Priority scheduler for the global concurrency slots (TIERS section 7).

A pure asyncio component: no HTTP, no other llmop imports. Usage::

    async with scheduler.slot(account, tier, zone) as ticket:
        ...  # one slot is held for the duration of the block

Dequeue rules (TIERS 7.2):

1. Tier order ``vip3 > vip2 > vip1 > normal > guest > low``. Running requests
   are never interrupted.
2. Within a tier, accounts take turns (round robin); within an account, first
   come first served. Each account is limited by its tier's ``max_concurrent``.
3. Fair share: of every ``fair_share_every`` grants, at least one goes to the
   longest-waiting NORMAL or GUEST request, if one is waiting. LOW is excluded.

Limits (TIERS 7.3): ``max_length`` (whole queue -> ``QueueFull(reason="queue")``,
HTTP 503), ``max_per_account`` (waiting requests per account ->
``QueueFull(reason="account")``, HTTP 429) and ``max_wait_seconds`` per zone
(-> ``QueueTimeout``, HTTP 429).

ETA (TIERS 7.4): requests ahead x average duration of the last 20 runs / slots.

The clock and the sleep function are injectable so tests can use a fake clock.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time
from collections import deque
from typing import Awaitable, Callable, Mapping

TIERS: tuple[str, ...] = ("vip3", "vip2", "vip1", "normal", "guest", "low")
"""Tier names in dequeue order (index 0 is served first)."""

DEFAULT_MAX_CONCURRENT: dict[str, int] = {
    "vip3": 3,
    "vip2": 2,
    "vip1": 2,
    "normal": 1,
    "guest": 1,
    "low": 1,
}
DEFAULT_MAX_WAIT: dict[str, float] = {"public": 60.0, "local": 600.0}
ZONES: tuple[str, ...] = ("local", "public")
FAIR_TIERS: frozenset[str] = frozenset({"normal", "guest"})
DURATION_HISTORY = 20
DEFAULT_DURATION = 20.0
"""Assumed duration (seconds) of one request before any has finished."""


class QueueFull(Exception):
    """The queue (``reason="queue"``, 503) or the account's share of it
    (``reason="account"``, 429) is full. ``retry_after`` is whole seconds."""

    def __init__(self, retry_after: int, reason: str = "queue"):
        super().__init__(f"queue full ({reason}), retry in ~{retry_after}s")
        self.retry_after = retry_after
        self.reason = reason


class QueueTimeout(Exception):
    """Waited longer than the zone's ``max_wait``. ``position`` is the position
    the request had when it gave up; ``retry_after`` is whole seconds."""

    def __init__(self, position: int, retry_after: int):
        super().__init__(f"queue position {position}, retry in ~{retry_after}s")
        self.position = position
        self.retry_after = retry_after


class Ticket:
    """One request's place in the scheduler.

    ``state`` is ``"new"`` (not yet queued), ``"waiting"``, ``"running"``,
    ``"done"``, ``"cancelled"``, ``"timeout"`` or ``"rejected"``.
    """

    __slots__ = (
        "account", "tier", "zone", "enqueued_at", "started_at", "finished_at",
        "state", "_seq", "_future", "_scheduler",
    )

    def __init__(self, scheduler: "Scheduler", account: str, tier: str, zone: str):
        self._scheduler = scheduler
        self.account = account
        self.tier = tier
        self.zone = zone
        self.enqueued_at: float | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.state = "new"
        self._seq = 0
        self._future: asyncio.Future[None] | None = None

    @property
    def waited(self) -> float:
        """Seconds spent waiting (so far, if still waiting)."""
        if self.enqueued_at is None:
            return 0.0
        end = self.started_at if self.started_at is not None else self._scheduler._clock()
        return max(0.0, end - self.enqueued_at)

    def position(self) -> int | None:
        """1-based queue position while waiting, 0 while running, else None."""
        return self._scheduler.position(self)

    def eta(self) -> float | None:
        """Estimated seconds until a slot (0.0 while running), else None."""
        return self._scheduler.eta(self)

    def __repr__(self) -> str:
        return f"<Ticket {self.account!r} {self.tier} {self.zone} {self.state}>"


class _Slot:
    """Return value of :meth:`Scheduler.slot`. ``ticket`` exists before entry
    so a caller can report the queue position while ``__aenter__`` waits."""

    def __init__(self, scheduler: "Scheduler", ticket: Ticket):
        self._scheduler = scheduler
        self.ticket = ticket

    async def __aenter__(self) -> Ticket:
        await self._scheduler._acquire(self.ticket)
        return self.ticket

    async def __aexit__(self, *exc: object) -> None:
        self._scheduler._release(self.ticket, "done")


class Scheduler:
    def __init__(
        self,
        *,
        slots: int = 6,
        max_length: int = 30,
        max_per_account: int = 2,
        max_wait_seconds: Mapping[str, float] | None = None,
        fair_share_every: int = 6,
        max_concurrent: Mapping[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        if slots < 1:
            raise ValueError("slots must be >= 1")
        if max_length < 0 or max_per_account < 0:
            raise ValueError("queue limits must be >= 0")
        if fair_share_every < 1:
            raise ValueError("fair_share_every must be >= 1")
        self.slots = slots
        self.max_length = max_length
        self.max_per_account = max_per_account
        self.max_wait_seconds = {**DEFAULT_MAX_WAIT, **(max_wait_seconds or {})}
        self.fair_share_every = fair_share_every
        self.max_concurrent = {**DEFAULT_MAX_CONCURRENT, **(max_concurrent or {})}
        unknown = set(self.max_concurrent) - set(TIERS)
        if unknown:
            raise ValueError(f"unknown tier(s): {sorted(unknown)}")
        self._clock = clock
        self._sleep = sleep
        self._seq = itertools.count(1)
        # tier -> rotation of accounts; account -> FIFO of its waiting tickets
        self._rotation: dict[str, deque[str]] = {t: deque() for t in TIERS}
        self._waiting: dict[str, dict[str, deque[Ticket]]] = {t: {} for t in TIERS}
        self._n_waiting = 0
        self._running: set[Ticket] = set()
        self._running_by_account: dict[str, int] = {}
        self._waiting_by_account: dict[str, int] = {}
        self._since_fair = 0
        self._durations: deque[float] = deque(maxlen=DURATION_HISTORY)

    # ------------------------------------------------------------------ public

    def slot(self, account: str, tier: str, zone: str) -> _Slot:
        """Async context manager holding one slot; yields the :class:`Ticket`.

        Raises ``QueueFull`` or ``QueueTimeout`` from ``__aenter__``. Cancelling
        the waiting task removes the request from the queue."""
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        if zone not in ZONES:
            raise ValueError(f"unknown zone {zone!r}")
        return _Slot(self, Ticket(self, account, tier, zone))

    @property
    def running(self) -> int:
        return len(self._running)

    @property
    def waiting(self) -> int:
        return self._n_waiting

    def average_duration(self) -> float:
        if not self._durations:
            return DEFAULT_DURATION
        return sum(self._durations) / len(self._durations)

    def position(self, ticket: Ticket) -> int | None:
        """1-based position in dispatch order (tier order, then round robin),
        ignoring per-account limits and the fair-share rule. 0 while running."""
        if ticket.state == "running":
            return 0
        if ticket.state != "waiting":
            return None
        ahead = 0
        own = TIERS.index(ticket.tier)
        for tier in TIERS[:own]:
            ahead += sum(len(q) for q in self._waiting[tier].values())
        queues = self._waiting[ticket.tier]
        k = queues[ticket.account].index(ticket)
        before_me = True
        for acct in self._rotation[ticket.tier]:
            if acct == ticket.account:
                before_me = False
                continue
            n = len(queues[acct])
            ahead += min(n, k + 1) if before_me else min(n, k)
        return ahead + k + 1

    def eta(self, ticket: Ticket) -> float | None:
        """Requests ahead x average duration / slots (TIERS 7.4)."""
        pos = self.position(ticket)
        if pos is None:
            return None
        if pos == 0:
            return 0.0
        return (pos - 1) * self.average_duration() / self.slots

    def snapshot(self) -> dict:
        """Plain-data view of the queue for status pages and ``/v1/me``."""
        by_tier = {t: {"waiting": 0, "running": 0} for t in TIERS}
        accounts: dict[str, dict] = {}

        def acct(name: str) -> dict:
            return accounts.setdefault(
                name, {"running": 0, "waiting": 0, "position": None, "eta": None})

        for t in self._running:
            by_tier[t.tier]["running"] += 1
            acct(t.account)["running"] += 1
        for tier in TIERS:
            for name, q in self._waiting[tier].items():
                by_tier[tier]["waiting"] += len(q)
                a = acct(name)
                a["waiting"] += len(q)
                for t in q:
                    pos = self.position(t)
                    if a["position"] is None or pos < a["position"]:
                        a["position"] = pos
                        a["eta"] = self.eta(t)
        return {
            "slots": self.slots,
            "running": self.running,
            "waiting": self.waiting,
            "max_length": self.max_length,
            "average_duration": self.average_duration(),
            "by_tier": by_tier,
            "accounts": accounts,
        }

    # ---------------------------------------------------------------- internal

    def _retry_after(self, requests_ahead: int) -> int:
        return max(1, math.ceil(requests_ahead * self.average_duration() / self.slots))

    async def _acquire(self, ticket: Ticket) -> None:
        if ticket.state != "new":
            raise RuntimeError("a slot can be entered only once")
        loop = asyncio.get_running_loop()
        ticket._future = loop.create_future()
        ticket._seq = next(self._seq)
        ticket.enqueued_at = self._clock()
        self._enqueue(ticket)
        self._pump()
        if ticket.state == "running":
            return
        # Limits apply only to requests that actually have to wait.
        if self._n_waiting - 1 >= self.max_length:
            self._dequeue(ticket, "rejected")
            raise QueueFull(self._retry_after(self._n_waiting), "queue")
        if self._waiting_by_account.get(ticket.account, 0) - 1 >= self.max_per_account:
            pos = self.position(ticket) or 1
            self._dequeue(ticket, "rejected")
            raise QueueFull(self._retry_after(pos), "account")

        timer = asyncio.ensure_future(self._sleep(self.max_wait_seconds[ticket.zone]))
        try:
            await asyncio.wait({ticket._future, timer}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            if ticket.state == "running":
                self._release(ticket, "cancelled")
            elif ticket.state == "waiting":
                self._dequeue(ticket, "cancelled")
                self._pump()
            raise
        finally:
            timer.cancel()
        if ticket.state == "running":
            return
        pos = self.position(ticket) or 1
        self._dequeue(ticket, "timeout")
        self._pump()
        raise QueueTimeout(pos, self._retry_after(pos))

    def _enqueue(self, ticket: Ticket) -> None:
        queues = self._waiting[ticket.tier]
        if ticket.account not in queues:
            queues[ticket.account] = deque()
            self._rotation[ticket.tier].append(ticket.account)
        queues[ticket.account].append(ticket)
        self._n_waiting += 1
        self._waiting_by_account[ticket.account] = (
            self._waiting_by_account.get(ticket.account, 0) + 1)
        ticket.state = "waiting"

    def _dequeue(self, ticket: Ticket, state: str) -> None:
        queues = self._waiting[ticket.tier]
        q = queues[ticket.account]
        q.remove(ticket)
        if not q:
            del queues[ticket.account]
            self._rotation[ticket.tier].remove(ticket.account)
        self._n_waiting -= 1
        n = self._waiting_by_account[ticket.account] - 1
        if n:
            self._waiting_by_account[ticket.account] = n
        else:
            del self._waiting_by_account[ticket.account]
        ticket.state = state

    def _eligible(self, ticket: Ticket) -> bool:
        return (self._running_by_account.get(ticket.account, 0)
                < self.max_concurrent[ticket.tier])

    def _pick_by_priority(self) -> Ticket | None:
        for tier in TIERS:
            queues = self._waiting[tier]
            for acct in self._rotation[tier]:
                head = queues[acct][0]
                if self._eligible(head):
                    return head
        return None

    def _oldest_fair(self) -> Ticket | None:
        best: Ticket | None = None
        for tier in FAIR_TIERS:
            for q in self._waiting[tier].values():
                head = q[0]  # FIFO per account: the head is that account's oldest
                if self._eligible(head) and (best is None or head._seq < best._seq):
                    best = head
        return best

    def _pump(self) -> None:
        while len(self._running) < self.slots and self._n_waiting:
            fair = self._oldest_fair()
            if fair is not None and self._since_fair >= self.fair_share_every - 1:
                chosen = fair
            else:
                chosen = self._pick_by_priority()
            if chosen is None:
                return
            if chosen.tier in FAIR_TIERS:
                self._since_fair = 0
            elif fair is not None:
                self._since_fair += 1
            self._grant(chosen)

    def _grant(self, ticket: Ticket) -> None:
        rotation = self._rotation[ticket.tier]
        self._dequeue(ticket, "running")
        if ticket.account in rotation:  # served: go to the back of the line
            rotation.remove(ticket.account)
            rotation.append(ticket.account)
        self._running.add(ticket)
        self._running_by_account[ticket.account] = (
            self._running_by_account.get(ticket.account, 0) + 1)
        ticket.started_at = self._clock()
        assert ticket._future is not None
        if not ticket._future.done():
            ticket._future.set_result(None)

    def _release(self, ticket: Ticket, state: str) -> None:
        if ticket.state != "running":
            return
        self._running.discard(ticket)
        n = self._running_by_account[ticket.account] - 1
        if n:
            self._running_by_account[ticket.account] = n
        else:
            del self._running_by_account[ticket.account]
        ticket.finished_at = self._clock()
        if state == "done" and ticket.started_at is not None:
            self._durations.append(max(0.0, ticket.finished_at - ticket.started_at))
        ticket.state = state
        self._pump()


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_WAIT",
    "QueueFull",
    "QueueTimeout",
    "Scheduler",
    "TIERS",
    "Ticket",
]
