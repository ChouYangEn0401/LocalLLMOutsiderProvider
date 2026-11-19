"""管理介面：登入、鎖定、CSRF、cookie 旗標、僅限本機、秘密值只出現一次。"""

from __future__ import annotations

import re

from llmop import crypto
from llmop.gateway.admin import CSRF_HEADER, SESSION_COOKIE
from llmop.protocol import b64u_encode

from .conftest import ADMIN_PASSWORD


async def login(gateway, password: str = ADMIN_PASSWORD, headers: dict | None = None):
    return await gateway.client.post("/admin/api/login", json={"password": password}, headers=headers or {})


async def logged_in(gateway) -> dict:
    """登入並回傳之後呼叫要帶的 CSRF 標頭。"""
    resp = await login(gateway)
    assert resp.status == 200
    return {CSRF_HEADER: (await resp.json())["csrf"]}


def set_cookie_header(resp) -> str:
    headers = [v for v in resp.headers.getall("Set-Cookie") if v.startswith(SESSION_COOKIE + "=")]
    assert len(headers) == 1
    return headers[0]


async def test_login_success_and_cookie_flags(gateway):
    resp = await login(gateway)
    assert resp.status == 200
    data = await resp.json()
    assert len(data["csrf"]) == 43
    cookie = set_cookie_header(resp)
    assert "HttpOnly" in cookie
    assert re.search(r"SameSite=Strict", cookie, re.I)
    assert "Secure" not in cookie
    assert "Max-Age=43200" in cookie
    assert "Path=/admin" in cookie

    resp = await gateway.client.get("/admin/api/session")
    assert await resp.json() == {"authenticated": True, "csrf": data["csrf"]}


async def test_secure_cookie_behind_https(gateway):
    resp = await login(gateway, headers={"X-Forwarded-Proto": "https"})
    assert resp.status == 200
    assert "Secure" in set_cookie_header(resp)


async def test_login_failure(gateway):
    resp = await login(gateway, "wrong password")
    assert resp.status == 401
    assert (await resp.json())["error"]["code"] == "unauthorized"
    assert "Set-Cookie" not in resp.headers
    resp = await gateway.client.get("/admin/api/keys")
    assert resp.status == 401
    resp = await gateway.client.get("/admin/api/session")
    assert await resp.json() == {"authenticated": False}


async def test_login_lockout_after_five_failures(gateway):
    for _ in range(5):
        assert (await login(gateway, "wrong password")).status == 401
    resp = await login(gateway)  # 密碼正確也被拒絕
    assert resp.status == 429
    assert (await resp.json())["error"]["code"] == "too_many_attempts"


async def test_login_requires_json(gateway):
    resp = await gateway.client.post("/admin/api/login", data={"password": ADMIN_PASSWORD})
    assert resp.status == 415


