"""``llmop audit exposure`` / ``llmop audit canary`` (llmop.audit).

No real network: sockets are a fake :class:`llmop.audit.Net`; HTTP goes either to the
real listener apps in-process (``AppsTransport``) or to a fake gateway
(``httpx.MockTransport``). Nothing listens on a port, nothing leaves this process.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from llmop import audit, cli, security
from llmop.app import build_state, create_app
from llmop.config import parse_config

PW = security.hash_password("pw-123456", n=2**4)
PORTS = {"local": 18781, "public_ui": 18780, "public_api": 18782}
UI_HOST, API_HOST = "ui.example.test", "api.example.test"


def raw_config(tmp_path: Path, **extra) -> dict:
    raw = {
        "server": {"data_dir": str(tmp_path / "data")},
        "backends": [{"name": "ollama", "type": "ollama", "base_url": "http://127.0.0.1:11434"}],
        "accounts": {"root": {"password_hash": PW, "role": "admin"}},
        "model": "qwen3.5:9b",
        "local_account": "root",
        "listeners": dict(PORTS),
        "tunnels": {"ui": {"public_url": f"https://{UI_HOST}"},
                    "api": {"public_url": f"https://{API_HOST}"}},
    }
    raw.update(extra)
    return raw


class FakeNet(audit.Net):
    """``open``: set of (host, port) that accept connections."""

    def __init__(self, open_=(), ips=("192.168.0.10",)):
        self.open = set(open_)
        self.ips = list(ips)
        self.tried: list[tuple[str, int]] = []

    def tcp_open(self, host, port, timeout=1.0):
        self.tried.append((host, port))
        return (host, port) in self.open

    def local_ips(self):
        return list(self.ips)


def running(*extra) -> FakeNet:
    return FakeNet({("127.0.0.1", p) for p in PORTS.values()} | set(extra))


class AppsTransport(httpx.BaseTransport):
    """Sends each request to the listener app for its host:port, in-process."""

    def __init__(self, apps: dict[str, object]):
        self.clients = {k: TestClient(app, base_url=f"http://{k}") for k, app in apps.items()}
        self.seen: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        host = request.url.host
        key = f"{host}:{request.url.port}" if request.url.port else host
        client = self.clients.get(key)
        if client is None:
            raise httpx.ConnectError("refused", request=request)
        headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
        r = client.request(request.method, request.url.raw_path.decode(), headers=headers,
                           content=request.read())
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)


@pytest.fixture
def node(tmp_path, fake_net):
    cfg = parse_config(raw_config(tmp_path))
    state = build_state(cfg, transport=fake_net.transport)
    local = create_app(state=state, surface="local", port=PORTS["local"])
    ui = create_app(state=state, surface="public-ui")
    api = create_app(state=state, surface="public-api")
    apps = {f"127.0.0.1:{PORTS['local']}": local, f"127.0.0.1:{PORTS['public_ui']}": ui,
            f"127.0.0.1:{PORTS['public_api']}": api, UI_HOST: ui, API_HOST: api}
    transport = AppsTransport(apps)
    http = httpx.Client(transport=transport, follow_redirects=False)
    yield cfg, state, http, transport
    http.close()
    state.store.close()


def by_id(report):
    return {c.id: c for c in report.checks}


# ------------------------------------------------------------------ exposure
def test_clean_node_passes_everything(node):
    cfg, state, http, transport = node
    report = audit.exposure(cfg, state.store, http=http, net=running())
    checks = by_id(report)
    assert report.ok, report.render()
    for cid in ("listen.18781.192.168.0.10", "listen.18780.192.168.0.10",
                "listen.18782.192.168.0.10", "backend.11434.192.168.0.10",
                "config.tunnel-targets", "local.api.root", "local.api.wrong-gate",
                "local.api.no-key", "local.api.fake-key", "local.api.e2e-garbage",
                "local.api.no-console", "local.ui.no-v1", "local.ui.no-worker",
                "local.ui.admin-read", "local.ui.admin-config", "local.ui.admin-write",
                "local.ui.llm-backend"):
        assert checks[cid].status == audit.PASS, (cid, checks[cid])
    # only loopback HTTP; the tunnels are not touched without --public
    assert {r.url.host for r in transport.seen} == {"127.0.0.1"}
    assert not any(c.id.startswith("public.") for c in report.checks)


def test_entrance_on_the_lan_fails(node):
    cfg, state, http, _ = node
    net = running(("192.168.0.10", PORTS["public_api"]))
    report = audit.exposure(cfg, state.store, http=http, net=net)
    assert not report.ok
    assert by_id(report)["listen.18782.192.168.0.10"].status == audit.FAIL


def test_backend_on_the_lan_is_a_warning(node):
    cfg, state, http, _ = node
    report = audit.exposure(cfg, state.store, http=http, net=running(("192.168.0.10", 1234)))
    assert report.ok  # a warning, not a failure: it is the model server's own setting
    assert by_id(report)["backend.1234.192.168.0.10"].status == audit.WARN


def test_only_connect_attempts_on_lan_addresses(node):
    cfg, state, http, _ = node
    net = running()
    audit.exposure(cfg, state.store, http=http, net=net)
    hosts = {h for h, _ in net.tried}
    assert hosts == {"127.0.0.1", "192.168.0.10"}


def test_tunnel_pointing_at_the_console_fails(tmp_path):
    raw = raw_config(tmp_path)
    raw["listeners"] = {"local": 18781, "public_ui": 18780, "public_api": 11434}  # = backend port
    cfg = parse_config(raw)
    report = audit.Report("exposure")
    audit.check_config(report, cfg)
    assert by_id(report)["config.tunnel-targets"].status == audit.FAIL


def test_not_running_skips_http(tmp_path):
    cfg = parse_config(raw_config(tmp_path))
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r)))
    report = audit.exposure(cfg, None, http=http, net=FakeNet(), with_key=True)
    assert not seen
    assert by_id(report)["listen.running"].status == audit.INFO
    assert by_id(report)["key.store"].status == audit.SKIP


def test_public_probes_go_through_the_tunnel_hosts(node):
    cfg, state, http, transport = node
    report = audit.exposure(cfg, state.store, http=http, net=running(), public=True)
    assert report.ok, report.render()
    checks = by_id(report)
    assert checks["public.api.no-key"].status == audit.PASS
    assert checks["public.ui.admin-write"].status == audit.PASS
    assert {API_HOST, UI_HOST} <= {r.url.host for r in transport.seen}


def test_public_without_tunnel_is_skipped(tmp_path, fake_net):
    raw = raw_config(tmp_path)
    raw.pop("tunnels")
    cfg = parse_config(raw)
    report = audit.Report("x")
    assert audit.public_bases(cfg, None) == {"ui": "", "api": ""}
    report = audit.exposure(cfg, None, http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))), net=FakeNet(), public=True)
    assert by_id(report)["public.urls"].status == audit.SKIP


def test_open_gateway_is_reported(node):
    """A listener that answers 200 where it must refuse is a FAIL (regression guard)."""
    cfg, state, _, _ = node
    gate = audit.read_gate(cfg, state.store)
    assert gate

    def handler(request):
        return httpx.Response(200, json={"object": "list", "data": []})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    report = audit.exposure(cfg, state.store, http=http, net=running())
    assert not report.ok
    assert by_id(report)["local.api.no-key"].status == audit.FAIL


def test_with_key_checks_and_always_revokes(node):
    cfg, state, http, _ = node
    report = audit.exposure(cfg, state.store, http=http, net=running(), with_key=True)
    checks = by_id(report)
    assert report.ok, report.render()
    assert checks["key.plain-refused"].status == audit.PASS
    assert checks["key.unsigned-refused"].status == audit.PASS
    assert checks["key.revoked"].status == audit.PASS
    keys = [k for k in state.store.list_guest_keys() if k.label == audit.AUDIT_LABEL]
    assert len(keys) == 1 and keys[0].revoked_at is not None
    assert keys[0].encryption == "required" and keys[0].expires_at <= time.time() + 700


def test_with_key_revokes_even_when_a_probe_crashes(node):
    cfg, state, _, _ = node

    def boom(request):
        raise RuntimeError("probe crashed")

    http = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(RuntimeError):
        audit.check_with_key(audit.Report("x"), http, state.store, "http://127.0.0.1:18782",
                             audit.read_gate(cfg, state.store))
    keys = [k for k in state.store.list_guest_keys() if k.label == audit.AUDIT_LABEL]
    assert keys and all(k.revoked_at is not None for k in keys)


def test_read_gate_never_creates_one(tmp_path):
    from llmop.store import Store

    cfg = parse_config(raw_config(tmp_path))
    store = Store(tmp_path / "x.sqlite3")
    assert audit.read_gate(cfg, store) == ""
    assert not (Path(cfg.data_path) / "api_gate.secret").exists()
    assert store.get_api_link() is None
    store.close()


def test_report_json_and_text():
    r = audit.Report("exposure")
    r.add("a", audit.PASS, "fine")
    r.add("b", audit.FAIL, "bad", "why")
    data = json.loads(r.to_json())
    assert data["ok"] is False and data["counts"]["FAIL"] == 1
    text = r.render()
    assert "[FAIL] bad" in text and "NOT OK" in text
    assert text.isascii()


def test_lan_ip_filter():
    assert audit._routable("192.168.1.2") and audit._routable("10.0.0.5")
    assert not audit._routable("127.0.0.1") and not audit._routable("169.254.1.1")
    assert not audit._routable("0.0.0.0") and not audit._routable("nonsense")


# ------------------------------------------------------------------ canary
class FakeGateway:
    """The encrypted face of a gateway with one worker: 202 first, then done."""

    def __init__(self, model="qwen3.5:9b"):
        from llmop.crypto import generate_worker_key, public_raw, worker_key_id
        from llmop.protocol import b64u_encode

        key = generate_worker_key()
        self.pub = b64u_encode(public_raw(key))
        self.key_id = worker_key_id(public_raw(key))
        self.model = model
        self.bodies: list[bytes] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/e2e/v1/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": self.model, "llmop": {"key_id": self.key_id, "public_key": self.pub}}]})
        if path.endswith("/e2e/v1/chat/completions"):
            self.bodies.append(request.read())
            return httpx.Response(202, json={"status": "pending"})
        if "/e2e/v1/requests/" in path:
            self.polls += 1
            return httpx.Response(200, json={"status": "done", "sealed": "x"})
        return httpx.Response(404, text="Not Found")


@pytest.fixture
def canary_env(tmp_path, monkeypatch):
    from llmop.app import build_state

    monkeypatch.setenv("LLMOP_HOME", str(tmp_path / "home"))
    cfg = parse_config(raw_config(tmp_path))
    state = build_state(cfg, api_link=True)
    yield cfg, state
    state.store.close()


def test_canary_passes_and_never_sends_the_marker_in_clear(canary_env, tmp_path):
    cfg, state = canary_env
    gw = FakeGateway()
    log = tmp_path / "serve.log"
    log.write_text("INFO llmop.secure.api: chat request_id=abc status=200\n", encoding="utf-8")
    report = audit.canary(cfg, state.store, http=httpx.Client(transport=httpx.MockTransport(gw)),
                          scan=[log], fingerprint=gw.key_id, marker="llmop-canary-TESTMARKER",
                          sleep=lambda s: None)
    checks = by_id(report)
    assert report.ok, report.render()
    assert checks["canary.sent"].status == audit.PASS and gw.polls == 1
    assert checks["canary.found"].status == audit.PASS
    assert gw.bodies and all(b"TESTMARKER" not in b for b in gw.bodies)  # sealed
    keys = [k for k in state.store.list_guest_keys() if k.label == audit.AUDIT_LABEL]
    assert keys and all(k.revoked_at is not None for k in keys)


def test_canary_finds_a_leak(canary_env, tmp_path):
    cfg, state = canary_env
    gw = FakeGateway()
    leak = tmp_path / "data" / "leak.log"
    leak.write_text("user said: llmop-canary-LEAK\n", encoding="utf-16-le")
    report = audit.canary(cfg, state.store, http=httpx.Client(transport=httpx.MockTransport(gw)),
                          fingerprint=gw.key_id, marker="llmop-canary-LEAK", sleep=lambda s: None)
    assert not report.ok
    assert "leak.log" in by_id(report)["canary.found"].detail


def test_canary_without_matching_worker_fails_and_revokes(canary_env):
    cfg, state = canary_env
    gw = FakeGateway()
    report = audit.canary(cfg, state.store, http=httpx.Client(transport=httpx.MockTransport(gw)),
                          fingerprint="not-this-one", sleep=lambda s: None)
    assert by_id(report)["canary.worker"].status == audit.FAIL
    assert not gw.bodies
    keys = [k for k in state.store.list_guest_keys() if k.label == audit.AUDIT_LABEL]
    assert keys and all(k.revoked_at is not None for k in keys)


def test_canary_without_gate_fails(tmp_path):
    from llmop.store import Store

    cfg = parse_config(raw_config(tmp_path))
    store = Store(tmp_path / "x.sqlite3")
    report = audit.canary(cfg, store, http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(500))))
    assert by_id(report)["canary.gate"].status == audit.FAIL
    store.close()


def test_local_worker_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMOP_HOME", str(tmp_path))
    assert audit.local_worker_fingerprint() == ""
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "keys.json").write_text(json.dumps({"current": "KID", "keys": []}))
    assert audit.local_worker_fingerprint() == "KID"


# ------------------------------------------------------------------ CLI
def test_cli_help_and_exit_codes(tmp_path, monkeypatch, capsys):
    assert cli.main(["audit", "--help"]) == 0
    assert "exposure" in capsys.readouterr().out
    cfg_path = tmp_path / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(raw_config(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(audit.Net, "local_ips", lambda self: [])
    monkeypatch.setattr(audit.Net, "tcp_open", lambda self, h, p, timeout=1.0: False)
    assert cli.main(["--config", str(cfg_path), "audit", "exposure", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "exposure" and out["ok"] is True
    # canary needs the database of a node that ran at least once
    assert cli.main(["--config", str(cfg_path), "audit", "canary"]) == 2
