"""T-R: problems found in review (R-03, R-04), fixed in T-F.

These were ``xfail(strict=True)`` until the fix; now they are normal regression
tests. See docs/security/REVIEW_v0.2.md for the issue numbers.
"""

from __future__ import annotations

import asyncio

import pytest

from llmop.app import make_scheduler
from llmop.config import parse_config
from llmop.quota import QuotaDenied, QuotaManager
from llmop.store import Store
from llmop.usage import UsageManager

from .conftest import FAST_HASH


async def test_account_max_concurrent_override_reaches_the_scheduler():
    cfg = parse_config({"accounts": {"bob": {"password_hash": FAST_HASH, "tier": "normal",
                                             "max_concurrent": 2}}})
    assert cfg.accounts["bob"].limits.max_concurrent == 2  # parsed and stored ...
    sched = make_scheduler(cfg)
    async with sched.slot("bob", "normal", "local"):
        second = asyncio.ensure_future(sched.slot("bob", "normal", "local").__aenter__())
        await asyncio.sleep(0.05)
        try:
            assert sched.running == 2  # ... but the scheduler still uses tiers.normal = 1
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


def test_vip3_gets_its_three_concurrent_requests():
    cfg = parse_config({"accounts": {"owner": {"password_hash": FAST_HASH, "tier": "vip3"}}})
    store = Store(":memory:")
    quota = QuotaManager(store, tables=UsageManager(store, cfg))
    acc = cfg.accounts["owner"]
    leases = [quota.acquire(acc), quota.acquire(acc)]
    try:
        leases.append(quota.acquire(acc))  # TIERS 2.3: VIP III max_concurrent = 3
    except QuotaDenied as exc:
        pytest.fail(f"third request refused: {exc.message}")
    finally:
        for lease in leases:
            lease.release()
