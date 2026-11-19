"""T-R: the remaining MERGE_PLAN 6.1 controls (one small test each), the gate's
secrecy, GUEST identity on both paths, and two fixed races.

Classes: 1 injection (cloudflared argv), 4 XSS (no HTML sinks), 6 session storage,
7 worker handshake throttle, 8 replay, 17 supply chain (pinned Python hash),
24 log forging, 26 secret file modes; MERGE_PLAN 6.2 (a) client proxy, (e)
proxy_headers, (f) image_url documented, (g) supply chain.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path

import pytest

from llmop import netguard
from llmop.apilink import ApiLink
from llmop.auth import usage_username
from llmop.config import TunnelSettings
from llmop.netguard import clean_log, redact_path
from llmop.store import Store
from llmop.tunnel import TunnelManager

from .conftest import CHAT, PORTS

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "llmop"


# --------------------------------------------------------------------------- gate secrecy
async def test_public_api_access_log_never_contains_the_gate(sec, caplog):
    key, _ = sec.state.store.create_api_key("alice", "k")
    caplog.set_level(logging.DEBUG)
    gate = sec.gate
    await sec.api.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key}"})
    await sec.api.get("/v1/models?secret=1", headers={"Authorization": f"Bearer {key}"})
    await sec.api_root.get(f"/{gate}x/v1/models")  # a near miss
    await sec.api.post("/v1/chat/completions", content=b"[" * 50_000)
    # server-side loggers only (the test's own httpx client logs the URL it called)
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert gate not in text and "secret=1" not in text
    assert "/***/v1/models" in text  # the access log line exists, masked


def test_websocket_handshake_log_line_is_masked():
    from llmop.node import _RedactWebSocketPath

    rec = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, '%s - "WebSocket %s" %s',
                            ("127.0.0.1:1", "/GATE123456789/worker/ws", "[accepted]"), None)
    assert _RedactWebSocketPath().filter(rec)
    assert "GATE123456789" not in rec.getMessage() and "/***/worker/ws" in rec.getMessage()


def test_worker_reconnect_log_does_not_contain_the_gate():
    """T-R fix: aiohttp's handshake error carries the full gateway_url (with the gate)."""
    from llmop.worker.link import _describe

    exc = RuntimeError("404, message='Invalid response status', "
                       "url='wss://api.example.trycloudflare.com/S3cr3tGate_-x/worker/ws'")
    text = _describe(exc)
    assert "S3cr3tGate_-x" not in text and "/***/worker/ws" in text


def test_redact_path_and_clean_log():
    """(class 24) log forging: control characters escaped, keys masked, gate masked."""
    assert redact_path("/abc123/v1/chat?k=sk-llop-xyz") == "/***/v1/chat?***"
    out = clean_log("alice\nINFO forged line\x1b[31m sk-llop-AAAAAAAAAAAA Bearer abcdefghijkl")
    assert "\n" not in out and "\x1b" not in out
    assert "sk-llop-AAAA" not in out and "abcdefghijkl" not in out


