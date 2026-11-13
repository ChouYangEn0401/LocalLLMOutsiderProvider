"""Web UI layer tests (llmop.web): login/session/CSRF/throttle, keys, usage, admin,
pages, static files and the playground. Independent of the API layer: the backend
router is a fake object and ``llmop.api.proxy`` is replaced by a stub module."""

from __future__ import annotations

import re
import sys
import time
import types
import warnings
from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from llmop import auth, security
from llmop.app import AppState
from llmop.config import ConfigHolder, parse_config
from llmop.quota import QuotaManager
from llmop.store import Store, UsageEvent
from llmop.web import LoginThrottle, build_web_router
from llmop.web.routes import STATIC_DIR

PASSWORDS = {
    "alice": "alice-pass-123",
    "bob": "bob-pass-456",
    "root": "root-pass-789",
    "ghost": "ghost-pass-000",
}
# Low scrypt cost keeps the suite fast; verify_password reads n from the hash.
HASHES = {u: security.hash_password(p, n=2**10) for u, p in PASSWORDS.items()}


def make_config(server: dict | None = None, **overrides):
    raw = {
        "server": {"session_minutes": 60, **(server or {})},
        "backends": [{"name": "fake", "type": "openai", "base_url": "http://fake"}],
        "default_quota": {
            "tokens_per_day": 1000,
            "tokens_per_month": 20000,
            "requests_per_minute": 5,
            "max_concurrent": 2,
            "max_tokens_per_request": 256,
        },
        "accounts": {
            "alice": {"password_hash": HASHES["alice"], "models": ["llama*", "qwen3:8b"],
                      "max_api_keys": 2, "display_name": "Alice"},
            "bob": {"password_hash": HASHES["bob"]},
            "root": {"password_hash": HASHES["root"], "role": "admin",
                     "quota": {"tokens_per_day": 0, "tokens_per_month": 0}},
            "ghost": {"password_hash": HASHES["ghost"], "disabled": True},
        },
    }
    raw.update(overrides)
    return parse_config(raw)


@dataclass
class FakeModel:
    id: str
    backend: str = "fake"
    owned_by: str = "fake"
    context_length: int | None = 8192
    created: int | None = None


class FakeRouter:
    def __init__(self):
        self.models = [FakeModel("llama3.1:8b"), FakeModel("qwen3:8b"), FakeModel("mistral:7b")]

    async def list_models(self):
        return list(self.models)

    async def health(self):
        return [{"name": "fake", "type": "openai", "base_url": "http://fake", "reachable": True,
                 "model_count": len(self.models), "error": None}]

    async def aclose(self):
        pass


class FakeTunnel:
    url = "https://abc-def.trycloudflare.com"
    status = "connected"
    last_error = None


def make_state(cfg=None) -> AppState:
    cfg = cfg or make_config()
    store = Store(":memory:")
    return AppState(holder=ConfigHolder(cfg), store=store, quota=QuotaManager(store),
                    router=FakeRouter())


