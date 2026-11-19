"""端對端測試的 fixture：照部署文件的步驟，把 v0.2 的整套系統在 loopback 上搭起來。

順序與真實部署相同：
1. 啟動假的 Ollama 與假的 OpenAI 相容後端（只綁 127.0.0.1）。
2. 寫好 config.yaml（帳號 root 是管理員、alice 是一般使用者；單一模型；三個入口的 port），
   以 ``llmop.node.Node`` 啟動整個節點（三個真的 uvicorn）。這與 ``llmop -c config.yaml serve
   --no-tunnel`` 是同一段程式（``test_e2e_cli.py`` 另外以子程序真的跑一次 CLI）。
3. 在 public-api 入口前面放錄音轉送器，扮演 Cloudflare API 隧道（用戶端與 worker 都只連它）。
4. 管理員登入本機控制台；alice 登入帳號網站。
5. `llmop worker init` 建立金鑰與設定檔；從主控台輸出取得要釘選的公鑰，修改設定檔的
   gateway_url（``ws://<隧道>/<gate>/worker/ws``）與 backends。
6. `llmop client keygen` 建立簽章金鑰；alice 在帳號網站以這把公鑰建立 ``encryption=required``
   的 API 金鑰；寫好用戶端 config.json（gateway_url 含 gate）。
7. 管理員在控制台建立 worker、取得註冊碼；worker 以註冊碼第一次啟動並註冊。
8. 啟動本機代理。
"""

from __future__ import annotations

import contextlib
import copy
import re

import aiohttp
import pytest
import yaml

from llmop import paths
from llmop.app import build_state
from llmop.client import cli as client_cli
from llmop.client import sdk
from llmop.client.config import ClientConfig, load_config, load_signing_key
from llmop.config import load_config as load_node_config
from llmop.node import Node
from llmop.worker import cli as worker_cli

from .harness import (
    ALICE_HASH,
    ALICE_PASSWORD,
    OLLAMA_MODEL,
    ROOT_HASH,
    ROOT_PASSWORD,
    ClientIdentity,
    FakeBackend,
    FakeCloudflared,
    FakeTunnel,
    RecordingRelay,
    Stack,
    WebClient,
    WorkerRunner,
    free_port,
    parse_key_blocks,
    proxy_app,
)


@pytest.fixture
def llmop_home(tmp_path, monkeypatch):
    """每個測試都用全新的 LLMOP_HOME；worker、用戶端各自在底下的子目錄。"""
    home = tmp_path / "llmop-home"
    monkeypatch.setenv("LLMOP_HOME", str(home))
    monkeypatch.delenv("LLMOP_DATA_DIR", raising=False)
    return home


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """用戶端輪詢的最短間隔與退避調短，讓 202 輪詢的測試不必真的等。"""
    monkeypatch.setattr(sdk, "POLL_MIN_INTERVAL", 0.01)
    monkeypatch.setattr(sdk, "POLL_BACKOFF", (0.01,))


def node_config(tmp_path, backends: dict[str, FakeBackend], *, alice: dict | None = None,
                **extra) -> dict:
    """config.yaml 的內容（dict）。``alice`` 覆寫 alice 帳號的設定，``extra`` 覆寫最上層。"""
    raw = {
        "server": {"data_dir": str(tmp_path / "node-data"), "access_log": False},
        "backends": [{"name": "ollama", "type": "ollama", "base_url": backends["ollama"].base_url,
                      "max_concurrency": 6}],
        "accounts": {
            "root": {"password_hash": ROOT_HASH, "role": "admin"},
            "alice": {"password_hash": ALICE_HASH, "max_api_keys": 5, **(alice or {})},
        },
        "model": OLLAMA_MODEL,
        "local_account": "root",
        "listeners": {"local": free_port(), "public_ui": free_port(), "public_api": free_port()},
        "queue": {"slots": 6},
    }
    raw.update(copy.deepcopy(extra))
    return raw


async def start_backends(aiohttp_server) -> dict[str, FakeBackend]:
    backends = {}
    for kind in ("ollama", "openai"):
        fake = FakeBackend(kind)
        server = await aiohttp_server(fake.app())
        fake.base_url = f"http://127.0.0.1:{server.port}"
        backends[kind] = fake
    return backends


