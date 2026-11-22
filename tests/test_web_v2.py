"""Web layer v0.2 (T-8): ``build_web_router(state, surface)``.

* ``surface="public"``: login + portal + own keys; admin data GET-only and masked;
  no admin write route registered at all (enumerated below).
* ``surface="local"``: console; every request must pass ``netguard.is_local`` and
  admin writes need a session created on the console.
* ``surface=None``: the v0.1 combined UI (tests/test_web.py keeps covering it).
"""

from __future__ import annotations

import base64
import re
import time
import warnings

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from llmop import auth, security
from llmop.app import AppState
from llmop.config import ConfigHolder, load_config, parse_config
from llmop.quota import QuotaManager
from llmop.scheduler import Scheduler
from llmop.store import Store, UsageEvent
from llmop.usage import UsageManager
from llmop.web import build_web_router
from llmop.web.routes import CSP, STATIC_DIR

PASSWORDS = {"alice": "alice-pass-123", "bob": "bob-pass-456", "root": "root-pass-789"}
HASHES = {u: security.hash_password(p, n=2**10) for u, p in PASSWORDS.items()}
BACKEND_SECRET = "sk-backend-SECRET-0123456789"
TUNNEL_SECRET = "tunnel-token-SECRET-abcdef"
LOCAL_URL = "http://127.0.0.1:8781"
SIG = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
SIG2 = base64.urlsafe_b64encode(bytes(range(1, 33))).rstrip(b"=").decode()

ALLOWED_PUBLIC_WRITES = {
    ("POST", "/ui/api/login"),
    ("POST", "/ui/api/logout"),
    ("POST", "/ui/api/keys"),
    ("PATCH", "/ui/api/keys/{key_id}"),
    ("DELETE", "/ui/api/keys/{key_id}"),
}


def raw_config(**overrides) -> dict:
    raw = {
        "server": {"session_minutes": 60},
        "model": "qwen3:8b",
        "backends": [{"name": "fake", "type": "openai", "base_url": "http://10.9.8.7:1234",
                      "api_key": BACKEND_SECRET}],
        "tunnel": {"enabled": False, "token": TUNNEL_SECRET},
        "accounts": {
            "alice": {"password_hash": HASHES["alice"], "max_api_keys": 2, "tier": "normal"},
            "bob": {"password_hash": HASHES["bob"], "tier": "vip1"},
            "root": {"password_hash": HASHES["root"], "role": "admin", "tier": "vip3"},
        },
    }
    raw.update(overrides)
    return raw


class FakeRouter:
    async def list_models(self):
        return []

    async def health(self):
        return [{"name": "fake", "type": "openai", "base_url": "http://10.9.8.7:1234",
                 "reachable": False, "model_count": 0, "error": "connect to 10.9.8.7 refused"}]

    async def aclose(self):
        pass


class FakeTunnel:
    url = "https://ui-host.trycloudflare.com"
    status = "connected"
    last_error = "stderr: /home/owner/secret/path"


class FakeApiLink:
    def __init__(self):
        self.gate = "gate-AAAA"
        self.calls = []

    def current_url(self):
        return f"https://api-host.trycloudflare.com/{self.gate}"

    def rotate(self, change_host=False, reason=""):
        self.calls.append((change_host, reason))
        self.gate = "gate-BBBB"


class AsyncApiLink(FakeApiLink):
    async def rotate(self, change_host=False, reason=""):
        self.calls.append((change_host, reason))
        self.gate = "gate-CCCC"


def make_state(cfg=None, **extra) -> AppState:
    cfg = cfg or parse_config(raw_config())
    store = Store(":memory:")
    holder = ConfigHolder(cfg)
    state = AppState(holder=holder, store=store, quota=QuotaManager(store), router=FakeRouter())
    state.usage = UsageManager(store, holder)
    for k, v in extra.items():
        setattr(state, k, v)
    return state


def make_client(state, surface, *, local=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_web_router(state, surface))
    if local is None:
        local = surface == "local"
    if local:
        return TestClient(app, base_url=LOCAL_URL, client=("127.0.0.1", 50123))
    return TestClient(app)


