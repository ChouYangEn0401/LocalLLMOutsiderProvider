"""``llmop audit``: check from the server itself that nothing is reachable that should not be.

Two commands, both run on the computer that runs ``llmop serve``:

``llmop audit exposure [--public] [--with-key] [--json]``
    1. **listen**: every entrance port and the LLM backend ports are tried on this
       computer's own non-loopback addresses (LAN IPs). The node listens on 127.0.0.1
       only, so the entrances must refuse; a reachable LLM backend port is a warning
       (e.g. LM Studio "serve on local network"). Only connect attempts: nothing is
       opened, nothing is sent.
    2. **config**: the tunnels may only point at the two public listeners, never at the
       local console or an LLM backend.
    3. **probes without credentials** against the public listeners (on 127.0.0.1, and
       with ``--public`` also through the tunnel URLs): wrong / missing gate -> 404,
       ``/v1`` without a key or with a made-up key -> 401, garbage to ``/e2e/v1`` ->
       4xx, admin endpoints of the account website -> 401/403/404, no ``/v1`` on the
       account website.
    4. ``--with-key``: a temporary GUEST key that must use encryption is created in the
       database, then: plain ``/v1`` -> 403 ``encryption_required``; an envelope signed
       with a key the gateway does not know -> 403; after revoking -> 401. The key is
       always revoked at the end (also on errors).

``llmop audit canary [--scan PATH ...] [--fingerprint KEY_ID] [--json]``
    Sends one encrypted (e2e) request whose text contains a random marker through the
    running node (temporary GUEST key, revoked afterwards), then searches the data
    directory, ``$LLMOP_HOME`` and every ``--scan`` path (log files) for the marker.
    It must not be found anywhere: the gateway, the database and the logs only ever see
    ciphertext. (The model's own server - LM Studio, Ollama - may log prompts: the
    computer that runs the model has to decrypt them. That is outside this check.)

Everything here takes its network access as parameters (``Net``, an ``httpx.Client``),
so the tests run with fake sockets and a fake HTTP transport.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx

PASS, FAIL, WARN, SKIP, INFO = "PASS", "FAIL", "WARN", "SKIP", "INFO"
BACKEND_PORTS = (11434, 1234)  # Ollama, LM Studio defaults (checked even if not configured)
CONNECT_TIMEOUT = 1.0
HTTP_TIMEOUT = 10.0
AUDIT_LABEL = "llmop-audit (temporary)"
KEY_TTL_SECONDS = 600  # the temporary key expires by itself even if revoking failed
MAX_SCAN_BYTES = 512 * 1024 * 1024  # skip files larger than this (not logs/data)


# --------------------------------------------------------------------------- report
@dataclass
class Check:
    id: str
    status: str
    title: str
    detail: str = ""


@dataclass
class Report:
    command: str
    checks: list[Check] = field(default_factory=list)

    def add(self, id: str, status: str, title: str, detail: str = "") -> Check:
        check = Check(id, status, title, detail)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failed

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in (PASS, FAIL, WARN, SKIP, INFO)}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def to_json(self) -> str:
        return json.dumps({"command": self.command, "ok": self.ok, "counts": self.counts(),
                           "checks": [asdict(c) for c in self.checks]}, indent=2)

    def render(self) -> str:
        lines = [f"llmop audit {self.command}", ""]
        for c in self.checks:
            lines.append(f"[{c.status}] {c.title}")
            if c.detail:
                lines.append(f"       {c.detail}")
        n = self.counts()
        lines += ["", "Result: " + ", ".join(f"{v} {k}" for k, v in n.items() if v),
                  "OK - nothing exposed that should not be." if self.ok else
                  "NOT OK - fix the FAIL lines before sharing the API link."]
        return "\n".join(lines)


# --------------------------------------------------------------------------- network
class Net:
    """Socket access of the audit (replaced in tests)."""

    def tcp_open(self, host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
        """True when something accepts a TCP connection (closed again at once)."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def local_ips(self) -> list[str]:
        """This computer's own non-loopback IPv4 addresses."""
        found: set[str] = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.add(info[4][0])
        except OSError:
            pass
        # The address of the default route. A UDP "connect" only picks the route; it
        # sends nothing (192.0.2.1 is TEST-NET-1, never used on a real network).
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))
                found.add(s.getsockname()[0])
        except OSError:
            pass
        return sorted(ip for ip in found if _routable(ip))


def _routable(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_unspecified or addr.is_link_local)