def make_client(state: AppState, throttle: LoginThrottle | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(build_web_router(state, throttle=throttle))
    return TestClient(app)


@pytest.fixture(autouse=True)
def cheap_dummy_hash(monkeypatch):
    monkeypatch.setattr(security, "_DUMMY_HASH", security.hash_password("x", n=2**10))


@pytest.fixture
def state():
    return make_state()


@pytest.fixture
def client(state):
    return make_client(state)


def login(client: TestClient, username: str, password: str | None = None, **kw):
    return client.post("/ui/api/login",
                       json={"username": username, "password": password or PASSWORDS[username]},
                       **kw)


def login_csrf(client: TestClient, username: str) -> str:
    r = login(client, username)
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def session_id(client: TestClient) -> str:
    return client.cookies.get(auth.SESSION_COOKIE)


# =========================================================================== login
def test_login_success_sets_cookie_and_returns_csrf(client, state):
    r = login(client, "alice")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["username"] == "alice" and data["role"] == "user"
    assert data["csrf"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.SESSION_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "samesite=lax" in cookie.lower()
    assert "Path=/" in cookie
    assert "Max-Age=3600" in cookie
    assert "Secure" not in cookie  # plain http + cookie_secure: auto
    sid = session_id(client)
    sess = state.store.get_session(sid)
    assert sess is not None and sess.username == "alice" and sess.csrf == data["csrf"]

    me = client.get("/ui/api/me").json()
    assert me["username"] == "alice" and me["display_name"] == "Alice"
    assert me["csrf"] == data["csrf"]
    assert me["models"] == ["llama*", "qwen3:8b"]
    assert me["max_api_keys"] == 2
    assert me["session_expires_at"] > time.time() + 3500
    assert "public_url" in me


def test_login_wrong_password_generic_message(client):
    r = login(client, "alice", "nope")
    assert r.status_code == 401
    assert "帳號或密碼錯誤" in r.json()["error"]["message"]
    assert auth.SESSION_COOKIE not in r.headers.get("set-cookie", "")


def test_login_unknown_user_runs_dummy_verify(client, monkeypatch):
    calls = []
    real = security.dummy_verify
    monkeypatch.setattr(security, "dummy_verify", lambda pw: calls.append(pw) or real(pw))
    r = login(client, "nobody", "whatever")
    assert r.status_code == 401
    assert r.json()["error"]["message"] == login(client, "alice", "bad").json()["error"]["message"]
    assert calls == ["whatever"]


def test_login_disabled_account(client):
    r = login(client, "ghost")
    assert r.status_code == 403
    assert session_id(client) is None


def test_login_requires_json(client):
    r = client.post("/ui/api/login", data={"username": "alice", "password": PASSWORDS["alice"]})
    assert r.status_code == 415
    r = client.post("/ui/api/login", json={"username": "alice"})
    assert r.status_code == 400


def test_login_cookie_secure_always():
    c = make_client(make_state(make_config({"cookie_secure": "always"})))
    r = login(c, "alice")
    assert r.status_code == 200
    assert "Secure" in r.headers["set-cookie"]


def test_login_cookie_secure_auto_behind_https_proxy(client):
    r = login(client, "alice", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]


def test_login_cookie_secure_never():
    c = make_client(make_state(make_config({"cookie_secure": "never"})))
    r = login(c, "alice", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in r.headers["set-cookie"]


def test_login_rotates_session(client, state):
    login_csrf(client, "alice")
    old = session_id(client)
    login_csrf(client, "alice")
    new = session_id(client)
    assert new and new != old
    assert state.store.get_session(old) is None
    assert state.store.get_session(new) is not None


def test_login_origin_mismatch_rejected(client):
    r = login(client, "alice", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert login(client, "alice", headers={"Origin": "http://testserver"}).status_code == 200


def test_login_throttle_per_ip(state):
    c = make_client(state)
    for i in range(10):
        assert login(c, f"user{i}", "bad").status_code == 401
    r = login(c, "alice")  # correct password, but the IP is locked out
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "次數過多" in r.json()["error"]["message"]


def test_login_throttle_per_username():
    cfg = make_config({"trusted_proxies": ["testclient"]})
    c = make_client(make_state(cfg))
    for i in range(10):
        r = login(c, "alice", "bad", headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.status_code == 401
    r = login(c, "alice", headers={"X-Forwarded-For": "10.0.1.1"})
    assert r.status_code == 429
    # Another account from a fresh IP is unaffected.
    assert login(c, "bob", headers={"X-Forwarded-For": "10.0.1.2"}).status_code == 200


def test_login_throttle_window_expires_and_success_forgives():
    now = [1000.0]
    throttle = LoginThrottle(max_failures=3, window=60, clock=lambda: now[0])
    c = make_client(make_state(make_config({"trusted_proxies": ["testclient"]})),
                    throttle=throttle)

    def attempt(user, pw, ip):
        return login(c, user, pw, headers={"X-Forwarded-For": ip}).status_code

    assert attempt("alice", "bad", "10.0.0.1") == 401
    assert attempt("alice", "bad", "10.0.0.2") == 401
    # A success clears the username counter (but not other IPs' failures).
    assert attempt("alice", PASSWORDS["alice"], "10.0.0.3") == 200
    assert attempt("alice", "bad", "10.0.0.4") == 401
    assert attempt("alice", "bad", "10.0.0.5") == 401
    assert attempt("alice", PASSWORDS["alice"], "10.0.0.6") == 200

    # Per-IP: failures for any username add up; the window then expires.
    for user in ("bob", "x", "y"):
        assert attempt(user, "bad", "10.9.9.9") == 401
    assert attempt("root", PASSWORDS["root"], "10.9.9.9") == 429
    now[0] += 61
    assert attempt("root", PASSWORDS["root"], "10.9.9.9") == 200


# =========================================================================== session
def test_session_expiry(client, state):
    login_csrf(client, "alice")
    assert client.get("/ui/api/me").status_code == 200
    state.store._exec("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
    r = client.get("/ui/api/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "not_logged_in"
    r = client.get("/ui/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login"


def test_logout(client, state):
    csrf = login_csrf(client, "alice")
    sid = session_id(client)
    assert client.post("/ui/api/logout").status_code == 403  # CSRF required
    r = client.post("/ui/api/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert state.store.get_session(sid) is None
    assert 'Max-Age=0' in r.headers["set-cookie"] or "expires=" in r.headers["set-cookie"].lower()
    assert client.get("/ui/api/me").status_code == 401


def test_unauthenticated_api_is_401_json(client):
    for path in ("/ui/api/me", "/ui/api/usage", "/ui/api/keys", "/ui/api/models",
                 "/ui/api/admin/overview"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["content-security-policy"].startswith("default-src 'self'")
    r = client.post("/ui/api/keys", json={"name": "x"})
    assert r.status_code == 401


def test_api_key_is_not_accepted_by_ui(client, state):
    key, _ = state.store.create_api_key("alice", "k")
    r = client.get("/ui/api/me", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


# =========================================================================== CSRF / Origin
def test_csrf_missing_or_wrong(client):
    csrf = login_csrf(client, "alice")
    assert client.post("/ui/api/keys", json={"name": "a"}).status_code == 403
    r = client.post("/ui/api/keys", json={"name": "a"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"
    r = client.post("/ui/api/keys", json={"name": "a"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201


def test_origin_checks(client):
    csrf = login_csrf(client, "alice")
    h = {"X-CSRF-Token": csrf}
    r = client.post("/ui/api/keys", json={"name": "a"}, headers={**h, "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "origin_mismatch"
    r = client.post("/ui/api/keys", json={"name": "a"}, headers={**h, "Origin": "null"})
    assert r.status_code == 403
    r = client.post("/ui/api/keys", json={"name": "a"},
                    headers={**h, "Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/ui/api/keys", json={"name": "a"}, headers={**h, "Origin": "http://testserver"})
    assert r.status_code == 201
    # Behind cloudflared / a reverse proxy: the browser origin is the public host.
    r = client.post("/ui/api/keys", json={"name": "b"},
                    headers={**h, "Origin": "https://abc.trycloudflare.com",
                             "X-Forwarded-Host": "abc.trycloudflare.com"})
    assert r.status_code == 201


def test_origin_matches_public_url(state):
    state.tunnel = FakeTunnel()
    c = make_client(state)
    csrf = login_csrf(c, "root")
    r = c.post("/ui/api/admin/logout-user", json={"username": "bob"},
               headers={"X-CSRF-Token": csrf, "Origin": FakeTunnel.url})
    assert r.status_code == 200


def test_delete_requires_csrf(client, state):
    csrf = login_csrf(client, "alice")
    kid = client.post("/ui/api/keys", json={"name": "a"},
                      headers={"X-CSRF-Token": csrf}).json()["record"]["id"]
    assert client.delete(f"/ui/api/keys/{kid}").status_code == 403
    assert state.store.count_active_keys("alice") == 1


# =========================================================================== API keys
def test_key_create_shows_once_and_list_shows_display_only(client, state):
    csrf = login_csrf(client, "alice")
    r = client.post("/ui/api/keys", json={"name": "laptop", "expires_days": 30},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201
    data = r.json()
    key = data["key"]
    assert key.startswith(security.API_KEY_PREFIX)
    rec = data["record"]
    assert rec["name"] == "laptop" and rec["display"] == f"sk-llop-...{key[-4:]}"
    assert 29 * 86400 < rec["expires_at"] - time.time() <= 30 * 86400
    found = state.store.find_api_key(key)
    assert found is not None and found.username == "alice"

    r = client.get("/ui/api/keys")
    assert r.status_code == 200
    assert key not in r.text
    body = r.json()
    assert body["active_count"] == 1 and body["max_api_keys"] == 2
    assert body["keys"][0]["display"] == rec["display"]
    assert body["keys"][0]["active"] is True


def test_key_validation(client):
    csrf = login_csrf(client, "alice")
    h = {"X-CSRF-Token": csrf}
    assert client.post("/ui/api/keys", json={"name": "x" * 61}, headers=h).status_code == 400
    assert client.post("/ui/api/keys", json={"name": 5}, headers=h).status_code == 400
    assert client.post("/ui/api/keys", json={"expires_days": 0}, headers=h).status_code == 400
    assert client.post("/ui/api/keys", json={"expires_days": "abc"}, headers=h).status_code == 400
    r = client.post("/ui/api/keys", json={}, headers=h)  # default name
    assert r.status_code == 201 and r.json()["record"]["name"].startswith("key-")


def test_max_api_keys_enforced(client):
    csrf = login_csrf(client, "alice")
    h = {"X-CSRF-Token": csrf}
    ids = [client.post("/ui/api/keys", json={"name": f"k{i}"}, headers=h).json()["record"]["id"]
           for i in range(2)]
    r = client.post("/ui/api/keys", json={"name": "k3"}, headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "max_api_keys"
    assert client.delete(f"/ui/api/keys/{ids[0]}", headers=h).status_code == 200
    assert client.post("/ui/api/keys", json={"name": "k3"}, headers=h).status_code == 201


def test_max_api_keys_zero_disables_keys():
    cfg = make_config()
    cfg.accounts["bob"].max_api_keys = 0
    c = make_client(make_state(cfg))
    csrf = login_csrf(c, "bob")
    r = c.post("/ui/api/keys", json={"name": "x"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403


def test_revoke_own_key_only(state):
    alice, bob = make_client(state), make_client(state)
    a_csrf, b_csrf = login_csrf(alice, "alice"), login_csrf(bob, "bob")
    kid = alice.post("/ui/api/keys", json={"name": "a"},
                     headers={"X-CSRF-Token": a_csrf}).json()["record"]["id"]
    r = bob.delete(f"/ui/api/keys/{kid}", headers={"X-CSRF-Token": b_csrf})
    assert r.status_code == 404
    assert state.store.count_active_keys("alice") == 1
    r = alice.delete(f"/ui/api/keys/{kid}", headers={"X-CSRF-Token": a_csrf})
    assert r.status_code == 200
    assert state.store.count_active_keys("alice") == 0
    assert alice.get("/ui/api/keys").json()["keys"] == []
    assert alice.delete(f"/ui/api/keys/{kid}", headers={"X-CSRF-Token": a_csrf}).status_code == 404


# =========================================================================== usage
def test_usage_shape(client, state):
    key, rec = state.store.create_api_key("alice", "laptop")
    now = time.time()
    state.store.record_usage(UsageEvent(username="alice", endpoint="chat.completions",
                                        model="llama3.1:8b", backend="fake", prompt_tokens=10,
                                        completion_tokens=20, key_id=rec.id, ts=now))
    state.store.record_usage(UsageEvent(username="alice", endpoint="chat.completions",
                                        model="qwen3:8b", backend="fake", prompt_tokens=5,
                                        completion_tokens=5, usage_source="estimate",
                                        ts=now - 3 * 86400))
    state.store.record_usage(UsageEvent(username="bob", endpoint="chat.completions",
                                        model="llama3.1:8b", backend="fake", prompt_tokens=99,
                                        completion_tokens=1, ts=now))
    login_csrf(client, "alice")
    r = client.get("/ui/api/usage?days=7")
    assert r.status_code == 200
    u = r.json()
    assert set(u) >= {"quota", "by_day", "by_model", "recent", "summary", "days"}
    assert u["quota"]["tokens_today"] == 30
    assert u["quota"]["limits"]["tokens_per_day"] == 1000
    assert len(u["by_day"]) == 7
    assert u["by_day"][-1]["day"] == date.today().isoformat()
    assert u["by_day"][-1]["total"] == 30
    three = (date.today() - timedelta(days=3)).isoformat()
    assert next(d for d in u["by_day"] if d["day"] == three)["total"] == 10
    assert sum(d["total"] for d in u["by_day"]) == 40
    assert {m["model"]: m["total"] for m in u["by_model"]} == {"llama3.1:8b": 30, "qwen3:8b": 10}
    assert u["summary"]["total"] == 40
    assert len(u["recent"]) == 2  # bob's request is not visible
    newest, older = u["recent"]
    assert newest["via"] == "key" and "laptop" in newest["via_label"]
    assert older["via"] == "playground" and older["usage_source"] == "estimate"
    assert "username" not in newest

    assert client.get("/ui/api/usage?days=abc").status_code == 400
    assert len(client.get("/ui/api/usage?days=9999").json()["by_day"]) == 366
    assert len(client.get("/ui/api/usage").json()["by_day"]) == 30


# =========================================================================== models / playground
def test_models_filtered_by_account_patterns(client):
    login_csrf(client, "alice")
    ids = [m["id"] for m in client.get("/ui/api/models").json()["data"]]
    assert ids == ["llama3.1:8b", "qwen3:8b"]


def test_models_all_for_star(client):
    login_csrf(client, "bob")
    data = client.get("/ui/api/models").json()["data"]
    assert [m["id"] for m in data] == ["llama3.1:8b", "qwen3:8b", "mistral:7b"]
    assert data[0] == {"id": "llama3.1:8b", "owned_by": "fake",
                       "context_length": 8192}


def test_models_router_failure(client, state):
    async def boom():
        raise RuntimeError("backend down")
    state.router.list_models = boom
    login_csrf(client, "bob")
    r = client.get("/ui/api/models")
    assert r.status_code == 200
    assert r.json()["data"] == [] and "backend down" in r.json()["error"]


@pytest.fixture
def forward_stub(monkeypatch):
    calls = []

    async def forward(state, endpoint, body, *, account, key_id, client_ip):
        calls.append({"state": state, "endpoint": endpoint, "body": body, "account": account,
                      "key_id": key_id, "client_ip": client_ip})
        return JSONResponse({
            "id": "chatcmpl-1", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi there"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    mod = types.ModuleType("llmop.api.proxy")
    mod.forward = forward
    monkeypatch.setitem(sys.modules, "llmop.api.proxy", mod)
    return calls


def test_playground_calls_forward(client, state, forward_stub):
    csrf = login_csrf(client, "alice")
    r = client.post("/ui/api/playground/chat",
                    json={"model": "llama3.1:8b", "prompt": "hello", "max_tokens": 50,
                          "stream": True},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hi there"
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert len(forward_stub) == 1
    call = forward_stub[0]
    assert call["endpoint"] == "chat.completions"
    assert call["key_id"] is None
    assert call["state"] is state
    assert call["account"].username == "alice"
    assert call["client_ip"] == "testclient"
    assert call["body"]["stream"] is False
    assert call["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert call["body"]["max_tokens"] == 50


def test_playground_messages_and_validation(client, forward_stub):
    csrf = login_csrf(client, "alice")
    h = {"X-CSRF-Token": csrf}
    msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"}, {"role": "user", "content": "again"}]
    r = client.post("/ui/api/playground/chat", json={"model": "qwen3:8b", "messages": msgs}, headers=h)
    assert r.status_code == 200
    assert forward_stub[-1]["body"]["messages"] == msgs
    assert "max_tokens" not in forward_stub[-1]["body"]
    assert client.post("/ui/api/playground/chat", json={"prompt": "x"}, headers=h).status_code == 400
    assert client.post("/ui/api/playground/chat", json={"model": "m"}, headers=h).status_code == 400
    bad = {"model": "m", "messages": [{"role": "tool", "content": "x"}]}
    assert client.post("/ui/api/playground/chat", json=bad, headers=h).status_code == 400
    assert client.post("/ui/api/playground/chat", json={"model": "m", "prompt": "x"}).status_code == 403
    assert len(forward_stub) == 1


# =========================================================================== admin
def test_admin_endpoints_forbidden_for_user(client, state):
    csrf = login_csrf(client, "bob")
    h = {"X-CSRF-Token": csrf}
    assert client.get("/ui/api/admin/overview").status_code == 403
    _, rec = state.store.create_api_key("alice", "a")
    r = client.post("/ui/api/admin/revoke-key", json={"id": rec.id}, headers=h)
    assert r.status_code == 403
    assert state.store.count_active_keys("alice") == 1
    assert client.post("/ui/api/admin/logout-user", json={"username": "alice"},
                       headers=h).status_code == 403
    r = client.get("/ui/admin", follow_redirects=False)
    assert r.status_code == 403 and "text/html" in r.headers["content-type"]


def test_admin_overview(state):
    state.tunnel = FakeTunnel()
    state.holder.last_error = "line 3: bad indent"
    _, rec = state.store.create_api_key("alice", "laptop")
    state.store.record_usage(UsageEvent(username="alice", endpoint="chat.completions",
                                        model="llama3.1:8b", backend="fake", prompt_tokens=7,
                                        completion_tokens=8, key_id=rec.id))
    state.store.record_usage(UsageEvent(username="removed", endpoint="chat.completions",
                                        model="llama3.1:8b", backend="fake", prompt_tokens=1))
    c = make_client(state)
    login_csrf(c, "root")
    r = c.get("/ui/api/admin/overview")
    assert r.status_code == 200
    o = r.json()
    assert o["public_url"] == FakeTunnel.url
    assert o["tunnel"]["url"] == FakeTunnel.url and o["tunnel"]["status"] == "connected"
    assert o["tunnel"]["running"] is True
    assert o["config_error"] == "line 3: bad indent"
    assert o["backends"][0]["name"] == "fake" and o["backends"][0]["reachable"] is True
    users = {u["username"]: u for u in o["users"]}
    assert set(users) == {"alice", "bob", "root", "ghost"}
    assert users["alice"]["usage"]["tokens_today"] == 15
    assert users["alice"]["active_keys"] == 1
    assert users["root"]["usage"]["limits"]["tokens_per_day"] == 0
    assert [k["username"] for k in o["keys"]] == ["alice"]
    assert "sk-llop-" in o["keys"][0]["display"]
    assert {r["username"] for r in o["recent"]} == {"alice", "removed"}
    assert [x["username"] for x in o["orphan_usage_month"]] == ["removed"]


def test_admin_overview_without_tunnel(client):
    login_csrf(client, "root")
    o = client.get("/ui/api/admin/overview").json()
    assert o["tunnel"]["running"] is False
    assert o["public_url"] == ""
    assert o["config_error"] is None


def test_admin_revoke_and_logout_user(state):
    admin, alice = make_client(state), make_client(state)
    csrf = login_csrf(admin, "root")
    login_csrf(alice, "alice")
    _, rec = state.store.create_api_key("alice", "a")
    h = {"X-CSRF-Token": csrf}
    assert admin.post("/ui/api/admin/revoke-key", json={"id": rec.id}, headers=h).status_code == 200
    assert state.store.count_active_keys("alice") == 0
    assert admin.post("/ui/api/admin/revoke-key", json={"id": rec.id}, headers=h).status_code == 404
    assert admin.post("/ui/api/admin/revoke-key", json={}, headers=h).status_code == 400
    assert admin.post("/ui/api/admin/revoke-key", json={"id": "x"}).status_code == 403  # no CSRF

    assert alice.get("/ui/api/me").status_code == 200
    r = admin.post("/ui/api/admin/logout-user", json={"username": "alice"}, headers=h)
    assert r.status_code == 200 and r.json()["sessions_deleted"] == 1
    assert alice.get("/ui/api/me").status_code == 401
    assert admin.get("/ui/api/me").status_code == 200


def test_admin_page(client):
    r = client.get("/ui/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/login")
    login_csrf(client, "root")
    r = client.get("/ui/admin")
    assert r.status_code == 200 and "admin.js" in r.text


def test_hot_reload_role_change(state, client):
    login_csrf(client, "bob")
    assert client.get("/ui/api/admin/overview").status_code == 403
    cfg = make_config()
    cfg.accounts["bob"].role = "admin"
    state.holder.set(cfg)
    assert client.get("/ui/api/admin/overview").status_code == 200


# =========================================================================== pages
def test_root_redirects(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login"
    login_csrf(client, "alice")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/dashboard"
    r = client.get("/ui/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/dashboard"


def test_dashboard_redirects_when_logged_out(client):
    r = client.get("/ui/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login"


def test_pages_have_security_headers(client):
    r = client.get("/ui/login")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Local LLM Outsider Provider" in r.text and "{{" not in r.text
    for resp in (r, client.get("/ui/static/app.css"), client.get("/ui/api/me")):
        assert resp.headers["content-security-policy"] == (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    login_csrf(client, "alice")
    r = client.get("/ui/dashboard")
    assert r.status_code == 200 and "dashboard.js" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_unknown_ui_paths(client):
    r = client.get("/ui/api/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    r = client.get("/ui/nope")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    assert r.headers["x-frame-options"] == "DENY"


# =========================================================================== static
def test_static_files_served(client):
    r = client.get("/ui/static/app.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    etag = r.headers["etag"]
    r2 = client.get("/ui/static/app.css", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    r = client.get("/ui/static/common.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    r = client.get("/ui/static/favicon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize("path", [
    "/ui/static/..%2froutes.py",
    "/ui/static/%2e%2e/routes.py",
    "/ui/static/..%2f..%2fconfig.py",
    "/ui/static/..%5croutes.py",
    "/ui/static/%2e%2e%2f%2e%2e%2f__init__.py",
    "/ui/static/routes.py",
    "/ui/static/login.html",
    "/ui/static/.hidden.css",
    "/ui/static/missing.css",
    "/ui/static/sub/app.css",
])
def test_static_path_traversal_blocked(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert b"import" not in r.content


def test_static_files_are_csp_safe():
    """No inline scripts/styles/handlers anywhere (they would be blocked by the CSP)."""
    for f in STATIC_DIR.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), f.name
        assert "<style" not in text, f.name
        assert not re.search(r"\sstyle\s*=", text), f.name
        assert not re.search(r"\son[a-z]+\s*=", text), f.name
        assert "http://" not in text and "https://" not in text, f.name  # no CDN
    for f in STATIC_DIR.glob("*.js"):
        text = f.read_text(encoding="utf-8")
        assert "innerHTML" not in text, f.name
        assert "setAttribute('style'" not in text and "eval(" not in text, f.name
