"""Usage tables per TIERS_AND_USAGE_RULES.md (sections 2, 3, 4, 8, 11).

Every subject (an account -- all its keys together -- or one GUEST key) has:

* a **1.5-hour bucket**: capacity ``C``, refills at ``R = C / window_seconds`` whether
  used or not, charged *after* each request, may go negative down to
  ``-debt_floor * C``;
* a **weekly table**: limit ``W``, reset at a fixed weekday/time (default Sunday 04:00
  local). GUEST keys have a total ``budget`` instead, never reset unless ``reset``.

State machine for NORMAL accounts (VIP I uses the same flag but only drops to the
NORMAL queue position; GUEST is answered 429 until the bucket refills)::

    NORMAL --(bucket <= 0 after a charge)--> LOW --(bucket >= recover_at * C)--> NORMAL

Bucket level, update time and the weekly counter are stored in the ``buckets`` table
so a restart refills by the elapsed time. Times going backwards refill nothing and
never re-run a weekly reset (the stored ``period_start`` wins).

This module never raises for a denial; :meth:`UsageManager.check` returns an
:class:`Admission` and :mod:`llmop.quota` turns it into ``QuotaDenied``.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .store import ApiKey, Store

TIER_NAMES = ("vip3", "vip2", "vip1", "normal", "guest")
LOW_PRIORITY = 6
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# TIERS section 9 defaults. A tier without ``bucket`` / ``weekly`` is unlimited.
DEFAULT_TIERS: dict[str, dict] = {
    "vip3": {"priority": 1, "max_concurrent": 3},
    "vip2": {"priority": 2, "max_concurrent": 2},
    "vip1": {"priority": 3, "max_concurrent": 2, "bucket": 240_000, "weekly": 3_000_000,
             "on_bucket_empty": "as_normal"},
    "normal": {"priority": 4, "max_concurrent": 1, "bucket": 80_000, "weekly": 1_000_000},
    "guest": {"priority": 5, "max_concurrent": 1, "bucket": 20_000, "budget": 50_000,
              "reset": False},
}
_TIER_KEYS = ("priority", "max_concurrent", "bucket", "weekly", "budget", "reset",
              "on_bucket_empty")


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dict or an object (config sections may be either)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _has(obj: Any, name: str) -> bool:
    if isinstance(obj, dict):
        return name in obj
    return hasattr(obj, name)


@dataclass(frozen=True)
class TierLimits:
    name: str
    priority: int
    max_concurrent: int
    bucket: int | None = None  # 1.5-hour capacity C (None = not limited)
    weekly: int | None = None  # weekly W (None = not limited)
    budget: int | None = None  # GUEST total budget
    reset: bool = False  # GUEST: budget resets with the weekly reset
    on_bucket_empty: str = "low"  # low | as_normal (VIP I)


@dataclass(frozen=True)
class UsageSettings:
    window_seconds: float = 5400.0
    recover_at: float = 0.25
    debt_floor: float = 0.25
    low_mode: str = "wait"  # wait | block
    weekday: int = 6  # 0 = Monday ... 6 = Sunday
    hour: int = 4
    minute: int = 0
    timezone: str = "local"


@dataclass
class Admission:
    allowed: bool
    tier: str  # configured tier (vip3 ... guest)
    queue_tier: str  # what the scheduler should use: vip3|vip2|vip1|normal|guest|low
    state: str  # NORMAL | LOW
    max_concurrent: int
    subject: str
    code: str = ""
    message: str = ""
    retry_after: int | None = None
    status: int = 200  # HTTP status for a denial (401 dead GUEST key, else 429)


def parse_settings(raw: Any) -> UsageSettings:
    """``config.usage`` (dict or object, any part may be missing) -> settings."""
    d = UsageSettings()
    if raw is None:
        return d
    wr = _get(raw, "week_reset")
    weekday = _get(wr, "weekday", d.weekday)
    if isinstance(weekday, str):
        weekday = WEEKDAYS.get(weekday.strip().lower()[:3], d.weekday)
    hour, minute = d.hour, d.minute
    t = _get(wr, "time")
    if isinstance(t, str) and ":" in t:
        h, m = t.split(":", 1)
        hour, minute = int(h), int(m)
    elif _get(wr, "hour") is not None:
        hour, minute = int(_get(wr, "hour")), int(_get(wr, "minute", 0) or 0)
    return UsageSettings(
        window_seconds=float(_get(raw, "window_seconds", d.window_seconds) or d.window_seconds),
        recover_at=float(_get(raw, "recover_at", d.recover_at)),
        debt_floor=float(_get(raw, "debt_floor", d.debt_floor)),
        low_mode=str(_get(raw, "low_mode", d.low_mode) or d.low_mode),
        weekday=int(weekday),
        hour=hour,
        minute=minute,
        timezone=str(_get(wr, "timezone", d.timezone) or d.timezone),
    )


def resolve_tier(name: str, tiers_cfg: Any = None, account: Any = None) -> TierLimits:
    """Limits of tier ``name``: the account's effective ``limits`` (config.py fills
    them in with per-account overrides applied), else the config ``tiers`` section
    for that tier, else the defaults; then any per-account attribute override
    (``account.bucket`` etc.). A ``0`` / missing bucket, weekly or budget = unlimited."""
    if name not in TIER_NAMES:
        name = "normal"
    base = DEFAULT_TIERS[name]
    section = _get(tiers_cfg, name)
    own = _get(account, "limits") if account is not None else None
    if own is not None:
        section = own
    vals: dict = {}
    for k in _TIER_KEYS:
        if section is not None and _has(section, k):
            vals[k] = _get(section, k)
        elif section is not None and k in ("bucket", "weekly", "budget"):
            vals[k] = None  # written tier without bucket / weekly = unlimited
        else:
            vals[k] = base.get(k)
    if account is not None:
        for k in _TIER_KEYS:
            v = getattr(account, k, None) if not isinstance(account, dict) else account.get(k)
            if v is not None and not callable(v):
                vals[k] = v
    return TierLimits(
        name=name,
        priority=int(vals["priority"] if vals["priority"] is not None else base["priority"]),
        max_concurrent=int(vals["max_concurrent"] or base["max_concurrent"]),
        bucket=int(vals["bucket"]) if vals["bucket"] else None,
        weekly=int(vals["weekly"]) if vals["weekly"] else None,
        budget=int(vals["budget"]) if vals["budget"] else None,
        reset=bool(vals["reset"]),
        on_bucket_empty=str(vals["on_bucket_empty"] or ("as_normal" if name == "vip1" else "low")),
    )


# ---------------------------------------------------------------------- week reset
def _zone(settings: UsageSettings):
    if settings.timezone in ("", "local"):
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.timezone)
    except Exception:  # unknown zone / no tz database (Windows without tzdata)
        return None


def _wall(now: float, tz) -> datetime:
    return datetime.fromtimestamp(now, tz).replace(tzinfo=None) if tz else datetime.fromtimestamp(now)


def _ts(wall: datetime, tz) -> float:
    return wall.replace(tzinfo=tz).timestamp() if tz else wall.timestamp()


def last_week_reset(now: float, settings: UsageSettings) -> float:
    """The most recent weekly reset moment at or before ``now``."""
    tz = _zone(settings)
    wall = _wall(now, tz)
    cand = (wall - timedelta(days=(wall.weekday() - settings.weekday) % 7)).replace(
        hour=settings.hour, minute=settings.minute, second=0, microsecond=0)
    if cand > wall:
        cand -= timedelta(days=7)
    return _ts(cand, tz)


def next_week_reset(now: float, settings: UsageSettings) -> float:
    tz = _zone(settings)
    last = _wall(last_week_reset(now, settings), tz)
    return _ts(last + timedelta(days=7), tz)


# ---------------------------------------------------------------------- manager
class UsageManager:
    """The two usage tables of every account / GUEST key, persisted in :class:`Store`.

    ``config`` may be a ``Config``, a ``ConfigHolder`` (``.get()``), a zero-argument
    callable returning one, or None (all defaults). Only ``config.usage`` and
    ``config.tiers`` are read, defensively.
    """

    def __init__(self, store: Store, config: Any = None, clock=time.time):
        self.store = store
        self._config = config
        self.clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------ config
    def _cfg(self) -> Any:
        c = self._config
        if c is None:
            return None
        if hasattr(c, "get") and not isinstance(c, dict) and not hasattr(c, "accounts"):
            return c.get()
        if callable(c) and not hasattr(c, "accounts"):
            return c()
        return c

    def settings(self) -> UsageSettings:
        return parse_settings(_get(self._cfg(), "usage"))

    def limits_for(self, account: Any = None, key: ApiKey | None = None) -> TierLimits:
        tiers = _get(self._cfg(), "tiers")
        if key is not None and key.is_guest:
            return resolve_tier("guest", tiers)
        return resolve_tier(getattr(account, "tier", None) or "normal", tiers, account)

    @staticmethod
    def subject_for(account: Any = None, key: ApiKey | None = None) -> str:
        if key is not None and key.is_guest:
            return f"key:{key.id}"
        if account is None:
            raise ValueError("an account or a GUEST key is required")
        return f"acct:{account.username}"

    # ------------------------------------------------------------ bucket math
    @staticmethod
    def _terms(lim: TierLimits, key: ApiKey | None) -> tuple[int | None, bool]:
        """GUEST ``(budget, reset)``: the key's own values win over the tier's."""
        if lim.name != "guest":
            return None, True
        budget = (key.budget_tokens if key is not None and key.budget_tokens else None) \
            or lim.budget
        reset = bool(key.reset) if key is not None else lim.reset
        return budget, reset

    def _load(self, subject: str, lim: TierLimits, st: UsageSettings, now: float,
              key: ApiKey | None = None) -> dict:
        """The stored row brought up to ``now`` (refill, rescale, weekly reset).

        Pure: nothing is written. Refill is monotone between charges, so evaluating
        the LOW -> NORMAL threshold lazily at read time is exact."""
        cap = float(lim.bucket or 0)
        budget, reset = self._terms(lim, key)
        periodic = not (budget and not reset)  # a non-resetting GUEST budget never resets
        row = self.store.get_bucket(subject)
        if row is None:
            start = last_week_reset(now, st) if periodic else (key.created_at if key else now)
            return {"level": cap, "updated_at": now, "capacity": cap, "low": 0,
                    "period_used": 0, "period_start": start, "exhausted_at": None}
        row = dict(row)
        if cap:
            old_cap = float(row["capacity"] or 0)
            if old_cap > 0 and old_cap != cap:  # tier changed: scale proportionally
                row["level"] = row["level"] * cap / old_cap
            elif old_cap <= 0:
                row["level"] = cap
            rate = cap / st.window_seconds
            dt = max(0.0, now - row["updated_at"])  # clock moved back -> no refill
            row["level"] = min(cap, row["level"] + rate * dt)
            if row["low"] and row["level"] >= st.recover_at * cap:
                row["low"] = 0
        else:
            row["level"] = 0.0
            row["low"] = 0
        row["capacity"] = cap
        row["updated_at"] = max(row["updated_at"], now)
        if periodic:
            boundary = last_week_reset(now, st)
            if boundary > (row["period_start"] or 0):  # never re-run a reset backwards
                row["period_used"] = 0
                row["period_start"] = boundary
                row["exhausted_at"] = None
        return row

    def _evaluate(self, row: dict, lim: TierLimits, st: UsageSettings, now: float,
                  subject: str, key: ApiKey | None) -> Admission:
        def adm(**kw) -> Admission:
            base = dict(allowed=True, tier=lim.name, queue_tier=lim.name, state="NORMAL",
                        max_concurrent=lim.max_concurrent, subject=subject)
            base.update(kw)
            return Admission(**base)

        cap = float(lim.bucket or 0)
        rate = cap / st.window_seconds if cap else 0.0

        if lim.name == "guest":
            if key is not None and key.revoked_at is not None:
                return adm(allowed=False, code="key_revoked", status=401,
                           message="此 key 已被撤銷 (key revoked)")
            if key is not None and key.expires_at is not None and key.expires_at <= now:
                return adm(allowed=False, code="key_expired", status=401,
                           message="此 key 已到期 (key expired)")
            budget, reset = self._terms(lim, key)
            if budget and row["period_used"] >= budget:
                if not reset:
                    return adm(allowed=False, code="key_exhausted", status=401,
                               message="此 key 的總額已用完 (key budget exhausted)")
                wait = max(1, math.ceil(next_week_reset(now, st) - now))
                return adm(allowed=False, code="insufficient_quota", status=429,
                           retry_after=wait,
                           message=f"本週額度已用完，約 {wait} 秒後重置 "
                                   f"(weekly budget used up, resets in ~{wait}s)")
            if cap and row["level"] <= 0:
                wait = max(1, math.ceil(-row["level"] / rate) + 1)
                return adm(allowed=False, code="rate_limit_exceeded", status=429,
                           retry_after=wait,
                           message=f"1.5 小時用量已用完，約 {wait} 秒後再試 "
                                   f"(usage bucket empty, retry in ~{wait}s)")
            return adm()

        bucket_low = bool(cap and row["low"])
        weekly_out = bool(lim.weekly and row["period_used"] >= lim.weekly)
        if not bucket_low and not weekly_out:
            return adm()
        if bucket_low and not weekly_out and lim.on_bucket_empty == "as_normal":
            return adm(queue_tier="normal")  # VIP I: NORMAL position, never LOW
        # LOW treatment (bucket dry for NORMAL, or weekly table used up -- D25).
        waits = []
        if bucket_low:
            waits.append((st.recover_at * cap - row["level"]) / rate)
        if weekly_out:
            waits.append(next_week_reset(now, st) - now)
        wait = max(1, math.ceil(max(waits)))
        if st.low_mode == "block":
            return adm(allowed=False, state="LOW", queue_tier="low", code="rate_limit_exceeded",
                       status=429, retry_after=wait,
                       message=f"用量已達上限（LOW），約 {wait} 秒後恢復 "
                               f"(usage limit reached, back to normal in ~{wait}s)")
        return adm(state="LOW", queue_tier="low", retry_after=wait)

    # ------------------------------------------------------------ public API
    def check(self, account: Any = None, key: ApiKey | None = None) -> Admission:
        """May this caller start a request now, and at which queue position?"""
        now = self.clock()
        st = self.settings()
        lim = self.limits_for(account, key)
        subject = self.subject_for(account, key)
        with self._lock:
            row = self._load(subject, lim, st, now, key)
        return self._evaluate(row, lim, st, now, subject, key)

    def charge(self, tokens: int, account: Any = None, key: ApiKey | None = None) -> dict:
        """Settle one finished request (always after the fact, TIERS 2.1). Returns status."""
        now = self.clock()
        st = self.settings()
        lim = self.limits_for(account, key)
        subject = self.subject_for(account, key)
        tokens = max(0, int(tokens or 0))
        with self._lock:
            row = self._load(subject, lim, st, now, key)
            cap = float(lim.bucket or 0)
            if cap:
                row["level"] = max(-st.debt_floor * cap, row["level"] - tokens)
                if row["level"] <= 0:
                    row["low"] = 1
            row["period_used"] = int(row["period_used"]) + tokens
            budget, _ = self._terms(lim, key)
            if budget and row["period_used"] >= budget \
                    and not row["exhausted_at"]:
                row["exhausted_at"] = now
            self._save(subject, row)
        return self.status(account, key)

    def fill(self, account: Any = None, key: ApiKey | None = None) -> dict:
        """Admin (local only): refill the 1.5-hour bucket to full and leave LOW."""
        now = self.clock()
        st = self.settings()
        lim = self.limits_for(account, key)
        subject = self.subject_for(account, key)
        with self._lock:
            row = self._load(subject, lim, st, now, key)
            row["level"] = float(lim.bucket or 0)
            row["low"] = 0
            self._save(subject, row)
        return self.status(account, key)

    def _save(self, subject: str, row: dict) -> None:
        self.store.save_bucket(
            subject, level=float(row["level"]), updated_at=float(row["updated_at"]),
            capacity=float(row["capacity"]), low=int(row["low"]),
            period_used=int(row["period_used"]), period_start=float(row["period_start"] or 0),
            exhausted_at=row["exhausted_at"],
        )

    def status(self, account: Any = None, key: ApiKey | None = None) -> dict:
        """Both tables for display (/v1/me, portal, read-only dashboard).

        Works for dead GUEST keys too (revoked / expired / exhausted)."""
        now = self.clock()
        st = self.settings()
        lim = self.limits_for(account, key)
        subject = self.subject_for(account, key)
        with self._lock:
            row = self._load(subject, lim, st, now, key)
        adm = self._evaluate(row, lim, st, now, subject, key)
        cap = float(lim.bucket or 0)
        out: dict = {
            "subject": subject,
            "tier": lim.name,
            "state": adm.state,
            "queue_tier": adm.queue_tier,
            "priority": LOW_PRIORITY if adm.queue_tier == "low" else
            resolve_tier(adm.queue_tier, _get(self._cfg(), "tiers")).priority,
            "max_concurrent": lim.max_concurrent,
            "allowed": adm.allowed,
            "code": adm.code,
            "retry_after": adm.retry_after,
            "bucket": None,
            "weekly": None,
        }
        if cap:
            rate = cap / st.window_seconds
            level = row["level"]
            out["bucket"] = {
                "capacity": int(cap),
                "level": round(level, 1),
                "percent": round(max(0.0, level) / cap * 100, 1),
                "refill_per_second": round(rate, 3),
                "seconds_to_full": max(0, math.ceil((cap - level) / rate)),
                "seconds_to_recover": max(0, math.ceil((st.recover_at * cap - level) / rate))
                if row["low"] else 0,
                "low": bool(row["low"]),
            }
        if lim.name != "guest" and lim.weekly:
            out["weekly"] = {
                "limit": lim.weekly,
                "used": int(row["period_used"]),
                "remaining": max(0, lim.weekly - int(row["period_used"])),
                "resets_at": next_week_reset(now, st),
            }
        elif lim.name != "guest":
            out["weekly"] = {"limit": None, "used": int(row["period_used"]), "remaining": None,
                             "resets_at": next_week_reset(now, st)}
        if lim.name == "guest":
            budget, reset = self._terms(lim, key)
            if key is not None and key.revoked_at is not None:
                kstate, since = "revoked", key.revoked_at
            elif key is not None and key.expires_at is not None and key.expires_at <= now:
                kstate, since = "expired", key.expires_at
            elif budget and row["period_used"] >= budget:
                kstate = "exhausted" if not reset else "used_up_until_reset"
                since = row["exhausted_at"]
            else:
                kstate, since = "active", None
            out["guest"] = {
                "budget": budget,
                "used": int(row["period_used"]),
                "remaining": None if not budget else max(0, budget - int(row["period_used"])),
                "reset": reset,
                "resets_at": next_week_reset(now, st) if reset else None,
                "status": kstate,
                "since": since,
                "expires_at": key.expires_at if key is not None else None,
            }
        return out