def _port_of(url: str) -> int | None:
    try:
        parts = urlsplit(url if "://" in url else "http://" + url)
        return parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None


def _is_loopback_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------- context
def read_gate(cfg: Any, store: Any) -> str:
    """The plaintext gate from ``<data>/api_gate.secret`` if it matches the database,
    else "". Read only: never creates or rotates a gate (unlike ``ApiLink``)."""
    from .apilink import GATE_FILE, _read_gate
    from .netguard import gate_hash

    gate = _read_gate(Path(cfg.data_path) / GATE_FILE)
    row = store.get_api_link() if store is not None else None
    if gate and row and gate_hash(gate) == row.get("current_gate_hash"):
        return gate
    return ""


def loopback_bases(cfg: Any) -> dict[str, str]:
    ls = cfg.listeners
    return {"local": f"http://127.0.0.1:{ls.local}", "ui": f"http://127.0.0.1:{ls.public_ui}",
            "api": f"http://127.0.0.1:{ls.public_api}"}


def public_bases(cfg: Any, store: Any) -> dict[str, str]:
    """The tunnel URLs of the account website and the API host ("" = none known)."""
    def kv(name: str) -> str:
        try:
            got = store.kv_get(name) if store is not None else None
        except Exception:
            got = None
        return (got[0] if got else "") or ""

    ui = cfg.server.public_url or getattr(cfg.tunnels.ui, "public_url", "") or kv("public_url")
    api = getattr(cfg.tunnels.api, "public_url", "") or kv("api_tunnel_url")
    return {"ui": ui.rstrip("/"), "api": api.rstrip("/")}


def _status(http: httpx.Client, method: str, url: str, **kw) -> tuple[int | None, str]:
    """(HTTP status, error code from an OpenAI/LLOP JSON body or ""). None = no answer."""
    try:
        resp = http.request(method, url, **kw)
    except httpx.HTTPError as exc:
        return None, type(exc).__name__
    code = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or "")
    except ValueError:
        pass
    return resp.status_code, code


def _expect(report: Report, id: str, title: str, got: tuple[int | None, str],
            allowed: Iterable[int], code: str | None = None) -> None:
    status, err = got
    allowed = tuple(allowed)
    if status is None:
        report.add(id, WARN, title, f"no answer ({err}); is llmop serve running?")
    elif status in allowed and (code is None or err == code):
        report.add(id, PASS, title, f"HTTP {status}" + (f" {err}" if err else ""))
    else:
        want = "/".join(str(s) for s in allowed) + (f" {code}" if code else "")
        report.add(id, FAIL, title, f"expected {want}, got HTTP {status}" + (f" {err}" if err else ""))


# --------------------------------------------------------------------------- exposure
def check_listen(report: Report, cfg: Any, net: Net) -> None:
    ls = cfg.listeners
    entrances = {"local console": ls.local, "account website": ls.public_ui,
                 "API listener": ls.public_api}
    ips = net.local_ips()
    if not ips:
        report.add("listen.lan", SKIP, "LAN addresses", "this computer has no non-loopback IPv4 address")
    for name, port in entrances.items():
        for ip in ips:
            if net.tcp_open(ip, port):
                report.add(f"listen.{port}.{ip}", FAIL, f"{name} (port {port}) reachable on {ip}",
                           "llmop must listen on 127.0.0.1 only; check listeners / server.host "
                           "and that no other program forwards this port")
            else:
                report.add(f"listen.{port}.{ip}", PASS, f"{name} (port {port}) not reachable on {ip}")
    ports = set(BACKEND_PORTS)
    for b in getattr(cfg, "backends", None) or ():
        if _is_loopback_url(b.base_url):  # a backend on another computer is not ours to judge
            port = _port_of(b.base_url)
            if port:
                ports.add(port)
    for port in sorted(ports):
        for ip in ips:
            if net.tcp_open(ip, port):
                report.add(f"backend.{port}.{ip}", WARN, f"LLM server port {port} reachable on {ip}",
                           "other computers on your network can use the model without llmop; turn "
                           "off 'serve on local network' (LM Studio) / OLLAMA_HOST=0.0.0.0")
            else:
                report.add(f"backend.{port}.{ip}", PASS, f"LLM server port {port} not reachable on {ip}")
    if not net.tcp_open("127.0.0.1", ls.public_api):
        report.add("listen.running", INFO, "llmop serve does not seem to run",
                   f"nothing answers on 127.0.0.1:{ls.public_api}; the HTTP checks below are skipped")


