"""What ``llmop serve`` prints at startup: the API link only masked (the secret path
must not end up on the screen or in a log file), no local ``/v1`` line, and a hint
to ``llmop tunnel show-api`` for the full link. All URLs here are fake."""

from __future__ import annotations

import logging
import socket
from types import SimpleNamespace

import pytest

from llmop import security
from llmop.apilink import ApiLink, mask_api_link
from llmop.app import build_state
from llmop.config import TunnelSettings, parse_config
from llmop.node import Node
from llmop.store import Store
from llmop.tunnel import TunnelManager

API_HOST = "https://api-host.example.test"
HINT = "llmop tunnel show-api"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def link():
    store = Store(":memory:")
    yield ApiLink(store, None, base_url=lambda: API_HOST)
    store.close()


def _assert_masked(text: str, link: ApiLink) -> None:
    gate = link.gate
    assert len(gate) >= 43
    assert gate not in text
    assert gate[3:13] not in text  # only the first 3 characters may show
    assert f"{API_HOST}/{gate[:3]}…/v1" in text
    assert HINT in text


# --------------------------------------------------------------------------- the helper
@pytest.mark.parametrize("url, want", [
    ("https://h.example.test/k7QzzzzzzzzzzzzzzzzzzzzzzZ", "https://h.example.test/k7Q…"),
    ("https://h.example.test/k7Qzzzzzzzzzzzzz/v1", "https://h.example.test/k7Q…"),
    ("https://h.example.test/k7Qzzzzzzzzzzzzz/e2e/v1/", "https://h.example.test/k7Q…"),
    ("http://127.0.0.1:8782/ab", "http://127.0.0.1:8782/ab…"),
    ("https://h.example.test", "https://h.example.test"),
    ("https://h.example.test/", "https://h.example.test"),
    ("", ""),
])
def test_mask_api_link(url, want):
    assert mask_api_link(url) == want


def test_masked_url_keeps_host_and_three_characters(link):
    masked = link.masked_url()
    assert masked == f"{API_HOST}/{link.gate[:3]}…"
    assert link.gate not in masked


# --------------------------------------------------------------------------- node banner
def test_node_banner_masks_the_api_link_and_drops_the_local_api_line(link):
    out: list[str] = []
    cfg = parse_config({})
    state = SimpleNamespace(api_link=link, tunnels={})
    Node(cfg, state=state, out=out.append)._banner()
    text = "\n".join(out)
    _assert_masked(text, link)
    assert "Local API" not in text
    assert f"127.0.0.1:{cfg.listeners.local}/v1" not in text
    # the general information stays
    assert f"127.0.0.1:{cfg.listeners.local}/" in text  # Console
    assert f"127.0.0.1:{cfg.listeners.public_ui}/" in text  # Accounts
    assert "Config" in text and "Stop" in text


def test_node_api_tunnel_callback_prints_the_masked_link(link):
    out: list[str] = []
    seen = {}

    def factory(settings, local_url, store=None, data_dir=None, **kw):
        seen.update(kw)
        return SimpleNamespace(**kw)

    state = SimpleNamespace(api_link=link, tunnels={}, store=None)
    node = Node(parse_config({}), state=state, out=out.append, tunnel_factory=factory)
    node._make_tunnel("api", TunnelSettings(enabled=True))
    seen["on_url"](API_HOST)
    _assert_masked("\n".join(out), link)


async def test_started_node_never_prints_or_logs_the_gate(tmp_path, fake_net, caplog):
    """A real start (three loopback listeners, fake tunnels, fake backend): neither the
    console output nor any log record contains the secret path."""

    class FakeTunnel:
        def __init__(self, settings, local_url, store=None, data_dir=None, **kw):
            self.name, self.mode, self.kw = kw.get("name"), settings.mode, kw
            self.status, self.url = "off", None

        def start(self):
            self.status, self.url = "ready", f"https://{self.name}-host.example.test"
            if self.kw.get("on_url"):
                self.kw["on_url"](self.url)
            return True

        def stop(self, timeout: float = 15.0):
            self.status, self.url = "stopped", None

    fast = security.hash_password("pw", n=2**4)
    cfg = parse_config({
        "server": {"data_dir": str(tmp_path / "data"), "access_log": True},
        "backends": [{"name": "ollama", "type": "ollama", "base_url": "http://ollama.test:11434",
                      "max_concurrency": 1}],
        "accounts": {"root": {"password_hash": fast, "role": "admin"}},
        "model": "m",
        "local_account": "root",
        "listeners": {"local": _free_port(), "public_ui": _free_port(), "public_api": _free_port()},
    })
    out: list[str] = []
    caplog.set_level(logging.DEBUG)
    node = Node(cfg, state=build_state(cfg, transport=fake_net.transport), tunnels=True,
                worker=False, out=out.append, tunnel_factory=FakeTunnel)
    await node.start()
    try:
        gate = node.state.api_link.gate
        text = "\n".join(out)
        assert gate and gate not in text and gate not in caplog.text
        assert f"https://api-host.example.test/{gate[:3]}…/v1" in text
        assert HINT in text
        assert f"127.0.0.1:{cfg.listeners.local}/v1" not in text
    finally:
        await node.stop()


# --------------------------------------------------------------------------- v0.1 banner
def test_single_listener_banner_has_no_local_api_line():
    from llmop.cli import _make_banner

    cfg = parse_config({})
    local = "http://127.0.0.1:8780"
    rows = _make_banner(cfg, local, None)
    text = "\n".join(rows)
    assert f"{local}/v1" not in text and "API base" not in text
    assert f"  Local     : {local}/" in rows[4]  # the local console line stays
    assert f"{local}/ui/login" in text  # and the login line
    assert "Stop" in text


# --------------------------------------------------------------------------- tunnel banners
def _publish(tmp_path, name: str, url: str) -> str:
    out: list[str] = []
    mgr = TunnelManager(TunnelSettings(enabled=True), "http://127.0.0.1:8782", None, tmp_path,
                        out=out.append, name=name)
    mgr._publish(url)
    return "\n".join(out)


def test_node_ui_tunnel_banner_has_no_api_base(tmp_path):
    text = _publish(tmp_path, "ui", "https://ui-host.example.test")
    assert "https://ui-host.example.test/ui/login" in text
    assert "/v1" not in text  # the account website does not serve the API


def test_api_tunnel_banner_shows_only_the_host(tmp_path):
    text = _publish(tmp_path, "api", "https://api-host.example.test")
    assert "https://api-host.example.test" in text and HINT in text
    assert "/v1" not in text


def test_v01_tunnel_banner_keeps_its_api_base(tmp_path):
    # v0.1 single listener: no secret path, /v1 is served on the tunnel host itself
    text = _publish(tmp_path, "", "https://one-host.example.test")
    assert "https://one-host.example.test/v1" in text