def login(client, username):
    r = client.post("/ui/api/login", json={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def routes_of(router) -> set[tuple[str, str]]:
    out = set()
    for r in router.routes:
        if isinstance(r, APIRoute):
            for m in r.methods:
                out.add((m, r.path))
    return out


@pytest.fixture(autouse=True)
def cheap_dummy_hash(monkeypatch):
    monkeypatch.setattr(security, "_DUMMY_HASH", security.hash_password("x", n=2**10))


# =========================================================================== route tables
def test_surface_must_be_known():
    with pytest.raises(ValueError):
        build_web_router(make_state(), "admin")


def test_public_registers_no_admin_write_route():
    public = routes_of(build_web_router(make_state(), "public"))
    writes = {(m, p) for m, p in public if m not in ("GET", "HEAD")}
    assert writes == ALLOWED_PUBLIC_WRITES
    for m, p in public:
        if "/admin" in p:
            assert m in ("GET", "HEAD"), (m, p)
    paths = {p for _, p in public}
    assert "/ui/api/admin/overview" in paths
    for forbidden in ("/ui/api/admin/config", "/ui/api/playground/chat", "/ui/api/models",
                      "/ui/api/admin/revoke-key", "/ui/api/admin/workers"):
        assert forbidden not in paths


def test_local_and_legacy_route_sets():
    local = routes_of(build_web_router(make_state(), "local"))
    legacy = routes_of(build_web_router(make_state()))
    public = routes_of(build_web_router(make_state(), "public"))
    assert public < local
    for need in [("POST", "/ui/api/admin/guest-keys"), ("POST", "/ui/api/admin/workers"),
                 ("POST", "/ui/api/admin/refill"), ("POST", "/ui/api/admin/api-link/rotate"),
                 ("GET", "/ui/api/admin/config"), ("PUT", "/ui/api/admin/config"),
                 ("POST", "/ui/api/admin/revoke-key")]:
        assert need in local and need in legacy
    # One model, no playground on v0.2 surfaces; v0.1 signature keeps them.
    assert ("POST", "/ui/api/playground/chat") not in local
    assert ("POST", "/ui/api/playground/chat") in legacy
    assert ("GET", "/ui/api/models") in legacy


def _admin_write_paths() -> list[tuple[str, str]]:
    local = routes_of(build_web_router(make_state(), "local"))
    out = []
    for m, p in sorted(local):
        if m in ("GET", "HEAD") or not p.startswith("/ui/api/admin"):
            continue
        out.append((m, p.replace("{worker_id}", "w_x")))
    assert len(out) >= 8
    return out


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_public_admin_writes_are_404_or_405(method):
    state = make_state()
    _, rec = state.store.create_api_key("alice", "a")
    state.store.create_worker("w")
    c = make_client(state, "public")
    csrf = login(c, "root")
    h = {"X-CSRF-Token": csrf}
    paths = {p for _, p in _admin_write_paths()} | {"/ui/api/admin/overview",
                                                     "/ui/api/admin/config"}
    for path in paths:
        r = c.request(method, path, json={"id": rec.id, "username": "alice", "name": "x",
                                          "text": "x", "base_sha256": "x"}, headers=h)
        assert r.status_code in (404, 405), (method, path, r.status_code)
    assert state.store.get_api_key(rec.id).active
    assert all(w.revoked_at is None for w in state.store.list_workers())
    assert len(state.store.list_workers()) == 1 and state.store.list_guest_keys() == []


# =========================================================================== local gate
def test_local_surface_requires_is_local():
    state = make_state()
    ok = make_client(state, "local")
    assert ok.get("/ui/login").status_code == 200
    # cloudflared connects from loopback but always adds forwarding headers.
    for headers in ({"cf-connecting-ip": "203.0.113.9"}, {"x-forwarded-for": "1.2.3.4"},
                    {"host": "evil.example:8781"}, {"via": "1.1 proxy"}):
        r = ok.get("/ui/login", headers=headers)
        assert r.status_code == 403, headers
        r = ok.post("/ui/api/login", json={"username": "root", "password": PASSWORDS["root"]},
                    headers=headers)
        assert r.status_code == 403 and r.json()["error"]["code"] == "local_only"
    remote_peer = TestClient(ok.app, base_url=LOCAL_URL, client=("203.0.113.5", 4000))
    assert remote_peer.get("/ui/api/me").status_code == 403
    # Host must name this listener's own port (DNS rebinding / wrong listener).
    r = ok.get("/ui/login", headers={"host": "127.0.0.1:8780"})
    assert r.status_code == 403 and r.headers["x-frame-options"] == "DENY"
    assert ok.get("/ui/login", headers={"host": "localhost:8781"}).status_code == 200


def test_local_admin_write_needs_session_from_console():
    state = make_state()
    pub = make_client(state, "public")
    csrf = login(pub, "root")
    sid = pub.cookies.get(auth.SESSION_COOKIE)
    assert state.store.get_session(sid).origin == "remote"
    # Same browser, same cookie jar: cookies on 127.0.0.1 are shared across ports.
    loc = make_client(state, "local")
    loc.cookies.set(auth.SESSION_COOKIE, sid)
    assert loc.get("/ui/api/me").json()["can_admin_write"] is False
    r = loc.post("/ui/api/admin/workers", json={"name": "w"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403 and r.json()["error"]["code"] == "local_session_required"
    assert state.store.list_workers() == []

    loc2 = make_client(state, "local")
    csrf2 = login(loc2, "root")
    assert state.store.get_session(loc2.cookies.get(auth.SESSION_COOKIE)).origin == "local"
    assert loc2.get("/ui/api/me").json()["can_admin_write"] is True
    r = loc2.post("/ui/api/admin/workers", json={"name": "w"}, headers={"X-CSRF-Token": csrf2})
    assert r.status_code == 201


def test_local_writes_keep_csrf_and_origin_checks():
    state = make_state()
    c = make_client(state, "local")
    csrf = login(c, "root")
    assert c.post("/ui/api/admin/workers", json={"name": "w"}).status_code == 403
    r = c.post("/ui/api/admin/workers", json={"name": "w"},
               headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/ui/api/admin/workers", json={"name": "w"},
               headers={"X-CSRF-Token": csrf, "Origin": LOCAL_URL})
    assert r.status_code == 201
    alice = make_client(state, "local")
    a = login(alice, "alice")
    assert alice.post("/ui/api/admin/workers", json={"name": "w"},
                      headers={"X-CSRF-Token": a}).status_code == 403


# =========================================================================== portal
def test_portal_shows_tier_tables_keys_link_and_queue():
    link = FakeApiLink()
    sched = Scheduler(slots=2)
    state = make_state(api_link=link, scheduler=sched)
    _, k1 = state.store.create_api_key("alice", "laptop")
    _, k2 = state.store.create_api_key("alice", "phone")
    now = time.time()
    for key, tokens in ((k1, 100), (k1, 50), (k2, 7)):
        state.store.record_usage(UsageEvent(username="alice", endpoint="chat.completions",
                                            model="m", backend="b", prompt_tokens=tokens,
                                            key_id=key.id, ts=now))
    state.store.record_usage(UsageEvent(username="alice", endpoint="chat.completions",
                                        model="m", backend="b", prompt_tokens=1000,
                                        key_id=k1.id, ts=now - 30 * 86400))
    acc = state.config.account("alice")
    state.usage.charge(80_000, acc)  # bucket empty -> LOW
    state.store.set_api_link("hash", now=now)

    c = make_client(state, "public")
    login(c, "alice")
    p = c.get("/ui/api/portal").json()
    assert p["tier"] == "normal" and p["model"] == "qwen3:8b"
    assert p["status"]["state"] == "LOW"
    assert p["status"]["bucket"]["capacity"] == 80_000
    assert p["status"]["weekly"]["limit"] == 1_000_000
    usage = {k["name"]: k["usage"] for k in p["keys"]}
    assert usage["laptop"]["all"] == {"requests": 3, "total": 1150}
    assert usage["laptop"]["week"] == {"requests": 2, "total": 150}
    assert usage["phone"]["all"]["total"] == 7
    assert p["api_link"]["url"] == "https://api-host.trycloudflare.com/gate-AAAA"
    assert p["api_link"]["v1_base_url"].endswith("/gate-AAAA/v1")
    assert p["api_link"]["updated_at"] == pytest.approx(now)
    assert p["queue"]["available"] is True and p["queue"]["slots"] == 2
    assert p["queue"]["waiting"] == 0
    assert "accounts" not in p["queue"]  # other accounts' queue entries stay private

    keys = c.get("/ui/api/keys").json()["keys"]
    assert {k["name"] for k in keys} == {"laptop", "phone"}


def test_portal_without_optional_components():
    state = make_state()
    del state.usage
    c = make_client(state, "public")
    login(c, "bob")
    p = c.get("/ui/api/portal").json()
    assert p["tier"] == "vip1" and p["status"]["bucket"]["capacity"] == 240_000
    assert p["api_link"] == {"available": False, "url": "", "v1_base_url": "",
                             "e2e_base_url": "", "updated_at": None}
    assert p["queue"] == {"available": False}
    c.cookies.clear()
    assert c.get("/ui/api/portal").status_code == 401


def test_me_reports_surface():
    state = make_state()
    c = make_client(state, "public")
    login(c, "root")
    me = c.get("/ui/api/me").json()
    assert me["surface"] == "public" and me["can_admin_write"] is False
    assert me["tier"] == "vip3" and me["playground"] is False


# =========================================================================== own keys
def test_key_encryption_and_signing_key():
    state = make_state()
    c = make_client(state, "public")
    h = {"X-CSRF-Token": login(c, "alice")}
    r = c.post("/ui/api/keys", json={"name": "enc", "encryption": "required"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "sig_public_key_required"
    r = c.post("/ui/api/keys", json={"encryption": "always"}, headers=h)
    assert r.status_code == 400
    for bad in ("not b64u!", "AAAA", SIG + "A", 5):
        r = c.post("/ui/api/keys", json={"sig_public_key": bad}, headers=h)
        assert r.status_code == 400, bad
    r = c.post("/ui/api/keys", json={"name": "enc", "encryption": "required",
                                     "sig_public_key": SIG}, headers=h)
    assert r.status_code == 201
    rec = r.json()["record"]
    assert rec["encryption"] == "required" and rec["sig_public_key"] == SIG
    stored = state.store.get_api_key(rec["id"])
    assert stored.encryption == "required" and stored.sig_public_key == SIG
    assert not auth.key_allows_plaintext(stored)

    url = f"/ui/api/keys/{rec['id']}"
    assert c.patch(url, json={"encryption": "optional"}).status_code == 403  # CSRF
    r = c.patch(url, json={"encryption": "optional"}, headers=h)
    assert r.status_code == 200 and r.json()["record"]["encryption"] == "optional"
    assert c.patch(url, json={"sig_public_key": SIG2}, headers=h).json()["record"][
        "sig_public_key"] == SIG2
    assert c.patch(url, json={"sig_public_key": None}, headers=h).status_code == 200
    r = c.patch(url, json={"encryption": "required"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "sig_public_key_required"
    assert c.patch(url, json={"tier": "vip3"}, headers=h).status_code == 400
    assert c.patch(url, json={}, headers=h).status_code == 400
    assert state.store.get_api_key(rec["id"]).tier is None

    # Somebody else's key, or a revoked one: 404.
    bob = make_client(state, "public")
    hb = {"X-CSRF-Token": login(bob, "bob")}
    assert bob.patch(url, json={"encryption": "optional"}, headers=hb).status_code == 404
    assert c.delete(url, headers=h).status_code == 200
    assert c.patch(url, json={"encryption": "optional"}, headers=h).status_code == 404


# =========================================================================== masking + secrets
def test_public_admin_overview_is_masked():
    state = make_state(tunnel=FakeTunnel(), api_link=FakeApiLink(), scheduler=Scheduler())
    state.holder.last_error = "C:/Users/owner/config.yaml: line 3"
    _, rec = state.store.create_api_key("alice", "laptop", sig_public_key=SIG)
    state.store.record_usage(UsageEvent(username="alice", endpoint="chat.completions",
                                        model="m", backend="fake", prompt_tokens=3,
                                        key_id=rec.id, client_ip="198.51.100.77",
                                        status="error", error="backend http://10.9.8.7 died"))
    w, _ = state.store.create_worker("box")
    state.store.record_worker_keys(w.id, {"kid1": "PUBKEY-xyz"})
    c = make_client(state, "public")
    login(c, "root")
    r = c.get("/ui/api/admin/overview")
    assert r.status_code == 200
    o = r.json()
    text = r.text
    for leak in ("10.9.8.7", "198.51.100.77", "Users/owner", "/home/owner", "gate-AAAA",
                 "PUBKEY-xyz", SIG, "api-host"):
        assert leak not in text, leak
    assert o["config_path"] is None and o["config_error"]
    assert o["can_admin_write"] is False and o["surface"] == "public"
    assert set(o["tunnels"]["ui"]) == {"running", "status", "has_error"}
    assert "base_url" not in o["backends"][0] and "error" not in o["backends"][0]
    assert "client_ip" not in o["recent"][0]
    assert o["api_link"] == {"available": True}
    assert "keys" not in o["workers"][0]

    lo = make_client(state, "local")
    login(lo, "root")
    full = lo.get("/ui/api/admin/overview").json()
    assert full["backends"][0]["base_url"] == "http://10.9.8.7:1234"
    assert full["api_link"]["url"].endswith("gate-AAAA")
    assert full["workers"][0]["keys"][0]["public_key"] == "PUBKEY-xyz"
    assert full["can_admin_write"] is True


def _all_get_paths(router) -> list[str]:
    out = []
    for m, p in routes_of(router):
        if m != "GET" or "{" in p or p == "/ui/api/admin/config":
            continue
        out.append(p)
    return sorted(out)


@pytest.mark.parametrize("surface", ["public", "local"])
def test_secrets_never_in_any_response(surface):
    state = make_state(tunnel=FakeTunnel(), api_link=FakeApiLink(), scheduler=Scheduler())
    alice_key, rec = state.store.create_api_key("alice", "laptop")
    guest_key, _ = state.store.create_guest_key("visitor")
    _, reg_token = state.store.create_worker("box")
    worker2, reg2 = state.store.create_worker("box2")
    _, worker_token = state.store.register_worker(reg2)
    state.store.record_usage(UsageEvent(username="alice", endpoint="chat.completions",
                                        model="m", backend="fake", prompt_tokens=3,
                                        key_id=rec.id))
    c = make_client(state, surface)
    login(c, "root")
    sid = c.cookies.get(auth.SESSION_COOKIE)
    secrets = [alice_key, guest_key, reg_token, reg2, worker_token, sid, BACKEND_SECRET,
               TUNNEL_SECRET, security.sha256_hex(alice_key), security.sha256_hex(sid),
               *HASHES.values(), *PASSWORDS.values()]
    paths = _all_get_paths(build_web_router(state, surface))
    assert "/ui/api/admin/overview" in paths and "/ui/api/portal" in paths
    bodies = []
    for path in paths + ["/ui/static/admin.js", "/ui/static/dashboard.js"]:
        r = c.get(path, follow_redirects=False)
        bodies.append(r.text + str(dict(r.headers)))
    alice = make_client(state, surface)
    login(alice, "alice")
    for path in ("/ui/api/portal", "/ui/api/keys", "/ui/api/usage", "/ui/api/me"):
        bodies.append(alice.get(path).text)
    joined = "\n".join(bodies)
    for s in secrets:
        assert s not in joined


# =========================================================================== local console actions
def test_guest_keys_issue_list_revoke():
    state = make_state()
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    r = c.post("/ui/api/admin/guest-keys", json={"label": "event", "budget_tokens": 1234,
                                                 "expires_days": 3, "reset": True}, headers=h)
    assert r.status_code == 201
    data = r.json()
    key = data["key"]
    assert key.startswith("sk-llop-")
    rec = state.store.get_api_key(data["record"]["id"])
    assert rec.username is None and rec.tier == "guest" and rec.budget_tokens == 1234
    assert rec.reset is True and rec.label == "event"
    assert c.post("/ui/api/admin/guest-keys", json={"encryption": "required"},
                  headers=h).status_code == 400
    assert c.post("/ui/api/admin/guest-keys", json={"reset": "yes"}, headers=h).status_code == 400
    assert c.post("/ui/api/admin/guest-keys", json={"budget_tokens": 0},
                  headers=h).status_code == 400

    o = c.get("/ui/api/admin/overview")
    assert key not in o.text
    g = o.json()["guest_keys"]
    assert [x["label"] for x in g] == ["event"]
    assert g[0]["status"]["guest"]["budget"] == 1234
    assert o.json()["keys"] == []  # GUEST keys are listed separately

    assert c.post("/ui/api/admin/revoke-key", json={"id": rec.id}, headers=h).status_code == 200
    g = c.get("/ui/api/admin/overview").json()["guest_keys"]
    assert g[0]["active"] is False and g[0]["status"]["guest"]["status"] == "revoked"


def test_workers_create_reissue_revoke():
    class Hub:
        def __init__(self):
            self.workers, self.disconnected = {}, []

        def disconnect(self, wid):
            self.disconnected.append(wid)

    hub = Hub()
    state = make_state(hub=hub)
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    assert c.post("/ui/api/admin/workers", json={"name": "  "}, headers=h).status_code == 400
    assert c.post("/ui/api/admin/workers", json={"name": "x" * 101},
                  headers=h).status_code == 400
    r = c.post("/ui/api/admin/workers", json={"name": "desk"}, headers=h)
    assert r.status_code == 201
    wid, token = r.json()["worker"]["id"], r.json()["reg_token"]
    hub.workers[wid] = object()
    listed = c.get("/ui/api/admin/overview")
    assert token not in listed.text
    assert listed.json()["workers"][0]["online"] is True

    r = c.post(f"/ui/api/admin/workers/{wid}/reg-token", json={}, headers=h)
    assert r.status_code == 200 and r.json()["reg_token"] != token
    new_token = r.json()["reg_token"]
    assert c.post("/ui/api/admin/workers/w_nope/reg-token", json={},
                  headers=h).status_code == 404
    r = c.post(f"/ui/api/admin/workers/{wid}/revoke", json={}, headers=h)
    assert r.status_code == 200 and r.json()["worker"]["revoked_at"]
    assert hub.disconnected == [wid]
    assert c.post(f"/ui/api/admin/workers/{wid}/reg-token", json={},
                  headers=h).status_code == 409
    from llmop.protocol import ProtocolError

    with pytest.raises(ProtocolError):
        state.store.register_worker(new_token)


def test_refill_bucket():
    state = make_state()
    acc = state.config.account("alice")
    state.usage.charge(90_000, acc)
    assert state.usage.status(acc)["state"] == "LOW"
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    r = c.post("/ui/api/admin/refill", json={"username": "alice"}, headers=h)
    assert r.status_code == 200
    st = state.usage.status(acc)
    assert st["state"] == "NORMAL" and st["bucket"]["percent"] == 100
    _, g = state.store.create_guest_key("v")
    state.usage.charge(20_000, None, g)
    assert c.post("/ui/api/admin/refill", json={"key_id": g.id}, headers=h).status_code == 200
    assert state.usage.status(None, g)["bucket"]["percent"] == 100
    assert c.post("/ui/api/admin/refill", json={"username": "nobody"},
                  headers=h).status_code == 404
    _, own = state.store.create_api_key("alice", "a")
    assert c.post("/ui/api/admin/refill", json={"key_id": own.id},
                  headers=h).status_code == 404
    assert c.post("/ui/api/admin/refill", json={}, headers=h).status_code == 400


@pytest.mark.parametrize("cls", [FakeApiLink, AsyncApiLink])
def test_rotate_api_link(cls):
    link = cls()
    state = make_state(api_link=link)
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    r = c.post("/ui/api/admin/api-link/rotate", json={"change_host": True, "reason": "leaked"},
               headers=h)
    assert r.status_code == 200
    assert link.calls == [(True, "leaked")]
    assert r.json()["api_link"]["url"].endswith(link.gate) and link.gate != "gate-AAAA"
    assert c.post("/ui/api/admin/api-link/rotate", json={"change_host": "yes"},
                  headers=h).status_code == 400


def test_rotate_api_link_missing_or_failing():
    state = make_state()
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    r = c.post("/ui/api/admin/api-link/rotate", json={}, headers=h)
    assert r.status_code == 503

    class Broken:
        url = ""

        def rotate(self, **kw):
            raise RuntimeError("cloudflared at /secret/path exploded")

    state.api_link = Broken()
    r = c.post("/ui/api/admin/api-link/rotate", json={}, headers=h)
    assert r.status_code == 502 and "/secret/path" not in r.text


def test_raw_config_editing(tmp_path):
    import yaml

    path = tmp_path / "config.yaml"
    original = "# operator notes\n" + yaml.safe_dump(raw_config(), sort_keys=False)
    # bytes, not write_text: on Windows text mode would write CRLF, and the console
    # rightly returns the file exactly as it is on disk.
    path.write_bytes(original.encode("utf-8"))
    state = make_state(load_config(path))
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    assert c.get("/ui/api/admin/overview").json()["config_editable"] is True
    got = c.get("/ui/api/admin/config").json()
    assert got["text"] == original and got["name"] == "config.yaml"
    sha = got["sha256"]

    bad = original.replace("model: qwen3:8b", "model: [unclosed")
    r = c.put("/ui/api/admin/config", json={"text": bad, "base_sha256": sha}, headers=h)
    assert r.status_code == 400 and path.read_text(encoding="utf-8") == original
    invalid = original.replace("tier: normal", "tier: platinum")
    r = c.put("/ui/api/admin/config", json={"text": invalid, "base_sha256": sha}, headers=h)
    assert r.status_code == 400 and path.read_text(encoding="utf-8") == original
    assert c.put("/ui/api/admin/config", json={"text": original},
                 headers=h).status_code == 400
    r = c.put("/ui/api/admin/config", json={"text": original, "base_sha256": "0" * 64},
              headers=h)
    assert r.status_code == 409
    assert c.put("/ui/api/admin/config", json={"text": original, "base_sha256": sha}
                 ).status_code == 403  # CSRF

    edited = original.replace("model: qwen3:8b", "model: llama3.1:8b")
    r = c.put("/ui/api/admin/config", json={"text": edited, "base_sha256": sha}, headers=h)
    assert r.status_code == 200
    assert path.read_text(encoding="utf-8") == edited
    assert state.config.model == "llama3.1:8b"
    assert c.get("/ui/api/me").json()["model"] == "llama3.1:8b"

    # Not on the public surface, not even for reading.
    pub = make_client(state, "public")
    login(pub, "root")
    assert pub.get("/ui/api/admin/config").status_code == 404
    assert pub.get("/ui/api/admin/overview").json()["config_editable"] is False


def test_config_editing_without_file():
    state = make_state()
    c = make_client(state, "local")
    login(c, "root")
    assert c.get("/ui/api/admin/config").status_code == 404


# =========================================================================== pages / static
@pytest.mark.parametrize("surface", ["public", "local"])
def test_pages_csp_and_no_playground(surface):
    state = make_state()
    c = make_client(state, surface)
    login(c, "root")
    for path in ("/ui/dashboard", "/ui/admin", "/ui/login", "/ui/static/app.css"):
        r = c.get(path, follow_redirects=False)
        assert r.headers["content-security-policy"] == CSP, path
        assert r.headers["x-frame-options"] == "DENY"
    dash = c.get("/ui/dashboard").text
    assert "dashboard.js" in dash and "playground" not in dash.lower()
    assert c.post("/ui/api/playground/chat", json={}).status_code in (404, 405)
    assert c.get("/ui/api/models").status_code == 404


def test_static_has_no_cdn_and_no_inline_code():
    for f in list(STATIC_DIR.glob("*.html")) + list(STATIC_DIR.glob("*.js")):
        text = f.read_text(encoding="utf-8")
        urls = re.findall(r"https?://[^\s'\"]+", text)
        assert all(u.startswith("http://www.w3.org/") for u in urls), (f.name, urls)
        assert "innerHTML" not in text and "outerHTML" not in text, f.name
        assert "insertAdjacentHTML" not in text and "document.write" not in text, f.name
        assert "eval(" not in text and "new Function" not in text, f.name
        if f.suffix == ".html":
            assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), f.name
            assert not re.search(r"\sstyle\s*=", text) and "<style" not in text, f.name
            assert not re.search(r"\son[a-z]+\s*=", text), f.name
    portal = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert "/ui/api/models" not in portal and "playground" not in portal
    admin = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    for endpoint in ("/ui/api/admin/guest-keys", "/ui/api/admin/workers",
                     "/ui/api/admin/refill", "/ui/api/admin/api-link/rotate",
                     "/ui/api/admin/config"):
        assert endpoint in admin


# =========================================================================== LLM backend panel
def _fake_lmstudio(monkeypatch, *, down=False):
    import httpx

    from llmop.backends import probe

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("refused")
        p = request.url.path
        if p == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m-a"}, {"id": "m-b"}]})
        if p == "/api/v0/models":
            return httpx.Response(200, json={"data": [
                {"id": "m-a", "state": "not-loaded"}, {"id": "m-b", "state": "loaded"},
                {"id": "emb", "type": "embeddings", "state": "loaded"}]})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_llm_backend_list_models(monkeypatch):
    _fake_lmstudio(monkeypatch)
    c = make_client(make_state(), "local")
    login(c, "root")
    r = c.get("/ui/api/admin/llm-backend/models", params={"type": "lmstudio", "base_url": "http://localhost:1235/v1/"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_url"] == "http://localhost:1235"
    assert [m["id"] for m in body["models"]] == ["m-b", "m-a"]  # loaded first
    assert body["models"][0]["loaded"] is True and body["models"][1]["loaded"] is False
    assert BACKEND_SECRET not in r.text
    info = c.get("/ui/api/admin/llm-backend").json()
    assert info["type"] == "openai" and "lmstudio" in info["types"]


def test_llm_backend_list_errors(monkeypatch):
    c = make_client(make_state(), "local")
    login(c, "root")
    for params in ({}, {"type": "nope", "base_url": "http://x:1"}, {"type": "ollama", "base_url": "ftp://x"},
                   {"type": "ollama", "base_url": "http://u:p@x:1"}, {"type": "ollama", "base_url": "http://x:1?a=b"}):
        assert c.get("/ui/api/admin/llm-backend/models", params=params).status_code == 400, params
    _fake_lmstudio(monkeypatch, down=True)
    r = c.get("/ui/api/admin/llm-backend/models", params={"type": "lmstudio", "base_url": "http://localhost:1"})
    assert r.status_code == 502 and "連不上" in r.json()["error"]["message"]


def test_llm_backend_not_for_non_admin_or_public(monkeypatch):
    _fake_lmstudio(monkeypatch)
    state = make_state()
    c = make_client(state, "local")
    login(c, "alice")
    assert c.get("/ui/api/admin/llm-backend").status_code == 403
    assert c.get("/ui/api/admin/llm-backend/models", params={"type": "lmstudio", "base_url": "http://a:1"}).status_code == 403
    pub = make_client(state, "public")
    login(pub, "root")
    assert pub.get("/ui/api/admin/llm-backend").status_code == 404
    assert pub.post("/ui/api/admin/llm-backend", json={}).status_code in (404, 405)
    assert ("POST", "/ui/api/admin/llm-backend") not in routes_of(build_web_router(state, "public"))


def _fake_loader(monkeypatch, *, status=200, down=False):
    """Fake Ollama / LM Studio for "載入模型"; returns the list of requests seen."""
    import httpx

    from llmop.backends import probe

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("refused")
        seen.append(request)
        p = request.url.path
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m-a"}, {"name": "m-b"}]})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "m-b", "context_length": 8192}]})
        if p in ("/api/generate", "/v1/chat/completions"):
            return httpx.Response(status, json={"done": True})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def test_llm_backend_load_model(monkeypatch):
    import json as _json

    seen = _fake_loader(monkeypatch)
    state = make_state()
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    body = {"type": "ollama", "base_url": "http://localhost:11434", "model": "m-a"}
    assert c.post("/ui/api/admin/llm-backend/load", json=body).status_code == 403  # CSRF
    r = c.post("/ui/api/admin/llm-backend/load", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "m-a" and r.json()["ok"] is True
    sent = seen[-1]
    assert sent.url.path == "/api/generate"
    assert _json.loads(sent.content) == {"model": "m-a", "prompt": "", "stream": False}
    # LM Studio: a 1-token request (just-in-time loading)
    r = c.post("/ui/api/admin/llm-backend/load", headers=h,
               json={"type": "lmstudio", "base_url": "http://localhost:1234", "model": "m-b"})
    assert r.status_code == 200 and seen[-1].url.path == "/v1/chat/completions"
    assert _json.loads(seen[-1].content)["max_tokens"] == 1
    # vLLM gets its model at start-up: refused with a clear reason, nothing sent
    n = len(seen)
    r = c.post("/ui/api/admin/llm-backend/load", headers=h,
               json={"type": "vllm", "base_url": "http://localhost:8000", "model": "x"})
    assert r.status_code == 400 and "啟動時" in r.json()["error"]["message"] and len(seen) == n
    # the config is not changed by loading
    assert state.config.backends[0].type == "openai"


def test_llm_backend_load_errors_and_access(monkeypatch):
    _fake_loader(monkeypatch, status=404)
    state = make_state()
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    body = {"type": "ollama", "base_url": "http://localhost:11434", "model": "nope"}
    r = c.post("/ui/api/admin/llm-backend/load", json=body, headers=h)
    assert r.status_code == 502 and "沒有這個模型" in r.json()["error"]["message"]
    monkeypatch.undo()  # restore the real AsyncClient before patching it again
    _fake_loader(monkeypatch, down=True)
    r = c.post("/ui/api/admin/llm-backend/load", json=body, headers=h)
    assert r.status_code == 502 and "連不上" in r.json()["error"]["message"]
    user = make_client(state, "local")
    hu = {"X-CSRF-Token": login(user, "alice")}
    assert user.post("/ui/api/admin/llm-backend/load", json=body, headers=hu).status_code == 403
    assert ("POST", "/ui/api/admin/llm-backend/load") not in routes_of(build_web_router(state, "public"))


def test_llm_backend_list_marks_loaded_ollama_models(monkeypatch):
    _fake_loader(monkeypatch)
    c = make_client(make_state(), "local")
    login(c, "root")
    r = c.get("/ui/api/admin/llm-backend/models", params={"type": "ollama", "base_url": "http://localhost:11434"})
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    assert [(m["id"], m["loaded"]) for m in models] == [("m-b", True), ("m-a", False)]
    assert models[0]["context_length"] == 8192


def test_llm_backend_set_writes_config(tmp_path):
    import yaml

    path = tmp_path / "config.yaml"
    original = (
        "# operator notes\nmodel: qwen3:8b   # the one model\n"
        "backends:\n  - name: fake\n    type: openai\n    base_url: http://10.9.8.7:1234  # lan\n"
        "accounts:\n  root:\n    role: admin\n    tier: vip3\n"
        f'    password_hash: "{HASHES["root"]}"\n'
    )
    path.write_bytes(original.encode("utf-8"))
    state = make_state(load_config(path))
    c = make_client(state, "local")
    h = {"X-CSRF-Token": login(c, "root")}
    body = {"type": "lmstudio", "base_url": "http://localhost:1235"}
    assert c.post("/ui/api/admin/llm-backend", json=body).status_code == 403  # CSRF
    assert c.post("/ui/api/admin/llm-backend", json={"type": "bogus", "base_url": "http://a:1"}, headers=h).status_code == 400
    r = c.post("/ui/api/admin/llm-backend", json=body, headers=h)  # no model: keep current
    assert r.status_code == 200, r.text
    assert state.config.backends[0].type == "lmstudio" and state.config.model == "qwen3:8b"
    assert state.config.backends[0].base_url == "http://localhost:1235"
    assert path.read_text(encoding="utf-8").startswith("# operator notes\n")
    r = c.post("/ui/api/admin/llm-backend", json={**body, "model": "qwen3.5-9b@q6_k"}, headers=h)
    assert r.status_code == 200 and r.json()["model"] == "qwen3.5-9b@q6_k"
    assert load_config(path).model == "qwen3.5-9b@q6_k" and state.config.model == "qwen3.5-9b@q6_k"