def check_config(report: Report, cfg: Any) -> None:
    from .tunnel import forbidden_tunnel_ports

    ls = cfg.listeners
    forbidden = forbidden_tunnel_ports(cfg)
    bad = sorted({ls.public_ui, ls.public_api} & forbidden)
    if bad:
        report.add("config.tunnel-targets", FAIL, "tunnels point only at the public listeners",
                   f"port(s) {bad} are also the local console or an LLM backend")
    else:
        report.add("config.tunnel-targets", PASS, "tunnels point only at the public listeners",
                   f"ui -> {ls.public_ui}, api -> {ls.public_api}; never {ls.local} (console) "
                   "or a backend")
    host = getattr(cfg.server, "host", "127.0.0.1") or "127.0.0.1"
    try:
        loop = host == "localhost" or ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        loop = False
    if not loop:
        report.add("config.server-host", WARN, "server.host is loopback",
                   f"server.host is {host}: 'llmop serve --port' (v0.1 mode) would listen on "
                   "the network")


def probe_without_credentials(report: Report, http: httpx.Client, bases: dict[str, str],
                              gate: str, where: str) -> None:
    """``where``: "local" (127.0.0.1 listeners) or "public" (tunnel URLs)."""
    api, ui = bases.get("api", ""), bases.get("ui", "")
    if api:
        wrong = secrets.token_urlsafe(32)
        _expect(report, f"{where}.api.root", f"[{where}] API host root -> 404",
                _status(http, "GET", api + "/"), (404,))
        _expect(report, f"{where}.api.wrong-gate", f"[{where}] wrong secret path -> 404",
                _status(http, "GET", f"{api}/{wrong}/v1/models"), (404,))
        if gate:
            g = f"{api}/{gate}"
            _expect(report, f"{where}.api.no-key", f"[{where}] /v1/models without a key -> 401",
                    _status(http, "GET", g + "/v1/models"), (401,))
            fake = "sk-llop-" + secrets.token_urlsafe(24)
            _expect(report, f"{where}.api.fake-key", f"[{where}] /v1/models with a made-up key -> 401",
                    _status(http, "GET", g + "/v1/models", headers={"Authorization": f"Bearer {fake}"}),
                    (401,))
            _expect(report, f"{where}.api.e2e-garbage", f"[{where}] garbage to /e2e/v1 -> 4xx",
                    _status(http, "POST", g + "/e2e/v1/chat/completions", content=b"not json",
                            headers={"Content-Type": "application/json"}), (400, 401, 403))
            _expect(report, f"{where}.api.no-console", f"[{where}] no console on the API host",
                    _status(http, "GET", g + "/ui/api/admin/overview"), (401, 403, 404, 405))
        else:
            report.add(f"{where}.api.gate", SKIP, f"[{where}] checks behind the secret path",
                       "the gate file is missing or does not match the database (start llmop serve once)")
    if ui:
        _expect(report, f"{where}.ui.no-v1", f"[{where}] account website has no /v1",
                _status(http, "GET", ui + "/v1/models"), (404,))
        _expect(report, f"{where}.ui.no-worker", f"[{where}] account website has no /worker/ws",
                _status(http, "GET", ui + "/worker/ws"), (404,))
        _expect(report, f"{where}.ui.admin-read", f"[{where}] admin data without login -> refused",
                _status(http, "GET", ui + "/ui/api/admin/overview"), (401, 403, 404, 405))
        _expect(report, f"{where}.ui.admin-config", f"[{where}] config file not readable",
                _status(http, "GET", ui + "/ui/api/admin/config"), (401, 403, 404, 405))
        _expect(report, f"{where}.ui.admin-write", f"[{where}] admin writes without login -> refused",
                _status(http, "POST", ui + "/ui/api/admin/guest-keys", json={"label": "x"}),
                (401, 403, 404, 405))
        _expect(report, f"{where}.ui.llm-backend", f"[{where}] LLM backend panel not reachable",
                _status(http, "POST", ui + "/ui/api/admin/llm-backend", json={}), (401, 403, 404, 405))


def _new_signing_key():
    from .crypto import generate_signing_key, public_raw
    from .protocol import b64u_encode

    key = generate_signing_key()
    return key, b64u_encode(public_raw(key))


def _temporary_key(store: Any, sig_public_key: str, budget: int = 1) -> tuple[str, Any]:
    return store.create_guest_key(AUDIT_LABEL, budget_tokens=budget, encryption="required",
                                  sig_public_key=sig_public_key,
                                  expires_at=time.time() + KEY_TTL_SECONDS)


