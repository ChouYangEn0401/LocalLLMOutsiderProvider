"""T-R: the three listeners of the v0.2 node, attacked from the outside.

MERGE_PLAN 6.1 classes covered here: 3 CSRF, 5 auth bypass, 10 flooding (body
limits), 11 DNS rebinding / Host, 12 SSRF-ish exposure of internals, 14 clickjacking,
15 unsafe parsing (deep JSON), 19 API key never reaches admin, 21 public surface
has no writes, 23 error leakage. MERGE_PLAN 6.2 holes (b) (c) (d) (h).
"""

from __future__ import annotations

import pytest

from llmop.netguard import FORWARD_HEADERS

from .conftest import CHAT, SIG, login

pytestmark = pytest.mark.asyncio

BIG = 4 * 1024 * 1024 + 10
DEEP = b"[" * 100_000 + b"]" * 100_000

ADMIN_WRITES = [
    ("POST", "/ui/api/admin/revoke-key", {"id": "x"}),
    ("POST", "/ui/api/admin/logout-user", {"username": "alice"}),
    ("POST", "/ui/api/admin/guest-keys", {"label": "x"}),
    ("POST", "/ui/api/admin/refill", {"username": "alice"}),
    ("POST", "/ui/api/admin/workers", {"name": "w"}),
    ("POST", "/ui/api/admin/workers/w_1/revoke", {}),
    ("POST", "/ui/api/admin/workers/w_1/reg-token", {}),
    ("POST", "/ui/api/admin/api-link/rotate", {}),
    ("PUT", "/ui/api/admin/config", {"text": "x", "base_sha256": "y"}),
]


# --------------------------------------------------------------------------- (d) is_local
@pytest.mark.parametrize("header", FORWARD_HEADERS)
async def test_local_console_refuses_every_forwarding_header(sec, header):
    """cloudflared connects from 127.0.0.1: any forwarding header => not local (6.2-d)."""
    csrf = await login(sec.local)
    h = {header: "203.0.113.9", "X-CSRF-Token": csrf}
    assert (await sec.local.get("/ui/api/admin/config", headers=h)).status_code == 403
    assert (await sec.local.post("/ui/api/admin/guest-keys", json={}, headers=h)).status_code == 403
    assert (await sec.local.post("/v1/chat/completions", json=CHAT, headers=h)).status_code == 403
    assert (await sec.local.post("/ui/api/login", json={"username": "root", "password": "x"},
                                 headers={header: "1.2.3.4"})).status_code == 403


