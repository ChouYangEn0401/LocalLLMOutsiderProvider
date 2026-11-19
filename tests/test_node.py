"""DISPATCH T-9: the whole node in one process (three real uvicorn listeners on
loopback, fake LLM backend through httpx.MockTransport, fake tunnels)."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from llmop import security
from llmop.app import build_state
from llmop.config import parse_config
from llmop.node import Node, NodeError, startup_warnings

FAST_HASH = security.hash_password("pw", n=2**4)
MODEL = "qwen3.5:9b"
CHAT = {"model": "anything", "messages": [{"role": "user", "content": "Say hello"}]}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_config(tmp_path, **extra):
    raw = {
        "server": {"data_dir": str(tmp_path / "data"), "access_log": False},
        "backends": [{"name": "ollama", "type": "ollama", "base_url": "http://ollama.test:11434",
                      "max_concurrency": 6}],
        "accounts": {"alice": {"password_hash": FAST_HASH},
                     "root": {"password_hash": FAST_HASH, "role": "admin"}},
        "model": MODEL,
        "local_account": "root",
        "listeners": {"local": free_port(), "public_ui": free_port(), "public_api": free_port()},
    }
    raw.update(extra)
    return parse_config(raw)


class FakeTunnel:
    instances: list["FakeTunnel"] = []
    counter = 0

    def __init__(self, settings, local_url, store=None, data_dir=None, **kw):
        FakeTunnel.counter += 1
        self.settings, self.local_url, self.kw = settings, local_url, kw
        self.name = kw.get("name")
        self.mode = settings.mode
        self.status, self.url = "off", None
        self.starts = self.stops = 0
        self.n = FakeTunnel.counter
        FakeTunnel.instances.append(self)

    def start(self):
        self.starts += 1
        self.status = "ready"
        self.url = f"https://{self.name}-{self.n}.trycloudflare.com"
        return True

    def stop(self, timeout: float = 15.0):
        self.stops += 1
        self.status, self.url = "stopped", None


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=10)


@pytest.fixture
def node_factory(tmp_path, fake_net):
    FakeTunnel.instances = []

    def factory(*, tunnels=False, **extra):
        cfg = make_config(tmp_path, **extra)
        state = build_state(cfg, transport=fake_net.transport)
        node = Node(cfg, state=state, tunnels=tunnels, worker=False, out=lambda _t: None,
                    tunnel_factory=FakeTunnel)
        return node

    return factory  # each test stops its node in a finally block


def urls(node):
    ls = node.cfg.listeners
    return (f"http://127.0.0.1:{ls.local}", f"http://127.0.0.1:{ls.public_ui}",
            f"http://127.0.0.1:{ls.public_api}")


# --------------------------------------------------------------------------- lifecycle
async def test_start_local_no_key_public_with_key_and_clean_shutdown(node_factory, fake_net):
    node = node_factory()
    await node.start()
    try:
        local, ui, api = urls(node)
        state = node.state
        key, _ = state.store.create_api_key("alice", "k")
        link = state.api_link.current_url()
        assert link.startswith(api + "/") and len(link.rsplit("/", 1)[1]) >= 43
        async with client() as c:
            r = await c.post(f"{local}/v1/chat/completions", json=CHAT)
            assert r.status_code == 200, r.text
            assert fake_net.ollama.last["model"] == MODEL
            r = await c.post(f"{link}/v1/chat/completions", json=CHAT,
                             headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 200, r.text
            r = await c.post(f"{link}/v1/chat/completions", json=CHAT)
            assert r.status_code == 401
            # the scheduler is wired: the public request went through the queue
            assert state.scheduler.snapshot()["running"] == 0
        users = {row["username"] for row in state.store.recent_usage(None)}
        assert users == {"root", "alice"}
    finally:
        await node.stop()
    assert all(s.task.done() for s in node.servers)
    for port in (node.cfg.listeners.local, node.cfg.listeners.public_ui,
                 node.cfg.listeners.public_api):
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))  # the port is free again


async def test_surfaces_are_separate(node_factory):
    node = node_factory()
    await node.start()
    try:
        local, ui, api = urls(node)
        gate = node.state.api_link.current_url()
        async with client() as c:
            # public-ui: account website only, no API at all
            for path in ("/v1/models", "/v1/chat/completions", "/e2e/v1/models", "/worker/ws"):
                assert (await c.get(ui + path)).status_code == 404, path
            assert (await c.get(ui + "/ui/login")).status_code == 200
            # public-api: no login page, no cookies, nothing without the gate
            for path in ("/", "/ui/login", "/ui/api/session", "/ui/dashboard"):
                r = await c.get(gate + path)
                assert r.status_code == 404, path
                assert "set-cookie" not in r.headers
            r = await c.post(gate + "/ui/api/login", json={"username": "alice", "password": "pw"})
            assert r.status_code in (404, 405) and "set-cookie" not in r.headers
            for path in ("/v1/models", "/e2e/v1/models", "/health"):
                assert (await c.get(api + path)).status_code == 404, path
            assert (await c.get(gate + "/health")).status_code == 200
            r = await c.get(gate + "/e2e/v1/models")
            assert r.status_code == 401  # the encrypted relay is mounted (needs a key)
            assert "set-cookie" not in r.headers
            # local: console + key-less /v1, only for this computer
            assert (await c.get(local + "/v1/models")).status_code == 200
            for headers in ({"Cf-Connecting-Ip": "1.2.3.4"}, {"Host": "evil.example"},
                            {"X-Forwarded-For": "1.2.3.4"}, {"Origin": "https://evil.example"}):
                r = await c.get(local + "/v1/models", headers=headers)
                assert r.status_code == 403, headers
    finally:
        await node.stop()


async def test_public_host_check_and_body_limit(node_factory):
    node = node_factory(tunnels=True)
    await node.start()
    try:
        local, ui, api = urls(node)
        gate = f"{api}/{node.state.api_link.gate}"  # the link names the tunnel host
        ui_host = node.state.tunnels["ui"].url.split("//")[1]
        api_host = node.state.tunnels["api"].url.split("//")[1]
        async with client() as c:
            assert (await c.get(ui + "/ui/login", headers={"Host": "evil.example"})).status_code == 421
            assert (await c.get(ui + "/ui/login", headers={"Host": ui_host})).status_code == 200
            assert (await c.get(ui + "/ui/login", headers={"Host": api_host})).status_code == 421
            assert (await c.get(gate + "/health", headers={"Host": "evil.example"})).status_code == 421
            assert (await c.get(gate + "/health", headers={"Host": api_host})).status_code == 200
            assert (await c.get(gate + "/health", headers={"Host": ui_host})).status_code == 421
            big = b"x" * (1024 * 1024 + 10)
            r = await c.post(ui + "/ui/api/login", content=big,
                             headers={"Content-Type": "application/json"})
            assert r.status_code == 413

            async def chunks():
                for _ in range(20):
                    yield b"x" * (1024 * 1024)

            key, _ = node.state.store.create_api_key("alice", "k")
            r = await c.post(gate + "/v1/chat/completions", content=chunks(),
                             headers={"Content-Type": "application/json",
                                      "Authorization": f"Bearer {key}"})
            assert r.status_code == 413
    finally:
        await node.stop()


async def test_guest_key_end_to_end(node_factory, fake_net):
    node = node_factory()
    await node.start()
    try:
        store = node.state.store
        key, rec = store.create_guest_key("visitor", budget_tokens=1000)
        gate = node.state.api_link.current_url()
        auth = {"Authorization": f"Bearer {key}"}
        async with client() as c:
            r = await c.post(gate + "/v1/chat/completions", json=CHAT, headers=auth)
            assert r.status_code == 200, r.text
            me = (await c.get(gate + "/v1/me", headers=auth)).json()
            assert me.get("key", {}).get("id", rec.id) == rec.id
            [row] = store.recent_usage(None)
            assert row["username"] == f"guest:{rec.id}" and row["key_id"] == rec.id
            fake_net.ollama.prompt_tokens = 5000  # one large request uses up the budget
            assert (await c.post(gate + "/v1/chat/completions", json=CHAT,
                                 headers=auth)).status_code == 200
            r = await c.post(gate + "/v1/chat/completions", json=CHAT, headers=auth)
            assert r.status_code == 401 and r.json()["error"]["code"] == "key_exhausted"
    finally:
        await node.stop()


# --------------------------------------------------------------------------- API link
async def test_rotate_path_old_gate_404_key_still_valid(node_factory):
    node = node_factory()
    await node.start()
    try:
        state = node.state
        key, _ = state.store.create_api_key("alice", "k")
        auth = {"Authorization": f"Bearer {key}"}
        old = state.api_link.current_url()
        async with client() as c:
            assert (await c.get(old + "/v1/models", headers=auth)).status_code == 200
            result = await state.api_link.rotate(reason="test")
            new = state.api_link.current_url()
            assert new != old and result["url"] == new
            assert (await c.get(old + "/v1/models", headers=auth)).status_code == 404
            assert (await c.get(new + "/v1/models", headers=auth)).status_code == 200
            # with a grace period both work until it ends
            await state.api_link.rotate(reason="grace", grace_seconds=0.6)
            newer = state.api_link.current_url()
            assert (await c.get(new + "/v1/models", headers=auth)).status_code == 200
            assert (await c.get(newer + "/v1/models", headers=auth)).status_code == 200
            await asyncio.sleep(0.8)
            assert (await c.get(new + "/v1/models", headers=auth)).status_code == 404
            assert (await c.get(newer + "/v1/models", headers=auth)).status_code == 200
    finally:
        await node.stop()


async def test_rotate_with_new_host_restarts_only_the_api_tunnel(node_factory):
    node = node_factory(tunnels=True)
    await node.start()
    try:
        state = node.state
        ui, api = state.tunnels["ui"], state.tunnels["api"]
        assert state.tunnel is ui and ui.starts == 1 and api.starts == 1
        assert ui.local_url.endswith(f":{node.cfg.listeners.public_ui}")
        assert api.local_url.endswith(f":{node.cfg.listeners.public_api}")
        forbidden = api.kw["forbidden_ports"]
        assert node.cfg.listeners.local in forbidden and 11434 in forbidden
        old_link = state.api_link.current_url()
        assert old_link.startswith(api.url + "/")
        key, _ = state.store.create_api_key("alice", "k")
        result = await state.api_link.rotate(change_host=True, reason="leaked")
        new_api = state.tunnels["api"]
        assert new_api is not api and new_api.starts == 1 and api.stops == 1
        assert state.tunnels["ui"] is ui and ui.starts == 1 and ui.stops == 0
        assert result["change_host"] is True
        assert state.api_link.current_url().startswith(new_api.url + "/")
        # the old gate is gone; the key keeps working on the new link (through loopback)
        _, _, api_local = urls(node)
        new_gate = state.api_link.current_url().rsplit("/", 1)[1]
        old_gate = old_link.rsplit("/", 1)[1]
        auth = {"Authorization": f"Bearer {key}"}
        async with client() as c:
            assert (await c.get(f"{api_local}/{old_gate}/v1/models", headers=auth)).status_code == 404
            assert (await c.get(f"{api_local}/{new_gate}/v1/models", headers=auth)).status_code == 200
        [entry] = [e for e in state.api_link.audit_entries() if e["reason"] == "leaked"]
        assert entry["change_host"] is True and len(entry["old_gate_sha256"]) == 64
        assert old_gate not in json.dumps(state.api_link.audit_entries())
    finally:
        await node.stop()
    assert ui.stops == 1 and state.tunnels["api"].stops == 1


async def test_rotate_via_the_local_console(node_factory):
    """The console button (T-8 web route) reaches ApiLink.rotate."""
    node = node_factory()
    await node.start()
    try:
        local, _, _ = urls(node)
        old = node.state.api_link.current_url()
        async with client() as c:
            r = await c.post(local + "/ui/api/login", json={"username": "root", "password": "pw"})
            assert r.status_code == 200, r.text
            csrf = r.json()["csrf"]
            r = await c.post(local + "/ui/api/admin/api-link/rotate", json={"reason": "button"},
                             headers={"X-CSRF-Token": csrf})
            assert r.status_code == 200, r.text
            assert r.json()["api_link"]["url"] == node.state.api_link.current_url() != old
    finally:
        await node.stop()


async def test_cli_rotation_in_another_process_is_picked_up(node_factory, tmp_path):
    from llmop.apilink import ApiLink
    from llmop.store import Store

    node = node_factory()
    await node.start()
    try:
        old = node.state.api_link.current_url()
        other = Store(node.cfg.data_path / "llmop.sqlite3")
        ApiLink(other, node.cfg.data_path).rotate_path("cli")
        other.close()
        node.state.api_link.sync(force=True)
        new = node.state.api_link.current_url()
        assert new != old
        async with client() as c:
            assert (await c.get(old + "/health")).status_code == 404
            assert (await c.get(new + "/health")).status_code == 200
    finally:
        await node.stop()


# --------------------------------------------------------------------------- checks
async def test_busy_port_refuses_to_start(node_factory):
    node = node_factory()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", node.cfg.listeners.public_ui))
        busy.listen()
        with pytest.raises(NodeError, match="cannot listen"):
            await node.start()
    await node.stop()


def test_startup_warnings(tmp_path):
    cfg = make_config(tmp_path, queue={"slots": 6})
    assert startup_warnings(cfg) == []
    cfg = make_config(tmp_path, queue={"slots": 6}, backends=[
        {"name": "cpp", "type": "llamacpp", "base_url": "http://127.0.0.1:8080", "max_concurrency": 2}])
    text = " ".join(startup_warnings(cfg))
    assert "queue.slots is 6" in text and "context" in text and "cpp" in text
    cfg = make_config(tmp_path, model="")
    assert any("model is not set" in w for w in startup_warnings(cfg))


async def test_in_process_worker_registers_over_loopback(node_factory, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from llmop.worker import cli as worker_cli

    monkeypatch.setenv("LLMOP_HOME", str(tmp_path / "home"))
    assert worker_cli.main(["init"]) == 0
    node = node_factory()
    node.want_worker = True
    await node.start()
    try:
        hub = node.state.hub
        for _ in range(200):
            if hub.workers:
                break
            await asyncio.sleep(0.05)
        assert len(hub.workers) == 1
        [worker] = node.state.store.list_workers()
        assert worker.name == "this computer"
        assert (tmp_path / "home" / "worker" / "state.json").exists()
    finally:
        await node.stop()
    assert node.worker_task.done()
