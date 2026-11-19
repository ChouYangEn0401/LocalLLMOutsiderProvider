"""端對端：v0.2 統一節點的入口邊界。

- 經過假 cloudflared（從 loopback 連入，帶 ``Cf-Connecting-Ip`` 與公開的 ``Host``）：本機控制台的
  每一個寫入路徑在兩個公開入口都不存在；在本機入口上，只要有轉送標頭就一律拒絕（即使帶著有效的
  管理員 session 與 CSRF）。
- ``encryption=required`` 的 key 走明文 ``/v1`` 被拒（403）。
- GUEST key 用完預算後 401 ``key_exhausted``，而狀態仍然看得到。
- 更換 API 連結：舊 gate 立刻 404（沒有寬限期），既有的 key 在新 gate 照常可用，UI 入口與通道不受影響。
- ``public-ui`` 沒有 ``/v1``；``public-api`` 沒有登入頁、不發 cookie。
- 秘密路徑（gate）不會出現在伺服器的紀錄裡。
"""

from __future__ import annotations

import logging
import re

import aiohttp
import pytest

from llmop.auth import SESSION_COOKIE
from llmop.web import build_web_router

from .harness import (
    ALICE_PASSWORD,
    CSRF_HEADER,
    DEFAULT_REPLY,
    MODELS,
    OLLAMA_MODEL,
    ROOT_PASSWORD,
    WebClient,
)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 寫入請求的本文：每個欄位都填上「如果真的執行就會留下痕跡」的值。
WRITE_BODY = {
    "name": "cf-attack", "username": "root", "password": ROOT_PASSWORD, "id": "", "label": "cf-attack",
    "reason": "cf-attack", "change_host": True, "budget_tokens": 1000, "text": "server: {}\n",
    "base_sha256": "0" * 64, "key_id": "", "encryption": "optional",
}
CHAT = {"model": "anything", "messages": [{"role": "user", "content": "你好"}]}


def _write_routes(state, surface: str) -> set[tuple[str, str]]:
    """``build_web_router(state, surface)`` 註冊的全部寫入路由 (方法, 路徑樣板)。"""
    out = set()
    for route in build_web_router(state, surface).routes:
        for method in getattr(route, "methods", None) or ():
            if method in WRITE_METHODS:
                out.add((method, route.path))
    return out


def _fill(path: str, ids: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)(?::[^}]*)?\}", lambda m: ids.get(m.group(1), "x"), path)


def _side_effects(stack) -> dict:
    """寫入路由真的執行時會改變的東西。"""
    store = stack.state.store
    return {
        "workers": [(w.id, w.revoked_at, w.reg_expires_at) for w in store.list_workers()],
        "keys": [(k.id, k.revoked_at, k.encryption, k.sig_public_key)
                 for k in store.list_api_keys(None, include_revoked=True)],
        "gate": stack.state.api_link.gate,
        "config": stack.config_path.read_text(encoding="utf-8"),
        "buckets": store.list_buckets(),
    }


async def _send(http: aiohttp.ClientSession, method: str, url: str, *, cookie: str = "",
                csrf: str = "") -> aiohttp.ClientResponse:
    headers = {CSRF_HEADER: csrf} if csrf else {}
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    async with http.request(method, url, json=WRITE_BODY, headers=headers, allow_redirects=False) as resp:
        await resp.read()
        return resp


# ---------------------------------------------------------------------------
# 假 cloudflared：本機控制台的寫入路徑
# ---------------------------------------------------------------------------


