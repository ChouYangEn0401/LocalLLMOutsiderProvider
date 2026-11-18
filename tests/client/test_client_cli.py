"""在全新的 LLMOP_HOME 上，以子行程實際執行 llmop client keygen / models / chat / proxy。"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import stat
import sys
from pathlib import Path

import aiohttp
import pytest

import llmop
from llmop import crypto, paths
from llmop.client.config import config_path, load_signing_key, signing_key_info, signing_key_path

from .fakes import API_KEY, PROMPT, REPLY_TEXT, FakeGateway

SRC = Path(llmop.__file__).resolve().parents[1]
MODEL = "qwen3:8b"


def _env(home: Path) -> dict:
    return {**os.environ, "LLMOP_HOME": str(home), "PYTHONPATH": str(SRC)}


async def run_cli(home: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "llmop",
        "client",
        *args,
        env=_env(home),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    return proc.returncode, out.decode(), err.decode()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def setup(llmop_home, aiohttp_server):
    """keygen 之後，啟動認得這把簽章金鑰的假閘道，並寫好 config.json。"""
    code, out, _ = await run_cli(llmop_home, "keygen")
    assert code == 0
    key = load_signing_key()
    fake = FakeGateway(crypto.public_raw(key))
    server = await aiohttp_server(fake.app())
    paths.write_json(
        config_path(),
        {
            "gateway_url": str(server.make_url("")).rstrip("/"),
            "api_key": API_KEY,
            "pinned_workers": [fake.public_key],
            "proxy_port": free_port(),
            "default_max_tokens": 512,
        },
        secret=True,
    )
    return fake


async def test_keygen_on_fresh_home(llmop_home):
    code, out, _ = await run_cli(llmop_home, "keygen")
    assert code == 0
    public_key, sig_key_id = signing_key_info(load_signing_key())
    assert f"public_key: {public_key}" in out
    assert f"sig_key_id: {sig_key_id}" in out
    assert stat.S_IMODE(os.stat(signing_key_path()).st_mode) == 0o600
    private_b64u = paths.read_json(signing_key_path())["private_key"]
    assert private_b64u not in out

    code, again, _ = await run_cli(llmop_home, "keygen")
    assert code == 0
    assert f"public_key: {public_key}" in again
    assert "已存在" in again


async def test_commands_without_config_fail_cleanly(llmop_home):
    for args in (["models"], ["chat", "--model", MODEL, "hi"], ["proxy"]):
        code, _, err = await run_cli(llmop_home, *args)
        assert code == 1
        assert "錯誤" in err and "Traceback" not in err


async def test_models(llmop_home, setup):
    code, out, err = await run_cli(llmop_home, "models")
    assert code == 0, err
    assert out.strip() == f"{MODEL}\t{setup.key_id}"


async def test_chat(llmop_home, setup):
    code, out, err = await run_cli(llmop_home, "chat", "--model", MODEL, "--max-tokens", "64", PROMPT)
    assert code == 0, err
    assert out.strip() == REPLY_TEXT
    header, body = setup.received[0]
    assert (header.model, header.max_tokens) == (MODEL, 64)
    assert body == {"messages": [{"role": "user", "content": PROMPT}]}
    assert PROMPT not in err and API_KEY not in err


async def test_chat_uses_default_max_tokens_and_reports_errors(llmop_home, setup):
    setup.gateway_error = "quota_exceeded"
    code, out, err = await run_cli(llmop_home, "chat", "--model", MODEL, PROMPT)
    assert code == 1
    assert "quota_exceeded" in err
    assert setup.received[0][0].max_tokens == 512


async def test_proxy(llmop_home, setup):
    port = paths.read_json(config_path())["proxy_port"]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "llmop",
        "client",
        "proxy",
        env=_env(llmop_home),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(200):
                try:
                    async with session.get(f"{base}/v1/models") as resp:
                        models = await resp.json()
                    break
                except aiohttp.ClientConnectionError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("代理沒有啟動")
            assert [m["id"] for m in models["data"]] == [MODEL]
            request = {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}], "stream": True}
            async with session.post(f"{base}/v1/chat/completions", json=request) as resp:
                assert resp.headers["Content-Type"].startswith("text/event-stream")
                text = await resp.text()
        assert REPLY_TEXT in text
        assert text.endswith("data: [DONE]\n\n")
    finally:
        proc.send_signal(signal.SIGINT)
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    assert "127.0.0.1" in out.decode()
    logs = err.decode()
    assert "request_id=" in logs
    for secret in (PROMPT, REPLY_TEXT, API_KEY):
        assert secret not in logs
