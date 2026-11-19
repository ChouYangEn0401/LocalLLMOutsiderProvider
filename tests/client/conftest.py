from __future__ import annotations

import aiohttp
import pytest

from llmop import crypto
from llmop.client import sdk
from llmop.client.config import ClientConfig

from .fakes import API_KEY, FakeGateway


@pytest.fixture(autouse=True)
def llmop_home(tmp_path, monkeypatch):
    """每個測試都用全新的暫存 LLMOP_HOME。"""
    home = tmp_path / "llmop-home"
    monkeypatch.setenv("LLMOP_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(sdk, "POLL_BACKOFF", (0.01,))
    monkeypatch.setattr(sdk, "POLL_MIN_INTERVAL", 0.0)


@pytest.fixture
def signing_key():
    return crypto.generate_signing_key()


@pytest.fixture
def fake(signing_key):
    return FakeGateway(crypto.public_raw(signing_key))


@pytest.fixture
async def gateway_url(fake, aiohttp_server):
    server = await aiohttp_server(fake.app())
    return str(server.make_url("")).rstrip("/")


@pytest.fixture
def config(gateway_url, fake):
    return ClientConfig(gateway_url=gateway_url, api_key=API_KEY, pinned_workers=(fake.public_key,))


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s
