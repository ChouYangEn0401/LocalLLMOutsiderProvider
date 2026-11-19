"""worker 測試的 fixture：暫存的 LLMOP_HOME、金鑰、假伺服器，以及在背景執行的 worker。"""

from __future__ import annotations

import asyncio
import time

import pytest
from worker_support import FakeGateway, FakeLLM

from llmop import paths
from llmop.crypto import generate_signing_key
from llmop.worker.config import parse_config
from llmop.worker.keystore import init_keys
from llmop.worker.link import serve


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMOP_HOME", str(tmp_path / "llmop"))
    return paths.home("worker")


@pytest.fixture
def keystore(home):
    store, _ = init_keys(home / "keys.json")
    return store


@pytest.fixture
def signing_key():
    return generate_signing_key()


async def _start(aiohttp_server, fake):
    server = await aiohttp_server(fake.app())
    assert server.host == "127.0.0.1"
    return server


@pytest.fixture
async def ollama(aiohttp_server):
    fake = FakeLLM("ollama")
    server = await _start(aiohttp_server, fake)
    fake.base_url = f"http://127.0.0.1:{server.port}"
    return fake


@pytest.fixture
async def openai(aiohttp_server):
    fake = FakeLLM("openai")
    server = await _start(aiohttp_server, fake)
    fake.base_url = f"http://127.0.0.1:{server.port}"
    return fake


@pytest.fixture
async def gateway(aiohttp_server):
    fake = FakeGateway()
    server = await _start(aiohttp_server, fake)
    fake.url = f"ws://127.0.0.1:{server.port}/worker/ws"
    return fake


@pytest.fixture
async def start_worker(home, keystore, gateway):
    """回傳 start(backends, **設定)：以假閘道與指定後端啟動 worker，測試結束時停止。"""
    tasks: list[asyncio.Task] = []

    def start(*backends, reg_token="llmop_reg_test", store=None, clock=None, link_options=None, **settings):
        config = parse_config(
            {
                "gateway_url": gateway.url,
                "backends": [
                    {"kind": b.kind, "base_url": b.base_url} if isinstance(b, FakeLLM) else b for b in backends
                ],
                **settings,
            }
        )
        options = {"min_backoff": 0.01, "max_backoff": 0.05, "refresh_seconds": 3600}
        options.update(link_options or {})
        # 開機門檻會拒絕早於啟動時刻的 ts（整數秒）；讓 worker 的時鐘「早兩秒啟動」，
        # 測試裡剛封裝的請求才不會因為和啟動落在同一秒而被拒。
        options["clock"] = clock or (lambda: time.time() - 2)
        task = asyncio.create_task(serve(config, store or keystore, home, reg_token, **options))
        tasks.append(task)
        return task

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
