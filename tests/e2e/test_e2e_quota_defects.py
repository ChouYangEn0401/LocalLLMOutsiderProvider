"""整合時發現、現在已經修好的兩個缺陷：每日 token 配額的准入檢查曾經可以被繞過。

docs/PROTOCOL.md 2.3 說 worker 一律用標頭裡的 max_tokens，「這樣閘道看到的配額數字才是真的」。
原本的漏洞：
1. OpenAI 相容後端把用戶端的 n（與舊參數 best_of）原樣轉給 vLLM／LM Studio，一筆請求可以要求
   n 倍的 max_tokens，而閘道只依 max_tokens 算配額。修法：src/llmop/worker/handler.py 在呼叫
   後端之前，先擋下 n 或 best_of 不是 1（或未給）的請求，回封裝的 bad_request。
2. 配額的准入檢查只看「今天已完成的用量」，進行中、還沒結算的請求不算在內，所以同時送出好幾筆
   就能讓合計遠超過每日上限。修法：src/llmop/gateway/store.py 的 admit 在放行的同一個交易裡
   把這筆的 max_tokens 記成「保留中」；hub.Hub.finish（工作結束時唯一會走到的地方，不管成功、
   失敗、逾時還是斷線）一定呼叫 store.release_reservation 把保留還回去。配額檢查因此變成
   「已完成用量 + 保留中 + 這筆的 max_tokens ≤ 每日上限」。

下面兩個測試驗證修好之後的行為，取代原本標成 xfail、展示缺陷的版本。
"""

from __future__ import annotations

import asyncio

from .harness import MODELS, wait_until


async def test_one_request_cannot_ask_the_backend_for_several_completions(make_stack):
    """n=8 的請求在到達後端之前就被 worker 擋下，後端完全沒被呼叫到，閘道的配額數字不會失真。

    worker 把這個拒絕封裝回來（它是從 worker 那端回的 sealed bad_request，不是用戶端本機就
    擋下的請求），本機代理照 sdk.ClientError 的規則把「sealed 的後端錯誤」一律對映成 502。
    """
    stack = await make_stack(token_limit=100)
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


async def test_concurrent_requests_cannot_exceed_the_daily_token_limit(make_stack):
    """每日 token 上限 100：第一筆 max_tokens=60 還在跑的時候，第二筆 max_tokens=60 要被擋
    （60 + 60 > 100），因為第一筆的 60 已經先被「保留」，不用等它真的結算完成。
    """
    stack = await make_stack(token_limit=100)
    backend = stack.backends["ollama"]
    backend.delay = 10
    first = asyncio.create_task(stack.chat(MODELS["ollama"], max_tokens=60))
    await wait_until(lambda: backend.calls, what="第一筆請求到達後端")
    try:
        status, data, _ = await stack.chat(MODELS["ollama"], max_tokens=60)
        assert status == 429, f"第二筆被放行了（HTTP {status}）：兩筆共 120 個 token 的額度，上限只有 100"
        assert data["error"]["code"] == "quota_exceeded"
    finally:
        backend.release.set()
        status, data, _ = await first
        assert status == 200, f"第一筆不該受影響：HTTP {status}：{data}"

    # 第一筆結算完成之後，保留已經換成真正的用量；上限還有空間給下一筆合理的請求。
    status, data, _ = await stack.chat(MODELS["ollama"], max_tokens=30)
    assert status == 200, f"結算之後的新請求應該放行，卻是 HTTP {status}：{data}"


async def _second_request(stack, backend):
    """送出第二筆；它若被放行就會卡在後端，所以一到達後端就放行，讓測試能拿到結果並失敗。"""
    task = asyncio.create_task(stack.chat(MODELS["ollama"], max_tokens=60))
    while not task.done():
        if len(backend.calls) >= 2:
            backend.release.set()
        await asyncio.sleep(0.01)
    return await task