def check_with_key(report: Report, http: httpx.Client, store: Any, api: str, gate: str) -> None:
    if not gate:
        report.add("key.gate", SKIP, "checks with a temporary key", "the secret path is unknown")
        return
    try:
        signing_key, sig_pub = _new_signing_key()
        from .crypto import generate_worker_key, public_raw, seal_request
    except ImportError:
        report.add("key.secure", SKIP, "checks with a temporary key",
                   'needs the secure extra: pip install "localllm-outsider-provider[secure]"')
        return
    plaintext, rec = _temporary_key(store, sig_pub)
    g = f"{api}/{gate}"
    auth = {"Authorization": f"Bearer {plaintext}"}
    try:
        report.add("key.created", INFO, "temporary GUEST key created (encryption: required)",
                   f"id {rec.id}; revoked at the end of this check")
        _expect(report, "key.plain-refused", "encryption-required key on plain /v1 -> 403",
                _status(http, "GET", g + "/v1/models", headers=auth), (403,), "encryption_required")
        # An envelope signed with a key the gateway does not know (not this key's).
        stranger, _ = _new_signing_key()
        envelope, _ = seal_request({"messages": [{"role": "user", "content": "audit"}]},
                                   model="audit", max_tokens=1,
                                   worker_public=public_raw(generate_worker_key()),
                                   signing_key=stranger)
        _expect(report, "key.unsigned-refused", "e2e request without this key's signature -> 403",
                _status(http, "POST", g + "/e2e/v1/chat/completions", headers=auth,
                        json=envelope.to_json()), (401, 403), "bad_signature")
    finally:
        revoked = store.revoke_api_key(rec.id)
    if revoked:
        _expect(report, "key.revoked", "revoked key -> 401",
                _status(http, "GET", g + "/v1/models", headers=auth), (401,))
    else:
        report.add("key.revoked", FAIL, "temporary key revoked",
                   f"could not revoke {rec.id}; run: llmop revoke-key {rec.id}")


def exposure(cfg: Any, store: Any, *, http: httpx.Client, net: Net | None = None,
             public: bool = False, with_key: bool = False) -> Report:
    net = net or Net()
    report = Report("exposure")
    check_listen(report, cfg, net)
    check_config(report, cfg)
    gate = read_gate(cfg, store)
    running = net.tcp_open("127.0.0.1", cfg.listeners.public_api)
    local = loopback_bases(cfg)
    if running:
        probe_without_credentials(report, http, {"api": local["api"], "ui": local["ui"]}, gate, "local")
    if public:
        pub = public_bases(cfg, store)
        if not pub["api"] and not pub["ui"]:
            report.add("public.urls", SKIP, "checks through the tunnels",
                       "no tunnel URL is known (start llmop serve --tunnel)")
        else:
            for name in ("ui", "api"):
                if not pub[name]:
                    report.add(f"public.{name}", SKIP, f"[public] {name} tunnel", "no URL known")
            if pub["ui"] and pub["api"] and urlsplit(pub["ui"]).hostname == urlsplit(pub["api"]).hostname:
                report.add("public.separate-hosts", WARN, "account website and API use different hosts",
                           "both tunnels have the same host name")
            probe_without_credentials(report, http, pub, gate, "public")
    if with_key:
        tunnel_api = public_bases(cfg, store)["api"] if public and store is not None else ""
        if store is None:
            report.add("key.store", SKIP, "checks with a temporary key", "no database yet")
        elif not running and not tunnel_api:
            report.add("key.running", SKIP, "checks with a temporary key", "llmop serve does not run")
        else:
            check_with_key(report, http, store, tunnel_api or local["api"], gate)
    return report


# --------------------------------------------------------------------------- canary
def default_scan_paths(cfg: Any) -> list[Path]:
    home = Path(os.environ.get("LLMOP_HOME") or "~/.llmop").expanduser()
    return [Path(cfg.data_path), home]


def find_marker(paths: Iterable[Path], marker: str) -> tuple[list[Path], int]:
    """Files (recursively) that contain ``marker`` as UTF-8 or UTF-16 text; and how
    many files were read."""
    needles = [marker.encode("utf-8"), marker.encode("utf-16-le")]
    hits: list[Path] = []
    seen: set[Path] = set()
    scanned = 0
    for root in paths:
        root = Path(root)
        files = [root] if root.is_file() else (p for p in root.rglob("*") if p.is_file()) \
            if root.is_dir() else []
        for f in files:
            try:
                key = f.resolve()
                if key in seen or f.stat().st_size > MAX_SCAN_BYTES:
                    continue
                seen.add(key)
                data = f.read_bytes()
            except OSError:
                continue
            scanned += 1
            if any(n in data for n in needles):
                hits.append(f)
    return hits, scanned