async def test_every_console_write_route_is_refused_through_cloudflared(make_stack):
    stack = await make_stack(tunnels=True)
    state = stack.state
    local_writes = _write_routes(state, "local")
    public_writes = _write_routes(state, "public")
    console_only = local_writes - public_writes  # 管理寫入：只在本機控制台
    # 對照組：列舉到的確實是控制台的管理寫入路由（含發 GUEST key、worker 註冊碼、更換 API 連結、編輯設定檔）。
    assert {("POST", "/ui/api/admin/guest-keys"), ("POST", "/ui/api/admin/workers"),
            ("POST", "/ui/api/admin/workers/{worker_id}/revoke"), ("POST", "/ui/api/admin/api-link/rotate"),
            ("PUT", "/ui/api/admin/config"), ("POST", "/ui/api/admin/refill")} <= console_only
    assert public_writes <= local_writes  # 帳號網站的寫入（登入、自己的 key）本機控制台也有
    ids = {"worker_id": stack.worker_id, "key_id": stack.client.api_key_id}
    before = _side_effects(stack)

    ui_host = state.tunnels["ui"].host
    api_host = state.tunnels["api"].host
    ls = stack.cfg.listeners
    cf_local = await stack.start_cloudflared(ls.local, ui_host)  # 設錯：隧道指到本機入口
    cf_local_host = await stack.start_cloudflared(ls.local, f"127.0.0.1:{ls.local}")  # 連 Host 都像本機
    cf_ui = await stack.start_cloudflared(ls.public_ui, ui_host)
    cf_api = await stack.start_cloudflared(ls.public_api, api_host)

    # 攻擊者手上有一個真的、有效的管理員 session 與 CSRF（例如偷來的 cookie）。
    root_sid, root_csrf = stack.console.session_id, stack.console.csrf
    assert root_sid and root_csrf
    # 同一個管理員也從外面登入了帳號網站（這是允許的；帳號網站只有唯讀的管理資料）。
    async with stack.http.post(cf_ui.url + "/ui/api/login",
                               json={"username": "root", "password": ROOT_PASSWORD}) as resp:
        assert resp.status == 200
        ui_csrf = (await resp.json())["csrf"]
        ui_sid = resp.cookies[SESSION_COOKIE].value
        assert resp.cookies[SESSION_COOKIE]["secure"]  # 經過 https 隧道：cookie 只走 https

    checked = 0
    for method, template in sorted(local_writes):
        path = _fill(template, ids)
        # 1. 本機入口 + 轉送標頭：一律 403（netguard.is_local），不論 Host 長怎樣、帶不帶有效 session。
        for cf in (cf_local, cf_local_host):
            resp = await _send(stack.http, method, cf.url + path, cookie=root_sid, csrf=root_csrf)
            assert resp.status == 403, (method, path, cf.public_host)
            assert "set-cookie" not in resp.headers
        # 2. public-api 入口：沒有任何網頁路由（有沒有 gate 都一樣）。
        for prefix in ("", f"/{stack.gate}"):
            resp = await _send(stack.http, method, cf_api.url + prefix + path, cookie=ui_sid, csrf=ui_csrf)
            assert resp.status in (404, 405), (method, prefix, path, resp.status)
            assert "set-cookie" not in resp.headers
        # 3. public-ui 入口：管理寫入路由根本沒有註冊（帶著管理員 session 與正確的 CSRF 也一樣）。
        if (method, template) in console_only:
            resp = await _send(stack.http, method, cf_ui.url + path, cookie=ui_sid, csrf=ui_csrf)
            assert resp.status in (404, 405), (method, path, resp.status)
        checked += 1
    assert checked == len(local_writes) >= 10

    # 沒有任何一個寫入真的發生：worker 沒被撤銷、沒有新 key、gate 沒換、設定檔沒變、桶沒被補滿。
    assert _side_effects(stack) == before
    # 本機控制台的 session 也沒被登出（logout／logout-user 都沒執行）。
    assert (await stack.console.overview())["surface"] == "local"
    # 對照組：同一個 session 直接從本機連（沒有轉送標頭）就可以寫。
    status, data = await stack.console.call("POST", "/ui/api/admin/api-link/rotate", {"reason": "對照組"})
    assert status == 200, data
    assert stack.state.api_link.gate != before["gate"]


async def test_local_listener_refuses_forwarded_requests_even_for_reads(make_stack):
    """本機入口上任何轉送標頭都代表「經過代理」：連登入頁、免 key 的 /v1 都拒絕。"""
    stack = await make_stack()
    for headers in ({"Cf-Connecting-Ip": "203.0.113.7"}, {"X-Forwarded-For": "203.0.113.7"},
                    {"Forwarded": "for=203.0.113.7"}, {"Host": "evil.example"},
                    {"Origin": "https://evil.example"}):
        for method, path, body in (("GET", "/ui/login", None), ("GET", "/v1/models", None),
                                   ("POST", "/v1/chat/completions", CHAT),
                                   ("POST", "/ui/api/login", {"username": "root", "password": ROOT_PASSWORD})):
            async with stack.http.request(method, stack.local_url + path, json=body, headers=headers) as resp:
                assert resp.status == 403, (headers, path)
                assert "set-cookie" not in resp.headers
    assert stack.backends["ollama"].calls == []
    # 對照組：同樣的請求直接從本機連就可以（免 key 的 /v1 走節點自己的後端轉接層）。
    async with stack.http.post(stack.local_url + "/v1/chat/completions", json=CHAT) as resp:
        assert resp.status == 200
        assert (await resp.json())["model"] == OLLAMA_MODEL
    assert stack.backends["ollama"].paths == ["/v1/chat/completions"]


