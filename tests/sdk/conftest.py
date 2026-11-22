"""SDK 測試的 fixture。

整套流程的測試沿用 tests/e2e 的 make_stack：真的節點（三個入口都只綁 127.0.0.1）＋假後端＋真的 worker＋
錄音轉送器（扮演 API 通道）。測完 make_stack 會把所有伺服器、worker 關掉。不開對外埠、不連真的 LLM。

同步的 SDK 在測試的事件迴圈裡會卡住伺服器，所以一律用 asyncio.to_thread 呼叫。
"""

from __future__ import annotations

import pytest

from e2e.conftest import fast_polling, llmop_home, make_stack  # noqa: F401  （重用 e2e 的 fixture）
from llmop.sdk import client as sdk_client


@pytest.fixture(autouse=True)
def fast_sdk_polling(monkeypatch):
    monkeypatch.setattr(sdk_client, "POLL_MIN_INTERVAL", 0.01)
    monkeypatch.setattr(sdk_client, "POLL_BACKOFF", (0.01,))


@pytest.fixture
def sdk_home(tmp_path, monkeypatch):
    """單元測試用的 LLMOP_HOME。"""
    home = tmp_path / "home"
    monkeypatch.setenv("LLMOP_HOME", str(home))
    return home
