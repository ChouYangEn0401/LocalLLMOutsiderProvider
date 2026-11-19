"""T-R security review: shared fixtures.

``sec`` builds one v0.2 node state (in-memory store, fake LLM through
``httpx.MockTransport``) and the three listener apps exactly as ``llmop.node`` does
(``create_app(state=..., surface=...)``), driven in-process with
``httpx.ASGITransport`` (TCP peer 127.0.0.1, like cloudflared on the same machine).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from llmop import security
from llmop.app import build_state, create_app
from llmop.config import parse_config
from llmop.store import Store

FAST_HASH = security.hash_password("pw-123456", n=2**4)
PASSWORD = "pw-123456"
MODEL = "qwen3.5:9b"
CHAT = {"model": "anything", "messages": [{"role": "user", "content": "Say hello"}]}
SIG = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
BACKEND_SECRET = "sk-backend-SECRET-0123456789"
PORTS = {"local": 18781, "public_ui": 18780, "public_api": 18782}


def raw_config(**extra) -> dict:
    raw = {
        "server": {"access_log": True},
        "backends": [{"name": "ollama", "type": "ollama", "base_url": "http://ollama.test:11434",
                      "max_concurrency": 6, "api_key": BACKEND_SECRET}],
        "accounts": {"alice": {"password_hash": FAST_HASH, "max_api_keys": 3},
                     "root": {"password_hash": FAST_HASH, "role": "admin", "tier": "vip3"}},
        "model": MODEL,
        "local_account": "root",
        "listeners": dict(PORTS),
    }
    raw.update(extra)
    return raw


@dataclass
class Node:
    state: Any
    local: httpx.AsyncClient
    ui: httpx.AsyncClient
    api: httpx.AsyncClient  # base URL already contains the gate
    api_root: httpx.AsyncClient  # no gate

    @property
    def gate(self) -> str:
        return self.state.api_link.gate

    async def aclose(self) -> None:
        for c in (self.local, self.ui, self.api, self.api_root):
            await c.aclose()


def _client(app, base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base, timeout=10)


@pytest.fixture
async def sec(fake_net):
    cfg = parse_config(raw_config())
    state = build_state(cfg, store=Store(":memory:"), transport=fake_net.transport, scheduler=True)
    local = create_app(state=state, surface="local", port=PORTS["local"])
    ui = create_app(state=state, surface="public-ui")
    api = create_app(state=state, surface="public-api")
    base_local = f"http://127.0.0.1:{PORTS['local']}"
    base_ui = f"http://127.0.0.1:{PORTS['public_ui']}"
    base_api = f"http://127.0.0.1:{PORTS['public_api']}"
    node = Node(state, _client(local, base_local), _client(ui, base_ui),
                _client(api, f"{base_api}/{state.api_link.gate}"), _client(api, base_api))
    try:
        yield node
    finally:
        await node.aclose()
        await state.router.aclose()


async def login(client: httpx.AsyncClient, user: str = "root") -> str:
    """Log in on ``client``'s surface; returns the CSRF token (cookie kept by the client)."""
    r = await client.post("/ui/api/login", json={"username": user, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]
