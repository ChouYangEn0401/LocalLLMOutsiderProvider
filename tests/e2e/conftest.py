"""端對端測試的 fixture：照 docs/DEPLOY.md 的步驟，把整套系統在 loopback 上搭起來。

順序與真實部署相同：
1. `llmop gateway init` 設定管理員密碼，啟動真的閘道。
2. 在閘道前面放錄音轉送器，扮演 Cloudflare 隧道（用戶端與 worker 都只連它）。
3. 管理員登入管理介面 API。
4. 啟動假的 Ollama 與假的 OpenAI 相容後端。
5. `llmop worker init` 建立金鑰與設定檔；從主控台輸出取得要釘選的公鑰，修改設定檔的 gateway_url 與 backends。
6. `llmop client keygen` 建立簽章金鑰；管理員以這把公鑰建立 API 金鑰；寫好用戶端 config.json。
7. 管理員建立 worker、取得註冊權杖；worker 以 --reg-token 第一次啟動並註冊。
8. 啟動本機代理。
"""

from __future__ import annotations

import contextlib
import getpass
import re

import aiohttp
import pytest

from llmop import paths
from llmop.client import cli as client_cli
from llmop.client import sdk
from llmop.client.config import ClientConfig, load_config, load_signing_key
from llmop.gateway import cli as gateway_cli
from llmop.gateway.app import build_app
from llmop.gateway.config import GatewayConfig
from llmop.gateway.store import Store
from llmop.worker import cli as worker_cli

from .harness import (
    ADMIN_PASSWORD,
    AdminClient,
    ClientIdentity,
    FakeBackend,
    RecordingRelay,
    Stack,
    WorkerRunner,
    parse_key_blocks,
    proxy_app,
)


@pytest.fixture
def llmop_home(tmp_path, monkeypatch):
    """每個測試都用全新的 LLMOP_HOME；閘道、worker、用戶端各自在底下的子目錄。"""
    home = tmp_path / "llmop-home"
    monkeypatch.setenv("LLMOP_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """用戶端輪詢的最短間隔與退避調短，讓 202 輪詢的測試不必真的等。"""
    monkeypatch.setattr(sdk, "POLL_MIN_INTERVAL", 0.01)
    monkeypatch.setattr(sdk, "POLL_BACKOFF", (0.01,))


@pytest.fixture
async def make_stack(llmop_home, aiohttp_server, capsys, monkeypatch):
    cleanups = contextlib.AsyncExitStack()

    async def factory(*, request_limit: int | None = None, token_limit: int | None = None,
                      **gateway_overrides) -> Stack:
        # 1. llmop gateway init，然後啟動閘道（同一個 Store 檔案）。
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": ADMIN_PASSWORD)
        assert gateway_cli.main(["init"]) == 0
        store = Store.open_default()
        app = build_app(GatewayConfig(**gateway_overrides), store)

        async def close_store(app) -> None:
            store.close()

        app.on_cleanup.append(close_store)
        gateway = await aiohttp_server(app)
        gateway_url = f"http://127.0.0.1:{gateway.port}"

        # 2. 隧道的位置。
        relay = RecordingRelay(gateway.port)
        await relay.start()
        cleanups.push_async_callback(relay.close)

        # 3. 管理員。
        admin = AdminClient(gateway_url)
        cleanups.push_async_callback(admin.close)
        await admin.login()

        # 4. 假後端（只綁 127.0.0.1）。
        backends = {}
        for kind in ("ollama", "openai"):
            fake = FakeBackend(kind)
            server = await aiohttp_server(fake.app())
            fake.base_url = f"http://127.0.0.1:{server.port}"
            backends[kind] = fake
            cleanups.callback(fake.release.set)

        # 5. llmop worker init；釘選值取自 worker 主控台的輸出。
        capsys.readouterr()
        assert worker_cli.main(["init"]) == 0
        worker_keys = parse_key_blocks(capsys.readouterr().out)
        assert len(worker_keys) == 1
        worker_home = paths.home("worker")
        worker_config = paths.read_json(worker_home / "config.json")
        worker_config["gateway_url"] = f"ws://127.0.0.1:{relay.port}/worker/ws"
        worker_config["backends"] = [{"kind": kind, "base_url": fake.base_url} for kind, fake in backends.items()]
        paths.write_json(worker_home / "config.json", worker_config, secret=True)

        # 6. llmop client keygen → 管理員建立 API 金鑰並登記簽章公鑰 → 用戶端 config.json。
        assert client_cli.main(["keygen"]) == 0
        console = capsys.readouterr().out
        public_key = re.search(r"public_key: (\S+)", console).group(1)
        sig_key_id = re.search(r"sig_key_id: (\S+)", console).group(1)
        key, api_key = await admin.create_key("e2e 用戶端", public_key, request_limit=request_limit,
                                              token_limit=token_limit)
        assert key["sig_key_id"] == sig_key_id  # 管理介面顯示的 sig_key_id 與用戶端主控台一致
        client = ClientIdentity(load_signing_key(), public_key, sig_key_id, api_key, key["id"])
        paths.write_json(paths.home("client") / "config.json", {
            "gateway_url": relay.url,
            "api_key": api_key,
            "pinned_workers": [worker_keys[0]["public_key"]],
        }, secret=True)
        config = load_config()

        # 7. 建立 worker、取得註冊權杖，worker 第一次啟動時註冊。
        worker_record, reg_token = await admin.create_worker("e2e-worker")
        worker = WorkerRunner(worker_home)
        cleanups.push_async_callback(worker.stop)
        worker.start(reg_token)

        # 8. 本機代理。
        async def start_proxy(pinned: list[str] | None = None, keepalive: float = 0.1) -> str:
            cfg = config if pinned is None else ClientConfig(
                gateway_url=config.gateway_url, api_key=config.api_key, pinned_workers=tuple(pinned))
            server = await aiohttp_server(proxy_app(cfg, client.signing_key, keepalive=keepalive))
            return f"http://127.0.0.1:{server.port}"

        http = aiohttp.ClientSession()
        cleanups.push_async_callback(http.close)
        stack = Stack(
            home=llmop_home,
            gateway_url=gateway_url,
            relay=relay,
            admin=admin,
            backends=backends,
            worker=worker,
            worker_id=worker_record["id"],
            reg_token=reg_token,
            worker_keys=worker_keys,
            client=client,
            proxy_url=await start_proxy(),
            http=http,
            start_proxy=start_proxy,
        )
        await stack.wait_online()
        await stack.wait_announced({worker_keys[0]["key_id"]})
        return stack

    yield factory
    await cleanups.aclose()
