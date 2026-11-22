"""端對端：真的以子程序跑 ``llmop -c config.yaml serve --no-tunnel --worker``（使用者實際下的指令）。

節點、節點內的 worker 都在子程序裡；測試這邊只有假後端、帳號網站的使用者、用戶端代理。
走一次：啟動 → 本機免 key 的 /v1 → 控制台看到 worker 上線與 API 連結 → 帳號網站建立加密 key →
用戶端代理經 gate 走加密面 → 同一把 key 走明文被拒 → SIGTERM 乾淨結束。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys

import aiohttp
import yaml

from llmop import paths
from llmop.client import cli as client_cli
from llmop.client.config import ClientConfig, load_signing_key
from llmop.worker import cli as worker_cli

from .conftest import node_config, start_backends
from .harness import (
    ALICE_PASSWORD,
    DEFAULT_REPLY,
    MODELS,
    ROOT_PASSWORD,
    WebClient,
    parse_key_blocks,
    proxy_app,
    wait_until,
)


async def test_llmop_serve_subprocess_end_to_end(llmop_home, tmp_path, aiohttp_server, capsys):
    backends = await start_backends(aiohttp_server)
    raw = node_config(tmp_path, backends)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    ls = raw["listeners"]
    local, ui = f"http://127.0.0.1:{ls['local']}", f"http://127.0.0.1:{ls['public_ui']}"

    # 節點內的 worker：先 `llmop worker init`（serve 會把 gateway_url 換成本機入口並自動註冊）。
    capsys.readouterr()
    assert worker_cli.main(["init"]) == 0
    (worker_key,) = parse_key_blocks(capsys.readouterr().out)
    worker_home = paths.home("worker")
    wcfg = paths.read_json(worker_home / "config.json")
    wcfg["backends"] = [{"kind": kind, "base_url": fake.base_url} for kind, fake in backends.items()]
    paths.write_json(worker_home / "config.json", wcfg, secret=True)

    env = {**os.environ, "LLMOP_HOME": str(llmop_home), "PYTHONUNBUFFERED": "1"}
    # Windows has no SIGTERM for another process (send_signal(SIGTERM) = TerminateProcess,
    # a hard kill). Closing the console window sends CTRL_BREAK / SIGBREAK, which the node
    # handles like SIGTERM; it needs its own process group to receive it.
    windows = os.name == "nt"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "llmop", "-c", str(config_path), "serve", "--no-tunnel", "--worker",
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    output = bytearray()

    async def drain() -> None:
        while chunk := await proc.stdout.read(4096):
            output.extend(chunk)

    reader = asyncio.create_task(drain())
    http = aiohttp.ClientSession()
    console, portal = WebClient(local), WebClient(ui)
    proxy_server = None
    try:
        async def healthy() -> bool:
            if proc.returncode is not None:
                raise AssertionError(f"llmop serve 結束了：{output.decode(errors='replace')}")
            try:
                async with http.get(local + "/health") as resp:
                    return resp.status == 200
            except aiohttp.ClientError:
                return False

        await wait_until(healthy, timeout=30, interval=0.1, what=" llmop serve 啟動")

        # 本機免 key 的 /v1：走節點的後端轉接層（單一模型）。
        async with http.post(local + "/v1/chat/completions",
                             json={"model": "x", "messages": [{"role": "user", "content": "嗨"}]}) as resp:
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["model"] == MODELS["ollama"]

        # 控制台：節點內的 worker 自動註冊並上線；API 連結含 gate。
        await console.login("root", ROOT_PASSWORD)

        async def worker_online() -> bool:
            workers = (await console.overview())["workers"]
            return len(workers) == 1 and workers[0]["online"] and workers[0]["name"] == "this computer"

        await wait_until(worker_online, timeout=20, what=" 節點內的 worker 上線")
        link = (await console.overview())["api_link"]["url"]
        assert re.fullmatch(rf"http://127\.0\.0\.1:{ls['public_api']}/[A-Za-z0-9_-]{{40,}}", link), link

        # 帳號網站：alice 以 `llmop client keygen` 的公鑰建立加密專用的 key。
        assert client_cli.main(["keygen"]) == 0
        public_key = re.search(r"public_key: (\S+)", capsys.readouterr().out).group(1)
        await portal.login("alice", ALICE_PASSWORD)
        _, api_key = await portal.create_key("cli", sig_public_key=public_key, encryption="required")

        # 用戶端代理經 gate 走加密面，釘選 worker 主控台印出的公鑰。
        cfg = ClientConfig(gateway_url=link, api_key=api_key, pinned_workers=(worker_key["public_key"],))
        proxy_server = await aiohttp_server(proxy_app(cfg, load_signing_key()))
        # worker 的重放防禦拒絕 ts（整數秒）早於它啟動時刻的請求；剛啟動的同一秒內送出的請求會被當成
        # stale_timestamp（B 的既有行為，見 harness.WorkerRunner）。子程序裡的 worker 無法調時鐘，等過這一秒。
        await asyncio.sleep(1.1)
        async with http.post(f"http://127.0.0.1:{proxy_server.port}/v1/chat/completions",
                             json={"model": MODELS["ollama"], "messages": [{"role": "user", "content": "嗨"}]}
                             ) as resp:
            data = await resp.json()
            assert resp.status == 200, data
        assert data["choices"][0]["message"]["content"] == DEFAULT_REPLY
        assert backends["ollama"].paths == ["/v1/chat/completions", "/api/chat"]  # 節點一次、worker 一次

        # 同一把 key 走明文 /v1：403。
        async with http.post(link + "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                             json={"model": "x", "messages": [{"role": "user", "content": "嗨"}]}) as resp:
            assert resp.status == 403
            assert (await resp.json())["error"]["code"] == "encryption_required"
    finally:
        await console.close()
        await portal.close()
        await http.close()
        if proc.returncode is None:
            proc.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGTERM)
        try:
            code = await asyncio.wait_for(proc.wait(), 30)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        await reader

    text = output.decode(errors="replace")
    assert code == 0, text
    assert "llmop stopped." in text
    assert "Traceback" not in text
    gate = link.rsplit("/", 1)[1]
    # 橫幅會印出 API 連結（給操作者看）；除此之外，紀錄裡不該出現 gate。
    assert text.count(gate) == text.count(link), [line for line in text.splitlines() if gate in line]