async def test_csrf_header_required(gateway):
    csrf = await logged_in(gateway)
    body = {"name": "k"}
    resp = await gateway.client.post("/admin/api/keys", json=body)
    assert resp.status == 403 and (await resp.json())["error"]["code"] == "csrf"
    resp = await gateway.client.post("/admin/api/keys", json=body, headers={CSRF_HEADER: "nope"})
    assert resp.status == 403
    resp = await gateway.client.post(
        "/admin/api/keys", data="name=k", headers=csrf | {"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert resp.status == 415
    resp = await gateway.client.post("/admin/api/keys", json=body, headers=csrf)
    assert resp.status == 201


async def test_logout(gateway):
    csrf = await logged_in(gateway)
    resp = await gateway.client.post("/admin/api/logout", json={}, headers=csrf)
    assert resp.status == 200
    gateway.client.session.cookie_jar.clear()
    resp = await gateway.client.get("/admin/api/keys")
    assert resp.status == 401


async def test_admin_local_only(make_gateway):
    gw = await make_gateway()
    for path in ("/admin/", "/admin", "/admin/api/session", "/admin/static/app.js"):
        resp = await gw.client.get(path, headers={"Cf-Connecting-Ip": "203.0.113.9"}, allow_redirects=False)
        assert resp.status == 404, path
    resp = await login(gw, headers={"Cf-Connecting-Ip": "203.0.113.9"})
    assert resp.status == 404
    # 用戶端 API 不受影響。
    resp = await gw.client.get("/healthz", headers={"Cf-Connecting-Ip": "203.0.113.9"})
    assert resp.status == 200


async def test_admin_remote_allowed_when_disabled(make_gateway):
    gw = await make_gateway(admin_local_only=False)
    resp = await gw.client.get("/admin/api/session", headers={"Cf-Connecting-Ip": "203.0.113.9"})
    assert resp.status == 200
    # 經過通道時以 Cf-Connecting-Ip 計算失敗次數，不同來源互不影響。
    for _ in range(5):
        assert (await login(gw, "wrong password", headers={"Cf-Connecting-Ip": "203.0.113.9"})).status == 401
    assert (await login(gw, headers={"Cf-Connecting-Ip": "203.0.113.9"})).status == 429
    assert (await login(gw, headers={"Cf-Connecting-Ip": "198.51.100.7"})).status == 200


async def test_static_ui_and_security_headers(gateway):
    resp = await gateway.client.get("/admin/")
    assert resp.status == 200
    html = await resp.text()
    assert "<script src=\"static/app.js\"" in html
    assert "<script>" not in html  # 沒有內嵌腳本
    assert resp.headers["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    for path in ("/admin/static/app.js", "/admin/static/style.css"):
        resp = await gateway.client.get(path)
        assert resp.status == 200, path
    resp = await gateway.client.get("/admin", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/admin/"


async def test_api_key_plaintext_only_in_creation_response(gateway):
    csrf = await logged_in(gateway)
    signing_key = crypto.generate_signing_key()
    sig_pub = b64u_encode(crypto.public_raw(signing_key))
    resp = await gateway.client.post(
        "/admin/api/keys",
        json={"name": "SYNOX", "sig_public_key": sig_pub, "daily_request_limit": 100, "daily_token_limit": None},
        headers=csrf,
    )
    assert resp.status == 201
    created = await resp.json()
    secret = created["api_key"]
    assert secret.startswith("llmop_sk_") and len(secret) == len("llmop_sk_") + 43
    key = created["key"]
    assert re.fullmatch(r"k_[0-9a-f]{12}", key["id"])
    assert key["sig_key_id"] == crypto.signing_key_id(crypto.public_raw(signing_key))

    resp = await gateway.client.get("/admin/api/keys")
    listing = await resp.text()
    assert secret not in listing
    keys = (await resp.json())["keys"]
    assert keys[0]["id"] == key["id"]
    assert keys[0]["today"] == {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}

    worker_resp = await gateway.client.post("/admin/api/workers", json={"name": "w"}, headers=csrf)
    reg_token = (await worker_resp.json())["reg_token"]
    assert reg_token.startswith("llmop_reg_")

    raw_db = gateway.store.path.read_bytes()
    assert secret.encode() not in raw_db
    assert reg_token.encode() not in raw_db
    # 這把金鑰確實可以使用（資料庫裡有它的雜湊）。
    assert gateway.store.find_api_key(secret).id == key["id"]


async def test_update_and_revoke_key(gateway):
    csrf = await logged_in(gateway)
    created = await (await gateway.client.post("/admin/api/keys", json={"name": "k"}, headers=csrf)).json()
    key_id = created["key"]["id"]
    assert created["key"]["sig_public_key"] is None

    bad = await gateway.client.patch(f"/admin/api/keys/{key_id}", json={"sig_public_key": "abc"}, headers=csrf)
    assert bad.status == 400
    bad = await gateway.client.patch(f"/admin/api/keys/{key_id}", json={"daily_token_limit": -1}, headers=csrf)
    assert bad.status == 400
    bad = await gateway.client.patch(f"/admin/api/keys/{key_id}", json={"name": "x"}, headers=csrf)
    assert bad.status == 400

    sig_pub = b64u_encode(crypto.public_raw(crypto.generate_signing_key()))
    resp = await gateway.client.patch(
        f"/admin/api/keys/{key_id}",
        json={"sig_public_key": sig_pub, "daily_request_limit": 5, "daily_token_limit": 1000},
        headers=csrf,
    )
    assert resp.status == 200
    key = (await resp.json())["key"]
    assert (key["sig_public_key"], key["daily_request_limit"], key["daily_token_limit"]) == (sig_pub, 5, 1000)

    resp = await gateway.client.patch(f"/admin/api/keys/{key_id}", json={"daily_request_limit": None}, headers=csrf)
    assert (await resp.json())["key"]["daily_request_limit"] is None

    resp = await gateway.client.post(f"/admin/api/keys/{key_id}/revoke", json={}, headers=csrf)
    assert (await resp.json())["key"]["revoked_at"] is not None
    resp = await gateway.client.post("/admin/api/keys/k_000000000000/revoke", json={}, headers=csrf)
    assert resp.status == 404


async def test_workers_admin(gateway):
    csrf = await logged_in(gateway)
    worker = await gateway.connect_worker(models=["m1", "m2"], name="online-one")

    resp = await gateway.client.get("/admin/api/workers")
    workers = (await resp.json())["workers"]
    assert len(workers) == 1
    w = workers[0]
    assert (w["id"], w["name"], w["online"], w["models"]) == (worker.worker_id, "online-one", True, ["m1", "m2"])
    assert w["current_key_id"] == worker.key_id
    assert w["keys"][0]["key_id"] == worker.key_id
    assert w["keys"][0]["public_key"] == b64u_encode(worker.public)
    assert isinstance(w["keys"][0]["first_seen_at"], int)

    resp = await gateway.client.post(f"/admin/api/workers/{worker.worker_id}/reg-token", json={}, headers=csrf)
    assert (await resp.json())["reg_token"].startswith("llmop_reg_")

    resp = await gateway.client.post(f"/admin/api/workers/{worker.worker_id}/revoke", json={}, headers=csrf)
    data = (await resp.json())["worker"]
    assert data["revoked_at"] is not None and data["online"] is False
    await worker.task  # 被撤銷的 worker 立刻被斷線
    resp = await gateway.client.post(f"/admin/api/workers/{worker.worker_id}/reg-token", json={}, headers=csrf)
    assert resp.status == 409


async def test_usage_endpoint(gateway):
    worker = await gateway.connect_worker()
    api_client = gateway.new_api_client()
    envelope, _ = api_client.seal(worker.public)
    assert (await gateway.post(api_client, envelope))[0] == 200

    await logged_in(gateway)
    resp = await gateway.client.get("/admin/api/usage?days=3")
    data = await resp.json()
    assert len(data["usage"]) == 1
    row = data["usage"][0]
    assert (row["api_key_id"], row["requests"], row["prompt_tokens"], row["completion_tokens"]) == (
        api_client.key_id, 1, 7, 11,
    )
    assert (await gateway.client.get("/admin/api/usage?days=0")).status == 400
