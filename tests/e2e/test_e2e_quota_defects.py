"""配額的兩個性質（取代 B 原本「保留中的 max_tokens 也要算進每日上限」的測試）。

v0.2 的決策 D5 是**事後記帳**：准入時只看「已經結算的用量」，進行中的請求不預扣。所以同時送出
好幾筆時，合計可以超過上限一點；這裡驗證超出的幅度有上界：

    結束後的總用量 ≤ (上限 − 1) + 同時進行的上限 × 單筆用量

同時進行的上限是帳號的 ``quota.max_concurrent``（超過的請求立刻被拒絕，不會排隊後再放行）。
超過上限之後，下一筆一定被擋。

另一個性質沿用 B：一筆請求不能要求後端回好幾份答案（n／best_of），否則單筆用量沒有上界；
worker 在呼叫後端之前就擋下（src/llmop/worker/handler.py）。
"""

from __future__ import annotations

import asyncio

import pytest

from .harness import FAKE_USAGE, MODELS, wait_until

LIMIT = 20  # 每日 token 上限：比一筆（16）多、比兩筆（32）少
MAX_CONCURRENT = 2
PER_REQUEST = sum(FAKE_USAGE["ollama"])  # 假後端每筆回報 11 + 5 個 token
SENT = 6


async def test_one_request_cannot_ask_the_backend_for_several_completions(make_stack):
    """n=8 的請求在到達後端之前就被 worker 擋下，後端完全沒被呼叫到，用量數字不會失真。

    worker 把這個拒絕封裝回來（它是從 worker 那端回的 sealed bad_request，不是用戶端本機就
    擋下的請求），本機代理照 sdk.ClientError 的規則把「sealed 的後端錯誤」一律對映成 502。
    """
    stack = await make_stack()
    status, data, _ = await stack.chat(MODELS["openai"], max_tokens=60, n=8)
    assert status == 502, f"應該被擋下卻放行了（HTTP {status}）：{data}"
    assert data["error"]["code"] == "bad_request"
    assert stack.backends["openai"].calls == [], "後端不該被呼叫到：n 不是 1 的請求要在 worker 這端就擋下"

    # best_of 是舊版參數，但一樣可以讓後端多算；也要擋。n／best_of 給 1 或完全不給都要正常放行。
    status, data, _ = await stack.chat(MODELS["openai"], max_tokens=60, best_of=4)
    assert status == 502 and data["error"]["code"] == "bad_request"
    status, data, _ = await stack.chat(MODELS["openai"], max_tokens=60, n=1)
    assert status == 200, f"n=1 應該正常放行，卻是 HTTP {status}：{data}"
    (call,) = stack.backends["openai"].calls
    assert call["max_tokens"] == 60


@pytest.mark.parametrize("path", ["encrypted", "plaintext"], ids=["加密面e2e", "明文v1"])
async def test_concurrent_overshoot_of_the_daily_token_limit_is_bounded(make_stack, path):
    # 帳號的同時上限（quota.max_concurrent）與排程器的每帳號同時上限（NORMAL 等級的 max_concurrent）
    # 都設成 2，兩筆真的同時在後端跑。
    stack = await make_stack(alice={"quota": {"tokens_per_day": LIMIT, "max_concurrent": MAX_CONCURRENT}},
                             tiers={"normal": {"max_concurrent": MAX_CONCURRENT}})
    backend = stack.backends["ollama"]
    backend.delay = 30  # 前幾筆卡在後端，直到測試放行

    if path == "encrypted":
        async def one() -> tuple[int, dict]:
            status, data, _ = await stack.chat(MODELS["ollama"], max_tokens=60)
            return status, data
    else:
        _, plain_key = await stack.portal.create_key("明文 key")

        async def one() -> tuple[int, dict]:
            return await stack.plain("POST", "/v1/chat/completions", api_key=plain_key, json_body={
                "model": "anything", "max_tokens": 60, "messages": [{"role": "user", "content": "嗨"}]})

    tasks = [asyncio.create_task(one()) for _ in range(SENT)]
    try:
        # 同時進行的上限之內的請求到達後端；其餘的立刻被拒絕（不必等前面的結束）。
        await wait_until(lambda: len(backend.calls) == MAX_CONCURRENT
                         and sum(t.done() for t in tasks) == SENT - MAX_CONCURRENT,
                         what=" 前兩筆到達後端、其餘被拒絕")
        refused = [t.result() for t in tasks if t.done()]
        for status, data in refused:
            assert status == 429, data
            assert data["error"]["code"] in ("quota_exceeded", "rate_limit_exceeded"), data
    finally:
        backend.release.set()
    results = await asyncio.gather(*tasks)
    assert sorted(status for status, _ in results) == [200] * MAX_CONCURRENT + [429] * (SENT - MAX_CONCURRENT)

    # 事後記帳：兩筆都放行了，合計超過上限，但超出的幅度不超過上界。
    used = stack.state.store.tokens_since("alice", 0)
    assert used == MAX_CONCURRENT * PER_REQUEST
    assert used <= (LIMIT - 1) + MAX_CONCURRENT * PER_REQUEST
    assert len(backend.calls) == MAX_CONCURRENT

    # 已經超過上限：下一筆一定被擋，後端不會再被呼叫。
    backend.delay = 0
    status, data = await one()
    assert status == 429, data
    assert data["error"]["code"] in ("quota_exceeded", "insufficient_quota"), data
    assert len(backend.calls) == MAX_CONCURRENT
