"""Quota enforcement.

Token / daily-request limits are counted from the ``usage_events`` table (so they
survive restarts). Requests-per-minute and concurrency are counted in memory.

Enforcement is "soft" like LiteLLM/OpenRouter: we check before the call, clamp the
request's ``max_tokens`` to what is left, and charge the real usage afterwards. One
request can therefore overshoot by at most its own size.

Day/month boundaries use the server's local time zone.

v0.2 (TIERS_AND_USAGE_RULES.md): when a :class:`llmop.usage.UsageManager` is given
(``tables=``), :meth:`QuotaManager.acquire` also judges the caller by tier and the two
usage tables (1.5-hour bucket, weekly table / GUEST budget). VIP II / III are never
blocked by the tables. The legacy ``default_quota`` limits above stay in force when
they are set (``requests_per_minute`` is the documented safety cap). Charging the
tables after a request is :meth:`UsageManager.charge` (or :meth:`QuotaManager.settle`).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from .config import Account
from .store import ApiKey, Store
from .usage import UsageManager


class QuotaDenied(Exception):
    """``code`` follows OpenAI: ``rate_limit_exceeded`` (retry later) or
    ``insufficient_quota`` (wait for the next day/month or ask the admin)."""

    def __init__(self, code: str, message: str, retry_after: int | None = None,
                 status: int = 429):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        # 401 for a dead GUEST key (key_exhausted / key_expired / key_revoked), else 429.
        self.status = status


def day_start(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def month_start(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def utc_offset_seconds() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


@dataclass
class Lease:
    """Granted permission to run one request. Always call :meth:`release`."""

    username: str
    max_tokens: int | None  # cap for the request's max_tokens (None = no cap)
    _mgr: "QuotaManager"
    _released: bool = False
    # Filled when usage tables are in use (else None): configured tier, the tier the
    # scheduler should queue under (vip3..guest or "low"), NORMAL/LOW, table subject.
    tier: str | None = None
    queue_tier: str | None = None
    state: str | None = None
    subject: str | None = None
    _inflight_key: str | None = None

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._mgr._release(self._inflight_key or self.username)


class QuotaManager:
    def __init__(self, store: Store, clock=time.time, tables: UsageManager | None = None):
        self.store = store
        self.clock = clock
        self.tables = tables
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = defaultdict(int)
        self._recent: dict[str, deque] = defaultdict(deque)  # request timestamps (60 s window)

    # ------------------------------------------------------------ helpers
    def _rpm_window(self, username: str, now: float) -> deque:
        q = self._recent[username]
        while q and q[0] <= now - 60:
            q.popleft()
        return q

    def usage(self, account: Account) -> dict:
        """Current counters and limits, for the dashboard and response headers."""
        now = self.clock()
        q = account.quota
        u = account.username
        today = self.store.tokens_since(u, day_start(now))
        month = self.store.tokens_since(u, month_start(now))
        req_today = self.store.requests_since(u, day_start(now))
        with self._lock:
            rpm_used = len(self._rpm_window(u, now))
            inflight = self._inflight.get(u, 0)
        return {
            "tokens_today": today,
            "tokens_month": month,
            "requests_today": req_today,
            "requests_last_minute": rpm_used,
            "in_flight": inflight,
            "limits": {
                "tokens_per_day": q.tokens_per_day,
                "tokens_per_month": q.tokens_per_month,
                "requests_per_minute": q.requests_per_minute,
                "requests_per_day": q.requests_per_day,
                "max_concurrent": q.max_concurrent,
                "max_tokens_per_request": q.max_tokens_per_request,
            },
            "remaining": {
                "tokens_today": None if not q.tokens_per_day else max(0, q.tokens_per_day - today),
                "tokens_month": None if not q.tokens_per_month else max(0, q.tokens_per_month - month),
                "requests_today": None if not q.requests_per_day else max(0, q.requests_per_day - req_today),
                "requests_minute": None if not q.requests_per_minute else max(0, q.requests_per_minute - rpm_used),
            },
            "resets": {
                "day": day_start(now) + 86400,
                "month": _next_month(now),
            },
        }

    # ------------------------------------------------------------ main API
    def acquire(self, account: Account | None, key: ApiKey | None = None) -> Lease:
        """Check every limit and reserve a concurrency slot. Raises :class:`QuotaDenied`.

        ``key`` is the API key used (optional); a GUEST key may come with
        ``account=None`` and is then judged by its own tables only."""
        adm = None
        if self.tables is not None:
            adm = self.tables.check(account, key)
            if not adm.allowed:
                raise QuotaDenied(adm.code, adm.message, retry_after=adm.retry_after,
                                  status=adm.status)
        if account is None:
            if adm is None:
                raise QuotaDenied("invalid_api_key", "No account for this key.", status=401)
            return self._guest_lease(adm)
        if account.disabled:
            raise QuotaDenied("account_disabled", "This account is disabled.")
        q = account.quota
        u = account.username
        now = self.clock()

        # DB-backed limits first (no lock needed; soft enforcement).
        remaining_tokens: list[int] = []
        if q.tokens_per_day:
            used = self.store.tokens_since(u, day_start(now))
            if used >= q.tokens_per_day:
                raise QuotaDenied(
                    "insufficient_quota",
                    f"Daily token quota used up ({used}/{q.tokens_per_day}). Resets at local midnight.",
                    retry_after=max(1, int(day_start(now) + 86400 - now)),
                )
            remaining_tokens.append(q.tokens_per_day - used)
        if q.tokens_per_month:
            used = self.store.tokens_since(u, month_start(now))
            if used >= q.tokens_per_month:
                raise QuotaDenied(
                    "insufficient_quota",
                    f"Monthly token quota used up ({used}/{q.tokens_per_month}).",
                    retry_after=max(1, int(_next_month(now) - now)),
                )
            remaining_tokens.append(q.tokens_per_month - used)
        if q.requests_per_day:
            n = self.store.requests_since(u, day_start(now))
            if n >= q.requests_per_day:
                raise QuotaDenied(
                    "insufficient_quota",
                    f"Daily request quota used up ({n}/{q.requests_per_day}).",
                    retry_after=max(1, int(day_start(now) + 86400 - now)),
                )

        with self._lock:
            window = self._rpm_window(u, now)
            if q.requests_per_minute and len(window) >= q.requests_per_minute:
                retry = max(1, int(window[0] + 60 - now) + 1)
                raise QuotaDenied(
                    "rate_limit_exceeded",
                    f"Too many requests: limit is {q.requests_per_minute} per minute.",
                    retry_after=retry,
                )
            if q.max_concurrent and self._inflight[u] >= q.max_concurrent:
                raise QuotaDenied(
                    "rate_limit_exceeded",
                    f"Too many requests at the same time: limit is {q.max_concurrent}.",
                    retry_after=2,
                )
            window.append(now)
            self._inflight[u] += 1

        cap = None
        if q.max_tokens_per_request:
            remaining_tokens.append(q.max_tokens_per_request)
        if remaining_tokens:
            cap = max(1, min(remaining_tokens))
        lease = Lease(username=u, max_tokens=cap, _mgr=self)
        if adm is not None:
            lease.tier, lease.queue_tier = adm.tier, adm.queue_tier
            lease.state, lease.subject = adm.state, adm.subject
        return lease

    def _guest_lease(self, adm) -> Lease:
        with self._lock:
            if self._inflight[adm.subject] >= adm.max_concurrent:
                raise QuotaDenied(
                    "rate_limit_exceeded",
                    f"Too many requests at the same time: limit is {adm.max_concurrent}.",
                    retry_after=2,
                )
            self._inflight[adm.subject] += 1
        return Lease(username="", max_tokens=None, _mgr=self, tier=adm.tier,
                     queue_tier=adm.queue_tier, state=adm.state, subject=adm.subject,
                     _inflight_key=adm.subject)

    def settle(self, tokens: int, account: Account | None, key: ApiKey | None = None) -> dict | None:
        """Charge a finished request to the usage tables (no-op without tables)."""
        if self.tables is None:
            return None
        return self.tables.charge(tokens, account, key)

    def _release(self, username: str) -> None:
        with self._lock:
            if self._inflight.get(username, 0) > 0:
                self._inflight[username] -= 1

    def headers(self, account: Account) -> dict[str, str]:
        """OpenAI-style ``x-ratelimit-*`` headers (only for limits that are set)."""
        info = self.usage(account)
        lim, rem = info["limits"], info["remaining"]
        h: dict[str, str] = {}
        if lim["requests_per_minute"]:
            h["x-ratelimit-limit-requests"] = str(lim["requests_per_minute"])
            h["x-ratelimit-remaining-requests"] = str(rem["requests_minute"])
            h["x-ratelimit-reset-requests"] = "60s"
        if lim["tokens_per_day"]:
            h["x-ratelimit-limit-tokens"] = str(lim["tokens_per_day"])
            h["x-ratelimit-remaining-tokens"] = str(rem["tokens_today"])
            h["x-ratelimit-reset-tokens"] = f"{max(0, int(info['resets']['day'] - self.clock()))}s"
        return h


def _next_month(now: float) -> float:
    dt = datetime.fromtimestamp(now).astimezone()
    if dt.month == 12:
        nxt = dt.replace(year=dt.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = dt.replace(month=dt.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()