# ---------------------------------------------------------------------------
# encryption=required 的 key 不能走明文
# ---------------------------------------------------------------------------


async def test_encryption_required_key_is_refused_on_plaintext_v1(make_stack):
    stack = await make_stack()
    key = stack.client.api_key  # alice 在帳號網站建立的 encryption=required 金鑰
    for method, path, body in (("POST", "/v1/chat/completions", CHAT), ("GET", "/v1/models", None),
                               ("GET", "/v1/me", None)):
        status, data = await stack.plain(method, path, api_key=key, json_body=body)
        assert status == 403, (path, data)
        assert data["error"]["code"] == "encryption_required"
    assert stack.backends["ollama"].calls == []
    # 同一把 key 走加密面照常可用。
    status, data, _ = await stack.chat(MODELS["ollama"])
    assert status == 200 and data["choices"][0]["message"]["content"] == DEFAULT_REPLY
    # 改成 optional 之後，明文 /v1 才放行（帳號網站上的設定確實生效）。
    status, data = await stack.portal.call("PATCH", f"/ui/api/keys/{stack.client.api_key_id}",
                                           {"encryption": "optional"})
    assert status == 200, data
    status, data = await stack.plain("POST", "/v1/chat/completions", api_key=key, json_body=CHAT)
    assert status == 200, data
    assert data["model"] == OLLAMA_MODEL  # 單一模型：請求的 model 一律覆寫


# ---------------------------------------------------------------------------
# GUEST key
# ---------------------------------------------------------------------------


async def test_guest_key_works_until_its_budget_is_used_then_key_exhausted(make_stack):
    stack = await make_stack()
    per_request = sum(stack.backends["ollama"].usage)  # 16
    budget = per_request + per_request // 2  # 一筆用不完、兩筆就超過（事後記帳）
    record, guest_key = await stack.console.create_guest_key("e2e 訪客", budget)
    assert record["status"]["guest"]["status"] == "active"

    for _ in range(2):
        status, data = await stack.plain("POST", "/v1/chat/completions", api_key=guest_key, json_body=CHAT)
        assert status == 200, data
    status, me = await stack.plain("GET", "/v1/me", api_key=guest_key)
    assert status == 401 and me["error"]["code"] == "key_exhausted"

    # 預算用完：之後一律 401 key_exhausted，請求不會到後端。
    status, data = await stack.plain("POST", "/v1/chat/completions", api_key=guest_key, json_body=CHAT)
    assert status == 401, data
    assert data["error"]["code"] == "key_exhausted"
    assert len(stack.backends["ollama"].calls) == 2

    # 狀態仍然看得到：本機控制台與帳號網站（管理員的唯讀資料）都列出這把 key，標示已用完。
    ui_admin = WebClient(stack.ui_url)
    try:
        await ui_admin.login("root", ROOT_PASSWORD)
        for client in (stack.console, ui_admin):
            (entry,) = [k for k in (await client.overview())["guest_keys"] if k["id"] == record["id"]]
            guest = entry["status"]["guest"]
            assert guest["status"] == "exhausted"
            assert guest["used"] == 2 * per_request and guest["budget"] == budget
            assert guest["remaining"] == 0
            assert entry["label"] == "e2e 訪客"
            assert guest_key not in str(entry)
        (masked,) = [k for k in (await ui_admin.overview())["guest_keys"] if k["id"] == record["id"]]
        assert "sig_public_key" not in masked
    finally:
        await ui_admin.close()
    rows = [r for r in stack.state.store.recent_usage(None) if r["key_id"] == record["id"]]
    assert len(rows) == 2 and {r["username"] for r in rows} == {f"guest:{record['id']}"}


# ---------------------------------------------------------------------------
# 更換 API 連結
# ---------------------------------------------------------------------------