def test_gate_file_is_private_and_only_hashes_in_the_database(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    link = ApiLink(store, tmp_path)
    gate = link.gate
    assert len(gate) >= 43
    row = store.get_api_link()
    assert gate not in str(row) and row["current_gate_hash"] == netguard.gate_hash(gate)
    if os.name == "posix":  # (class 26) 0600 has no effect on Windows (accepted, 6.1-26)
        assert (tmp_path / "api_gate.secret").stat().st_mode & 0o077 == 0
    audit = (tmp_path / "api_link_audit.jsonl")
    link.rotate_path("test")
    assert gate not in audit.read_text() and link.gate not in audit.read_text()
    store.close()


# --------------------------------------------------------------------------- GUEST identity
async def test_guest_key_has_one_identity_on_direct_and_encrypted_paths(sec):
    """Usage rows and the queue use ``guest:<key id>`` on /v1 and /e2e/v1 alike."""
    from llmop.secure.api import SecureContext, _settle
    from llmop.secure.hub import Hub, Job

    gkey, rec = sec.state.store.create_guest_key("visitor")
    r = await sec.api.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {gkey}"})
    assert r.status_code == 200, r.text
    me = (await sec.api.get("/v1/me", headers={"Authorization": f"Bearer {gkey}"})).json()
    assert me["tier"] == "guest" and me["username"] is None

    ctx = SecureContext(sec.state, Hub(10), 1)
    lease = sec.state.quota.acquire(None, rec)
    job = Job(api_key_id=rec.id, request_id="r1", key_id="k", model="m", started_at=0.0)
    job.relayed, job.usage, job.finished_at = True, (3, 4), 1.0
    _settle(ctx, rec, None, lease)(job)
    names = {row["username"] for row in sec.state.store.recent_usage(None)}
    assert names == {f"guest:{rec.id}"} == {usage_username(None, rec)}


# --------------------------------------------------------------------------- client proxy (6.2-a)
def test_client_proxy_guard():
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import make_mocked_request

    from llmop.client import proxy

    class T:
        def get_extra_info(self, name, default=None):
            return ("127.0.0.1", 8788) if name == "sockname" else default

    def guard(headers, method="POST"):
        req = make_mocked_request(method, "/v1/chat/completions", headers=headers, transport=T())
        err = proxy._guard_error(req)
        return err.status if err else 200

    ok = {"Host": "127.0.0.1:8788", "Content-Type": "application/json"}
    assert guard(ok) == 200
    assert guard({**ok, "Host": "evil.example:8788"}) == 403  # DNS rebinding
    assert guard({**ok, "Origin": "http://127.0.0.1:8788"}) == 403  # any browser page
    assert guard({**ok, "Sec-Fetch-Site": "cross-site"}) == 403
    assert guard({**ok, "Content-Type": "text/plain"}) == 415  # CORS-simple request
    with pytest.raises(ValueError):
        proxy.check_bind_host("0.0.0.0")


# --------------------------------------------------------------------------- (6.2-e) proxy_headers
def test_local_listener_runs_without_proxy_headers():
    """The node starts the local listener with proxy_headers off (peer = real TCP peer)."""
    src = (SRC / "node.py").read_text(encoding="utf-8")
    m = re.search(r'\("local", create_app\(.*?\),\s*cfg\.listeners\.local, (\w+), access\)', src, re.S)
    assert m and m.group(1) == "False"


# --------------------------------------------------------------------------- workers / replay
def test_worker_handshake_throttle():
    """(class 7) 10 failed worker handshakes per source in 5 minutes -> blocked."""
    from llmop.secure.hub import HANDSHAKE_FAILURE_LIMIT, HandshakeThrottle

    now = [1000.0]
    t = HandshakeThrottle(clock=lambda: now[0])
    for _ in range(HANDSHAKE_FAILURE_LIMIT):
        assert not t.blocked("1.2.3.4")
        t.failed("1.2.3.4")
    assert t.blocked("1.2.3.4") and not t.blocked("5.6.7.8")
    now[0] += 301
    assert not t.blocked("1.2.3.4")


def test_worker_replay_guard():
    """(class 8) the worker refuses a repeated (signer, request_id) and stale requests."""
    pytest.importorskip("cryptography")
    from llmop.protocol import REQUEST_TYP, Header, ProtocolError, b64u_encode
    from llmop.worker.replay import ReplayGuard

    now = int(time.time())
    guard = ReplayGuard(lambda: now)
    h = Header(v=1, typ=REQUEST_TYP, request_id=b64u_encode(secrets.token_bytes(16)), ts=now,
               key_id=b64u_encode(b"k" * 16), sig_key_id=b64u_encode(b"s" * 16), model="m",
               max_tokens=10)
    guard.check(h)
    with pytest.raises(ProtocolError):
        guard.check(h)
    with pytest.raises(ProtocolError):
        guard.check(Header(**{**h.__dict__, "request_id": b64u_encode(secrets.token_bytes(16)),
                              "ts": now - 1000}))


# --------------------------------------------------------------------------- static checks
def test_cloudflared_is_started_without_a_shell():
    """(class 1) argv list, never shell=True / os.system."""
    for path in (SRC / "tunnel.py", SRC / "cli.py", SRC / "node.py"):
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text and "os.system(" not in text, path


def test_web_pages_have_no_html_sinks():
    """(class 4) all external strings go through textContent."""
    for js in (SRC / "web" / "static").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert sink not in text, (js.name, sink)


def test_sessions_and_keys_are_stored_hashed(tmp_path):
    """(class 6) only sha256 of session ids and API keys reach the database."""
    store = Store(tmp_path / "db.sqlite3")
    key, _ = store.create_api_key("alice", "k")
    sid, _ = store.create_session("alice", 60)
    store.close()
    raw = (tmp_path / "db.sqlite3").read_bytes()
    for p in tmp_path.glob("db.sqlite3*"):
        raw += p.read_bytes()
    assert key.encode() not in raw and sid.encode() not in raw


def test_portable_build_pins_and_checks_the_python_hash(tmp_path):
    """(class 17, 6.2-g) the downloaded Python archive must match a pinned sha256."""
    spec = importlib.util.spec_from_file_location("build_portable", ROOT / "scripts" / "build_portable.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_portable_tr"] = mod
    spec.loader.exec_module(mod)
    assert re.fullmatch(r"[0-9a-f]{64}", mod.PYTHON_SHA256)
    assert mod.PYTHON_URL.startswith("https://")
    fake = tmp_path / "python.tar.gz"
    fake.write_bytes(b"tampered")
    assert mod.verify_archive(fake, mod.PYTHON_SHA256) is False


def test_readme_documents_image_url_ssrf_as_v03():
    """(class 12, 6.2-f) not filtered in v0.2: the README must say so."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "image_url" in readme and "v0.3" in readme[readme.index("image_url") - 200:
                                                        readme.index("image_url") + 300]


# --------------------------------------------------------------------------- tunnel race (fixed)
def test_tunnel_url_is_visible_only_after_it_is_persisted(tmp_path):
    """T-R fix for the flaky tests/test_tunnel.py::test_ui_and_api_tunnels_are_independent:
    ``url`` used to be set before the store row and the URL file were written."""
    seen = []

    class SpyStore:
        def kv_set(self, k, v):
            seen.append(("kv", mgr.url))

        def kv_get(self, k):
            return None

    mgr = TunnelManager(TunnelSettings(enabled=True), f"http://127.0.0.1:{PORTS['public_api']}",
                        SpyStore(), tmp_path, out=lambda s: None, name="api",
                        on_url=lambda u: seen.append(("cb", (tmp_path / "api_tunnel_url.txt").read_text())))
    mgr._publish("https://x-y.trycloudflare.com")
    assert seen[0] == ("kv", None)
    assert seen[1] == ("cb", "https://x-y.trycloudflare.com\n")
    assert mgr.url == "https://x-y.trycloudflare.com"