@pytest.fixture
async def make_stack(llmop_home, tmp_path, aiohttp_server, capsys, monkeypatch):
    cleanups = contextlib.AsyncExitStack()

    async def factory(*, alice: dict | None = None, tunnels: bool = False,
                      sync_wait_seconds: float | None = None, **extra) -> Stack:
        # 1. 假後端。
        backends = await start_backends(aiohttp_server)
        for fake in backends.values():
            cleanups.callback(fake.release.set)

        # 2. config.yaml → 整個節點（三個入口）。
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(node_config(tmp_path, backends, alice=alice, **extra),
                                              allow_unicode=True), encoding="utf-8")
        cfg = load_node_config(config_path)
        state = build_state(cfg)
        node = Node(cfg, state=state, tunnels=tunnels, worker=False, out=lambda _t: None,
                    tunnel_factory=FakeTunnel)
        cleanups.push_async_callback(node.stop)
        await node.start()
        if sync_wait_seconds is not None:
            state.secure_router.secure.sync_wait_seconds = sync_wait_seconds

        # 3. API 隧道的位置。
        relay = RecordingRelay(cfg.listeners.public_api)
        await relay.start()
        cleanups.push_async_callback(relay.close)
        gate = state.api_link.gate

        # 4. 管理員登入本機控制台；alice 登入帳號網站。
        console = WebClient(f"http://127.0.0.1:{cfg.listeners.local}")
        cleanups.push_async_callback(console.close)
        await console.login("root", ROOT_PASSWORD)
        portal = WebClient(f"http://127.0.0.1:{cfg.listeners.public_ui}")
        cleanups.push_async_callback(portal.close)
        await portal.login("alice", ALICE_PASSWORD)

        # 5. llmop worker init；釘選值取自 worker 主控台的輸出。
        capsys.readouterr()
        assert worker_cli.main(["init"]) == 0
        worker_keys = parse_key_blocks(capsys.readouterr().out)
        assert len(worker_keys) == 1
        worker_home = paths.home("worker")
        worker_config = paths.read_json(worker_home / "config.json")
        worker_config["gateway_url"] = f"ws://127.0.0.1:{relay.port}/{gate}/worker/ws"
        worker_config["backends"] = [{"kind": kind, "base_url": fake.base_url} for kind, fake in backends.items()]
        paths.write_json(worker_home / "config.json", worker_config, secret=True)

        # 6. llmop client keygen → alice 在帳號網站建立加密專用的 API 金鑰 → 用戶端 config.json。
        assert client_cli.main(["keygen"]) == 0
        out = capsys.readouterr().out
        public_key = re.search(r"public_key: (\S+)", out).group(1)
        sig_key_id = re.search(r"sig_key_id: (\S+)", out).group(1)
        record, api_key = await portal.create_key("e2e 用戶端", sig_public_key=public_key,
                                                  encryption="required")
        assert record["sig_key_id"] == sig_key_id  # 帳號網站顯示的 sig_key_id 與用戶端主控台一致
        assert record["encryption"] == "required"
        assert api_key.startswith("sk-llop-")
        client = ClientIdentity(load_signing_key(), public_key, sig_key_id, api_key, record["id"])
        paths.write_json(paths.home("client") / "config.json", {
            "gateway_url": f"{relay.url}/{gate}",
            "api_key": api_key,
            "pinned_workers": [worker_keys[0]["public_key"]],
        }, secret=True)
        config = load_config()

        # 7. 管理員建立 worker、取得註冊碼，worker 第一次啟動時註冊。
        worker_record, reg_token = await console.create_worker("e2e-worker")
        worker = WorkerRunner(worker_home)
        cleanups.push_async_callback(worker.stop)
        worker.start(reg_token)

        # 8. 本機代理。
        async def start_proxy(pinned: list[str] | None = None, keepalive: float = 0.1,
                              gateway_url: str | None = None) -> str:
            cfg_ = ClientConfig(
                gateway_url=gateway_url or config.gateway_url, api_key=config.api_key,
                pinned_workers=tuple(pinned) if pinned is not None else config.pinned_workers)
            server = await aiohttp_server(proxy_app(cfg_, client.signing_key, keepalive=keepalive))
            return f"http://127.0.0.1:{server.port}"

        async def start_cloudflared(target_port: int, public_host: str) -> FakeCloudflared:
            fake = FakeCloudflared(target_port, public_host)
            server = await aiohttp_server(fake.app())
            fake.url = f"http://127.0.0.1:{server.port}"
            return fake

        async def restart_node() -> None:
            """閘道重新啟動：停掉整個節點，用同一份 config.yaml 與資料目錄再啟動一個（同樣的 port）。
            記憶體裡的狀態（待處理與結果表、worker 連線）都沒了；資料庫與 gate 檔還在。"""
            await stack.node.stop()
            cfg2 = load_node_config(config_path)
            node2 = Node(cfg2, state=build_state(cfg2), tunnels=tunnels, worker=False,
                         out=lambda _t: None, tunnel_factory=FakeTunnel)
            cleanups.push_async_callback(node2.stop)
            await node2.start()
            stack.node = node2

        http = aiohttp.ClientSession()
        cleanups.push_async_callback(http.close)
        stack = Stack(
            node=node,
            config_path=config_path,
            home=llmop_home,
            relay=relay,
            console=console,
            portal=portal,
            backends=backends,
            worker=worker,
            worker_id=worker_record["id"],
            reg_token=reg_token,
            worker_keys=worker_keys,
            client=client,
            proxy_url=await start_proxy(),
            http=http,
            start_proxy=start_proxy,
            start_cloudflared=start_cloudflared,
            restart_node=restart_node,
        )
        await stack.wait_online()
        await stack.wait_announced({worker_keys[0]["key_id"]})
        return stack

    yield factory
    await cleanups.aclose()