async def test_rotating_the_api_link(make_stack):
    stack = await make_stack(tunnels=True)
    state = stack.state
    _, plain_key = await stack.portal.create_key("明文 key")
    old_gate = stack.gate
    old_tunnel_url = stack.tunnel_url
    ui_tunnel, api_tunnel = state.tunnels["ui"], state.tunnels["api"]
    ui_server = next(s for s in stack.node.servers if s.name == "public-ui")
    assert (await stack.plain("GET", "/v1/models", api_key=plain_key))[0] == 200
    assert (await stack.chat(MODELS["ollama"]))[0] == 200

    # 管理員在本機控制台按「更換 API 連結」（連主機名一起換）。
    status, data = await stack.console.call("POST", "/ui/api/admin/api-link/rotate",
                                            {"change_host": True, "reason": "e2e 外洩"})
    assert status == 200, data
    new_gate = stack.gate
    assert new_gate != old_gate
    assert data["api_link"]["url"] == state.api_link.current_url()
    assert data["api_link"]["url"].startswith(state.tunnels["api"].url + "/")

    # 舊 gate 立刻 404（預設沒有寬限期），不論明文、加密面或 worker 連線。
    for path in ("/v1/models", "/v1/chat/completions", "/e2e/v1/models", "/health"):
        status, _ = await stack.plain("GET", path, api_key=plain_key, base=old_tunnel_url)
        assert status == 404, path
    with pytest.raises(aiohttp.WSServerHandshakeError):
        async with stack.http.ws_connect(f"ws://127.0.0.1:{stack.relay.port}/{old_gate}/worker/ws"):
            pass
    # 釘選、gate 都還是舊的用戶端代理：連不到閘道，請求沒有送出。
    sent = len(stack.relay.posted_envelopes())
    status, _, _ = await stack.chat(MODELS["ollama"])
    assert status != 200
    assert len(stack.relay.posted_envelopes()) == sent

    # 既有的 key 在新 gate 照常可用：明文的、加密的（用戶端只要更新 gateway_url）。
    status, data = await stack.plain("POST", "/v1/chat/completions", api_key=plain_key, json_body=CHAT)
    assert status == 200, data
    new_proxy = await stack.start_proxy(gateway_url=stack.tunnel_url)
    status, data, _ = await stack.chat(MODELS["ollama"], proxy_url=new_proxy)
    assert status == 200 and data["choices"][0]["message"]["content"] == DEFAULT_REPLY

    # 只有 API 通道被換掉；UI 通道、UI 入口、帳號網站的登入都沒被碰到。
    assert state.tunnels["api"] is not api_tunnel and api_tunnel.stops == 1
    assert state.tunnels["ui"] is ui_tunnel and ui_tunnel.starts == 1 and ui_tunnel.stops == 0
    assert next(s for s in stack.node.servers if s.name == "public-ui") is ui_server
    assert not ui_server.task.done()
    assert (await stack.portal.portal())["api_link"]["url"] == state.api_link.current_url()
    cf_ui = await stack.start_cloudflared(stack.cfg.listeners.public_ui, ui_tunnel.host)
    async with stack.http.get(cf_ui.url + "/ui/login") as resp:
        assert resp.status == 200
    # 稽核紀錄只有雜湊，沒有 gate 本身。
    (entry,) = [e for e in state.api_link.audit_entries() if e["reason"] == "e2e 外洩"]
    assert entry["change_host"] is True
    assert old_gate not in str(state.api_link.audit_entries())


# ---------------------------------------------------------------------------
# 入口分離
# ---------------------------------------------------------------------------