def local_worker_fingerprint() -> str:
    """key_id of this computer's worker (``$LLMOP_HOME/worker/keys.json``), or ""."""
    path = Path(os.environ.get("LLMOP_HOME") or "~/.llmop").expanduser() / "worker" / "keys.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("current") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def canary(cfg: Any, store: Any, *, http: httpx.Client, scan: Iterable[Path] = (),
           fingerprint: str = "", timeout: float = 120.0, marker: str = "",
           sleep: Callable[[float], None] = time.sleep) -> Report:
    report = Report("canary")
    marker = marker or "llmop-canary-" + secrets.token_hex(12)
    gate = read_gate(cfg, store) if store is not None else ""
    if not gate:
        report.add("canary.gate", FAIL, "send an encrypted test request",
                   "the secret path is unknown: start llmop serve first")
        return report
    try:
        from .crypto import seal_request, worker_key_id
        from .protocol import b64u_decode
        signing_key, sig_pub = _new_signing_key()
    except ImportError:
        report.add("canary.secure", FAIL, "send an encrypted test request",
                   'needs the secure extra: pip install "localllm-outsider-provider[secure]"')
        return report
    fingerprint = fingerprint or local_worker_fingerprint()
    base = f"{loopback_bases(cfg)['api']}/{gate}/e2e/v1"
    plaintext, rec = _temporary_key(store, sig_pub, budget=64)
    auth = {"Authorization": f"Bearer {plaintext}"}
    sent = False
    try:
        entries: list = []
        try:
            resp = http.get(base + "/models", headers=auth)
            if resp.status_code == 200:
                entries = resp.json().get("data") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            entries = []
        worker = None
        for e in entries:
            meta = e.get("llmop") if isinstance(e, dict) else None
            if not isinstance(meta, dict) or (fingerprint and meta.get("key_id") != fingerprint):
                continue
            try:
                pub = b64u_decode(meta.get("public_key") or "")
                if worker_key_id(pub) == meta.get("key_id"):
                    worker = (str(e.get("id")), pub)
                    break
            except Exception:
                continue
        if worker is None:
            why = f"no online worker with key_id {fingerprint}" if fingerprint else "no online worker"
            report.add("canary.worker", FAIL, "send an encrypted test request",
                       f"{why} (llmop worker init, roles: [gateway, worker], or --fingerprint)")
        else:
            model, pub = worker
            body = {"messages": [{"role": "user", "content": f"Reply with OK. {marker}"}]}
            envelope, _opener = seal_request(body, model=model, max_tokens=8, worker_public=pub,
                                             signing_key=signing_key)
            rid = envelope.header.request_id
            try:
                r = http.post(base + "/chat/completions", headers=auth, json=envelope.to_json())
                status = r.status_code
                deadline = time.monotonic() + timeout
                while status == 202 and time.monotonic() < deadline:
                    sleep(0.5)
                    status = http.get(f"{base}/requests/{rid}", headers=auth).status_code
                sent = True
                state = "answered" if status == 200 else f"HTTP {status}"
                report.add("canary.sent", PASS if status == 200 else WARN,
                           "encrypted test request went through the gateway",
                           f"model {model}, request {rid}: {state} (the reply is not shown)")
            except httpx.HTTPError as exc:
                report.add("canary.sent", FAIL, "send an encrypted test request",
                           f"no answer from 127.0.0.1 ({type(exc).__name__}); is llmop serve running?")
    finally:
        store.revoke_api_key(rec.id)
    if not sent:
        return report
    paths = [*default_scan_paths(cfg), *[Path(p) for p in scan]]
    hits, scanned = find_marker(paths, marker)
    if hits:
        report.add("canary.found", FAIL, "test text not found in data and logs",
                   "found in: " + ", ".join(str(h) for h in hits))
    else:
        report.add("canary.found", PASS, "test text not found in data and logs",
                   f"{scanned} file(s) searched in: " + ", ".join(str(p) for p in paths))
    if not list(scan):
        report.add("canary.logs", INFO, "log files",
                   "llmop writes its log to the console; to check a saved log too, run with "
                   "--scan <log file>")
    return report