async def test_local_console_dns_rebinding_and_cross_origin(sec):
    """(6.2-c, class 11) foreign Host, wrong port, foreign Origin, cross-site fetch."""
    for headers in ({"Host": "evil.example:18781"}, {"Host": "127.0.0.1:9999"},
                    {"Host": "localhost.evil.example"}, {"Origin": "http://evil.example"},
                    {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
        r = await sec.local.post("/v1/chat/completions", json=CHAT, headers=headers)
        assert r.status_code == 403, headers
    assert (await sec.local.get("/ui/login", headers={"Host": "localhost:18781"})).status_code == 200


async def test_raw_config_never_reaches_a_public_listener(sec):
    """The raw config.yaml holds secrets: only the local console serves it."""
    csrf = await login(sec.ui)
    for client in (sec.ui, sec.api, sec.api_root):
        r = await client.get("/ui/api/admin/config")
        assert r.status_code == 404, client.base_url
    # the account website's admin overview is masked: no backend URL / key, no config
    r = await sec.ui.get("/ui/api/admin/overview", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    text = r.text
    assert "ollama.test" not in text and "SECRET" not in text and "password_hash" not in text


async def test_raw_config_needs_a_session_made_on_the_console(sec):
    """T-R fix: a session from the account website (cookies are shared across ports
    on 127.0.0.1) may not read the raw file (password hashes, backend keys)."""
    sid, _ = sec.state.store.create_session("root", 600, origin="remote")
    sec.local.cookies.set("llmop_session", sid)
    r = await sec.local.get("/ui/api/admin/config")
    assert r.status_code == 403 and r.json()["error"]["code"] == "local_session_required"
    await login(sec.local)
    r = await sec.local.get("/ui/api/admin/config")
    assert r.status_code in (200, 404)  # 404 = this test config has no file


async def test_public_ui_has_no_admin_write_route(sec):
    """(class 21, D4) every admin write is 404/405 on the account website, even for an
    admin with a valid session and CSRF token."""
    csrf = await login(sec.ui)
    for method, path, body in ADMIN_WRITES:
        r = await sec.ui.request(method, path, json=body, headers={"X-CSRF-Token": csrf})
        assert r.status_code in (404, 405), (method, path, r.status_code)
    assert sec.state.store.list_guest_keys() == []


async def test_public_ui_host_check_and_body_limit(sec):
    """(6.2-b/c) 421 for a foreign Host; 413 above 1 MiB (Content-Length and chunked)."""
    assert (await sec.ui.get("/ui/login", headers={"Host": "evil.example"})).status_code == 421
    r = await sec.ui.post("/ui/api/login", content=b"x" * (1024 * 1024 + 1),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 413

    async def chunks():
        for _ in range(3):
            yield b"x" * (512 * 1024)

    r = await sec.ui.post("/ui/api/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    # the public UI has no API at all
    for path in ("/v1/models", "/e2e/v1/models"):
        assert (await sec.ui.get(path)).status_code == 404


async def test_public_api_gate_host_and_body_limits(sec):
    key, _ = sec.state.store.create_api_key("alice", "k")
    auth = {"Authorization": f"Bearer {key}"}
    assert (await sec.api_root.get("/v1/models", headers=auth)).status_code == 404
    assert (await sec.api_root.get("/" + "A" * 43 + "/v1/models", headers=auth)).status_code == 404
    assert (await sec.api.get("/v1/models", headers=auth)).status_code == 200
    assert (await sec.api.get("/v1/models", headers={**auth, "Host": "evil.example"})).status_code == 421
    # 4 MiB on /v1 (6.2-b): Content-Length first ...
    r = await sec.api.post("/v1/chat/completions", content=b"x" * BIG,
                           headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 413

    async def chunks():  # ... and a chunked body counted while it is read
        for _ in range(5):
            yield b"x" * (1024 * 1024)

    r = await sec.api.post("/v1/chat/completions", content=chunks(),
                           headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 413
    # 16 MiB on the encrypted path and on the whole public API listener
    r = await sec.api.post("/e2e/v1/chat/completions", content=b"x" * (16 * 1024 * 1024 + 1),
                           headers=auth)
    assert r.status_code == 413
    # no login page / cookies on the API listener
    for path in ("/ui/login", "/", "/ui/api/admin/overview"):
        r = await sec.api.get(path)
        assert r.status_code == 404 and "set-cookie" not in r.headers


async def test_public_api_v1_refuses_encryption_required_keys(sec, fake_net):
    key, _ = sec.state.store.create_api_key("alice", "enc", encryption="required", sig_public_key=SIG)
    r = await sec.api.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "encryption_required"
    r = await sec.api.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    gkey, _ = sec.state.store.create_guest_key("g", encryption="required", sig_public_key=SIG)
    r = await sec.api.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {gkey}"})
    assert r.status_code == 403


async def test_api_key_never_opens_the_web_ui(sec):
    """(class 19) an API key is not a session: the UI says 401 with only a key."""
    key, _ = sec.state.store.create_api_key("root", "k")
    r = await sec.ui.get("/ui/api/portal", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


async def test_deep_json_is_400_not_500(sec):
    """(class 15) T-R fix: json.loads raises RecursionError on deep nesting."""
    key, _ = sec.state.store.create_api_key("alice", "k")
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = await sec.api.post("/v1/chat/completions", content=DEEP, headers=h)
    assert r.status_code == 400, r.text
    r = await sec.api.post("/e2e/v1/chat/completions", content=DEEP, headers=h)
    assert r.status_code == 400, r.text
    r = await sec.ui.post("/ui/api/login", content=DEEP, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text


async def test_backend_error_text_never_reaches_a_public_client(sec, fake_net):
    """(6.2-h, class 23) internal URLs / paths in a backend error stay in the log."""
    key, _ = sec.state.store.create_api_key("alice", "k")
    fake_net.ollama.fail = (500, {"error": {"message": "boom at http://10.0.0.5/admin C:\\secret\\x"}})
    r = await sec.api.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code >= 500
    assert "10.0.0.5" not in r.text and "secret" not in r.text and "ollama" not in r.text.lower()
    fake_net.ollama.fail = None
    fake_net.ollama.down = True
    r = await sec.api.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code >= 500 and "ollama.test" not in r.text


async def test_csrf_and_origin_on_the_account_website(sec):
    """(class 3) own-key writes need the CSRF token and a matching Origin."""
    csrf = await login(sec.ui, "alice")
    assert (await sec.ui.post("/ui/api/keys", json={"name": "a"})).status_code == 403
    r = await sec.ui.post("/ui/api/keys", json={"name": "a"},
                          headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await sec.ui.post("/ui/api/keys", json={"name": "a"},
                          headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = await sec.ui.post("/ui/api/keys", json={"name": "a"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201
    r = await sec.ui.post("/ui/api/keys", content=b'{"name":"b"}',
                          headers={"X-CSRF-Token": csrf, "Content-Type": "text/plain"})
    assert r.status_code == 415


async def test_clickjacking_and_csp_headers(sec):
    """(classes 4, 14) strict CSP + frame denial on every UI response."""
    for client in (sec.ui, sec.local):
        r = await client.get("/ui/login")
        assert r.headers["X-Frame-Options"] == "DENY"
        csp = r.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
        assert "unsafe-inline" not in csp
        assert r.headers["Referrer-Policy"] == "no-referrer"


async def test_redirects_are_fixed_internal_paths(sec):
    """(class 13) no open redirect: every redirect is to a fixed /ui path."""
    for path in ("/", "/ui", "/ui/dashboard", "/ui/admin"):
        r = await sec.ui.get(path, params={"next": "https://evil.example"})
        assert r.status_code == 303
        assert r.headers["location"] in ("/ui/login", "/ui/dashboard", "/ui/login?next=/ui/admin")


async def test_static_path_traversal(sec):
    """(class 2) static files only by whitelisted name."""
    for name in ("..%2fconfig.yaml", "../../app.py", "%2e%2e/%2e%2e/secret", "admin.js/../x.js",
                 "..\\..\\app.py"):
        r = await sec.ui.get("/ui/static/" + name)
        assert r.status_code == 404, name
    assert (await sec.ui.get("/ui/static/app.css")).status_code == 200


async def test_login_is_throttled_and_injection_safe(sec):
    """(classes 1, 7) SQL-ish usernames are just wrong logins; then 429."""
    for _ in range(10):
        r = await sec.ui.post("/ui/api/login", json={"username": "root' OR '1'='1", "password": "x"})
        assert r.status_code == 401
    r = await sec.ui.post("/ui/api/login", json={"username": "root' OR '1'='1", "password": "x"})
    assert r.status_code == 429 and "Retry-After" in r.headers