async def test_public_ui_has_no_api_and_public_api_has_no_login_or_cookies(make_stack):
    stack = await make_stack(tunnels=True)
    state = stack.state
    key = stack.client.api_key
    cf_ui = await stack.start_cloudflared(stack.cfg.listeners.public_ui, state.tunnels["ui"].host)
    cf_api = await stack.start_cloudflared(stack.cfg.listeners.public_api, state.tunnels["api"].host)
    auth = {"Authorization": f"Bearer {key}"}

    # public-ui：帳號網站，完全沒有 API（有沒有 gate、帶不帶 key 都一樣）。
    async with stack.http.get(cf_ui.url + "/ui/login") as resp:
        assert resp.status == 200  # 對照組
    for method, path in (("GET", "/v1/models"), ("POST", "/v1/chat/completions"), ("GET", "/v1/me"),
                         ("GET", "/e2e/v1/models"), ("POST", "/e2e/v1/chat/completions"),
                         ("GET", f"/{stack.gate}/v1/models"), ("GET", f"/{stack.gate}/e2e/v1/models"),
                         ("GET", "/worker/ws"), ("GET", f"/{stack.gate}/worker/ws")):
        async with stack.http.request(method, cf_ui.url + path, json=CHAT if method == "POST" else None,
                                      headers=auth) as resp:
            assert resp.status in (404, 405), (method, path, resp.status)
    with pytest.raises(aiohttp.WSServerHandshakeError):
        async with stack.http.ws_connect(f"ws://127.0.0.1:{stack.cfg.listeners.public_ui}/worker/ws"):
            pass

    # public-api：沒有登入頁、不發 cookie（有沒有 gate 都一樣）；只有 /v1、/e2e/v1、/worker/ws。
    responses = []
    for prefix in ("", f"/{stack.gate}"):
        for method, path, body in (("GET", "/", None), ("GET", "/ui/login", None), ("GET", "/ui/dashboard", None),
                                   ("GET", "/ui/admin", None), ("GET", "/ui/api/session", None),
                                   ("POST", "/ui/api/login", {"username": "alice", "password": ALICE_PASSWORD}),
                                   ("GET", "/ui/static/app.js", None)):
            async with stack.http.request(method, cf_api.url + prefix + path, json=body,
                                          allow_redirects=False) as resp:
                responses.append(resp)
                assert resp.status in (404, 405), (method, prefix + path, resp.status)
    async with stack.http.get(cf_api.url + f"/{stack.gate}/e2e/v1/models", headers=auth) as resp:
        responses.append(resp)
        assert resp.status == 200  # 對照組：加密面確實掛在這個入口
    async with stack.http.get(cf_api.url + f"/{stack.gate}/health") as resp:
        responses.append(resp)
        assert resp.status == 200
    for resp in responses:
        assert "set-cookie" not in resp.headers
    assert all(status != 200 or path.endswith(("/e2e/v1/models", "/health")) for _, path, status in cf_api.seen)


# ---------------------------------------------------------------------------
# gate 不進紀錄
# ---------------------------------------------------------------------------


class _Collect(logging.Filter):
    """收集紀錄（掛在 logger 上的 filter：uvicorn 每次建立 Config 都會以 dictConfig 換掉 handler，
    但不會拿掉 filter）。訊息在最後才組出來，所以看到的是所有 filter（包括節點的遮蔽）處理後的結果，
    也就是 handler 真正寫出去的內容。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def filter(self, record: logging.LogRecord) -> bool:
        self.records.append(record)
        return True

    @property
    def lines(self) -> list[str]:
        return [r.getMessage() for r in self.records]


async def test_gate_never_appears_in_server_logs(make_stack):
    """uvicorn 對每一條 WebSocket 連線記一行 ``"WebSocket <路徑>" [accepted]``（不受 access_log 控制）；
    public-api 上的路徑第一段就是 gate，必須遮蔽。"""
    collect = _Collect()
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "llmop", "llmop.secure.api", "llmop.secure.hub",
             "llmop.secure.worker_ws", "llmop.node", "llmop.access", "llmop.worker.link")
    loggers = [logging.getLogger(name) for name in names]
    for lg in loggers:
        lg.addFilter(collect)
    try:
        stack = await make_stack()  # worker 經過隧道連到 /<gate>/worker/ws
        gate = stack.gate
        # 也試一個錯的 gate（會被拒絕，但同樣不該把路徑原樣寫進紀錄）。
        with pytest.raises(aiohttp.WSServerHandshakeError):
            async with stack.http.ws_connect(f"ws://127.0.0.1:{stack.relay.port}/wrong-{gate[:8]}/worker/ws"):
                pass
        assert (await stack.chat(MODELS["ollama"]))[0] == 200
        await stack.restart_node()  # 重新啟動時的訊息也算
        await stack.wait_online(True)
    finally:
        for lg in loggers:
            lg.removeFilter(collect)
    text = "\n".join(collect.lines)
    assert "[accepted]" in text  # 對照組：uvicorn 的 WebSocket 紀錄確實收進來了
    assert "/***/worker/ws" in text
    assert gate not in text
    assert f"wrong-{gate[:8]}" not in text
