"""netguard：本機／外網判斷、公開面 Host、gate middleware、log 清理。"""

import pytest
from fastapi import FastAPI, WebSocket
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from llmop import netguard
from llmop.netguard import (
    FORWARD_HEADERS,
    GateMiddleware,
    GateState,
    check_public_host,
    clean_log,
    gate_hash,
    is_local,
    redact_path,
)

PORT = 8781


def make_request(headers=None, client=("127.0.0.1", 50000), server=("127.0.0.1", PORT), host="127.0.0.1:8781",
                 scope_type="http"):
    raw = []
    if host is not None:
        raw.append((b"host", host.encode("latin-1")))
    for k, v in headers or []:
        raw.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    scope = {"type": scope_type, "method": "GET", "path": "/", "raw_path": b"/", "query_string": b"",
             "headers": raw, "client": client, "server": server}
    if scope_type == "websocket":
        return WebSocket(scope, None, None)
    return Request(scope)


# --------------------------------------------------------------------------- is_local


@pytest.mark.parametrize("host", ["127.0.0.1:8781", "localhost:8781", "[::1]:8781", "LOCALHOST:8781",
                                  "localhost.:8781"])
@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "127.0.0.2", "::ffff:127.0.0.1"])
def test_loopback_without_forward_headers_is_local(host, peer):
    assert is_local(make_request(client=(peer, 1234), host=host))


@pytest.mark.parametrize("header", FORWARD_HEADERS)
def test_loopback_plus_any_forward_header_is_external(header):
    assert not is_local(make_request([(header, "1.2.3.4")]))
    assert not is_local(make_request([(header.upper(), "1.2.3.4")]))  # 大小寫不影響
    assert not is_local(make_request([(header, "")]))  # 空值也算


def test_cloudflared_request_shape_is_external():
    req = make_request([("cf-connecting-ip", "203.0.113.9"), ("x-forwarded-for", "203.0.113.9"),
                        ("cf-ray", "abc"), ("x-forwarded-proto", "https")],
                       host="example.trycloudflare.com")
    assert not is_local(req)


@pytest.mark.parametrize("peer", ["10.0.0.5", "192.168.1.2", "203.0.113.1", "testclient", "", "::2",
                                  "0.0.0.0"])
def test_non_loopback_peer_is_external(peer):
    assert not is_local(make_request(client=(peer, 1)))


def test_missing_client_is_external():
    assert not is_local(make_request(client=None))


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8781", "127.0.0.1", "localhost",
                                  "127.0.0.1:8782", "localhost:80", "127.0.0.1.evil.example:8781",
                                  "user@127.0.0.1:8781", "127.0.0.1:8781/x", "127.0.0.1:8781, evil",
                                  "::1:8781", "[::1:8781", "127.0.0.1:abc", "127.0.0.1:99999", "",
                                  "0.0.0.0:8781", "127.0.0.1 :8781"])
def test_host_not_in_allowlist_is_rejected(host):
    assert not is_local(make_request(host=host))


def test_missing_or_duplicate_host_is_rejected():
    assert not is_local(make_request(host=None))
    assert not is_local(make_request([("host", "127.0.0.1:8781")]))  # 兩個 Host


def test_port_argument_overrides_server_port():
    req = make_request(host="localhost:9000", server=("127.0.0.1", 9000))
    assert is_local(req)
    assert not is_local(req, port=PORT)
    assert is_local(make_request(host="localhost:8781", server=None), port=PORT)
    assert not is_local(make_request(server=None))  # 不知道 port → 不算本機


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8781", "http://127.0.0.1:8781/"])
def test_same_local_origin_is_allowed(origin):
    assert is_local(make_request([("origin", origin)]))
    assert is_local(make_request([("origin", "http://localhost:8781")], host="localhost:8781"))
    assert is_local(make_request([("origin", "http://[::1]:8781")], host="[::1]:8781"))


@pytest.mark.parametrize("origin", ["https://evil", "http://evil.example:8781", "null", "",
                                    "https://127.0.0.1:8781", "http://127.0.0.1:8782",
                                    "http://127.0.0.1", "http://localhost:8781", "http://u@127.0.0.1:8781",
                                    "http://127.0.0.1:8781/path", "file://", "http://[::1]:8781"])
def test_foreign_origin_is_rejected(origin):
    assert not is_local(make_request([("origin", origin)]))


def test_localhost_host_with_evil_origin_is_rejected():
    assert not is_local(make_request([("origin", "https://evil")], host="localhost:8781"))


def test_duplicate_origin_is_rejected():
    req = make_request([("origin", "http://127.0.0.1:8781"), ("origin", "http://127.0.0.1:8781")])
    assert not is_local(req)


def test_sec_fetch_site():
    assert not is_local(make_request([("sec-fetch-site", "cross-site")]))
    assert not is_local(make_request([("sec-fetch-site", "Cross-Site")]))
    assert is_local(make_request([("sec-fetch-site", "same-origin")]))
    assert is_local(make_request([("sec-fetch-site", "none")]))


def test_websocket_scope_and_raw_scope_accepted():
    req = make_request(scope_type="websocket")
    assert is_local(req)
    assert is_local(req.scope)
    assert not is_local(make_request([("x-forwarded-for", "1.2.3.4")], scope_type="websocket"))
    with pytest.raises(TypeError):
        is_local(object())


def test_is_local_through_testclient():
    app = FastAPI()

    @app.get("/")
    def root(request: Request):
        return {"local": is_local(request)}

    local = TestClient(app, base_url="http://127.0.0.1:8781", client=("127.0.0.1", 5555))
    assert local.get("/").json() == {"local": True}
    assert local.get("/", headers={"cf-connecting-ip": "1.1.1.1"}).json() == {"local": False}
    assert local.get("/", headers={"host": "evil"}).json() == {"local": False}
    assert TestClient(app).get("/").json() == {"local": False}  # 預設 testclient／testserver


# --------------------------------------------------------------------------- check_public_host


ALLOWED = ["https://abc.trycloudflare.com", "llm.example.org", ""]


@pytest.mark.parametrize("host", ["abc.trycloudflare.com", "ABC.trycloudflare.com.", "abc.trycloudflare.com:443",
                                  "llm.example.org", "127.0.0.1:8782", "localhost", "[::1]:8782"])
def test_public_host_allowed(host):
    assert check_public_host(make_request(host=host), ALLOWED)


@pytest.mark.parametrize("host", ["evil.example", "abc.trycloudflare.com.evil.example", "trycloudflare.com",
                                  "x.abc.trycloudflare.com", "", "abc.trycloudflare.com/x",
                                  "user@llm.example.org"])
def test_public_host_rejected(host):
    assert not check_public_host(make_request(host=host), ALLOWED)


def test_public_host_missing_duplicate_and_forwarded_host():
    assert not check_public_host(make_request(host=None), ALLOWED)
    assert not check_public_host(make_request([("host", "evil")], host="llm.example.org"), ALLOWED)
    assert not check_public_host(make_request([("x-forwarded-host", "evil.example")], host="llm.example.org"),
                                 ALLOWED)
    assert check_public_host(make_request([("x-forwarded-host", "abc.trycloudflare.com")],
                                          host="llm.example.org"), ALLOWED)
    assert not check_public_host(make_request(host="llm.example.org"), [])  # 清單空 → 只剩 loopback


# --------------------------------------------------------------------------- GateMiddleware


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def gate_app(state_box, clock):
    inner = FastAPI()

    @inner.get("/v1/models")
    def models(request: Request):
        return {"path": request.url.path, "scope_path": request.scope["path"],
                "raw": request.scope.get("raw_path", b"").decode()}

    @inner.get("/")
    def root(request: Request):
        return {"path": request.scope["path"]}

    @inner.websocket("/worker/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"path": websocket.scope["path"]})
        await websocket.close()

    return GateMiddleware(inner, lambda: state_box[0], clock=clock)


def test_gate_passes_and_strips_path():
    clock = Clock()
    client = TestClient(gate_app([GateState(gate_hash("g4t3"))], clock))
    r = client.get("/g4t3/v1/models?x=1")
    assert r.status_code == 200
    assert r.json() == {"path": "/v1/models", "scope_path": "/v1/models", "raw": "/v1/models"}
    assert client.get("/g4t3").json() == {"path": "/"}
    assert client.get("/g4t3/").json() == {"path": "/"}


@pytest.mark.parametrize("path", ["/v1/models", "/wrong/v1/models", "/", "//g4t3/v1/models", "/G4T3/v1/models",
                                  "/g4t3x/v1/models", "/g4t/v1/models", "/v1/g4t3/models"])
def test_wrong_gate_is_404(path):
    client = TestClient(gate_app([GateState(gate_hash("g4t3"))], Clock()))
    r = client.get(path)
    assert r.status_code == 404
    assert "g4t3" not in r.text


def test_empty_or_missing_state_blocks_everything():
    box = [GateState("")]
    client = TestClient(gate_app(box, Clock()))
    assert client.get("/anything/v1/models").status_code == 404
    box[0] = None
    assert client.get("/anything/v1/models").status_code == 404


def test_old_gate_works_only_during_grace():
    clock = Clock(1000.0)
    box = [GateState(gate_hash("new"), previous_hash=gate_hash("old"), grace_until=1600.0)]
    client = TestClient(gate_app(box, clock))
    assert client.get("/new/v1/models").status_code == 200
    assert client.get("/old/v1/models").status_code == 200
    clock.t = 1599.9
    assert client.get("/old/v1/models").status_code == 200
    clock.t = 1600.0
    assert client.get("/old/v1/models").status_code == 404
    assert client.get("/new/v1/models").status_code == 200


def test_rotation_without_grace_kills_old_gate_immediately():
    box = [GateState(gate_hash("one"))]
    client = TestClient(gate_app(box, Clock()))
    assert client.get("/one/v1/models").status_code == 200
    box[0] = GateState(gate_hash("two"), previous_hash=gate_hash("one"), grace_until=0.0)
    assert client.get("/one/v1/models").status_code == 404
    assert client.get("/two/v1/models").status_code == 200


def test_gate_websocket():
    client = TestClient(gate_app([GateState(gate_hash("g4t3"))], Clock()))
    with client.websocket_connect("/g4t3/worker/ws") as ws:
        assert ws.receive_json() == {"path": "/worker/ws"}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/bad/worker/ws"):
            pass


async def test_gate_websocket_http_response_extension():
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "websocket.connect"}

    async def app(scope, receive, send):  # pragma: no cover - 不應被呼叫
        raise AssertionError("不該到這裡")

    mw = GateMiddleware(app, lambda: GateState(gate_hash("g")))
    await mw({"type": "websocket", "path": "/x/worker/ws", "extensions": {"websocket.http.response": {}}},
             receive, send)
    assert sent[0]["type"] == "websocket.http.response.start" and sent[0]["status"] == 404


async def test_gate_lifespan_passes_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await GateMiddleware(app, lambda: None)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_gate_state_allows():
    s = GateState(gate_hash("a"), previous_hash=gate_hash("b"), grace_until=10)
    assert s.allows("a", 0) and s.allows("b", 9.9)
    assert not s.allows("b", 10) and not s.allows("", 0) and not s.allows("c", 0)
    assert gate_hash("a") == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"


# --------------------------------------------------------------------------- log 清理


def test_redact_path():
    assert redact_path("/s3cr3t/v1/chat/completions") == "/***/v1/chat/completions"
    assert redact_path("/s3cr3t/v1/models?api_key=sk-llop-abcdefghijk") == "/***/v1/models?***"
    assert redact_path("/s3cr3t") == "/***"
    assert redact_path("/") == "/"
    assert redact_path("") == ""
    assert "s3cr3t" not in redact_path("/s3cr3t/worker/ws")
    assert "\n" not in redact_path("/g/v1\nFAKE LOG LINE")


def test_clean_log_escapes_control_characters():
    out = clean_log("alice\r\n2026-01-01 INFO admin logged in\x1b[31m‮evil x\x00")
    assert "\n" not in out and "\r" not in out and "\x1b" not in out
    assert "‮" not in out and " " not in out and "\x00" not in out
    assert "\\x0d\\x0a" in out and "\\u202e" in out
    assert clean_log("中文 名字 ok") == "中文 名字 ok"


def test_clean_log_masks_secrets_and_truncates():
    out = clean_log("key=sk-llop-AAAAAAAAAAAAAAAAAAAA auth=Bearer abcdefghijklmnop")
    assert "AAAAAAAA" not in out and "abcdefghijklmnop" not in out
    assert "sk-***" in out and "Bearer ***" in out
    long = clean_log("x" * 1000)
    assert len(long) == 200 and long.endswith("…")
    assert clean_log("x" * 1000, max_len=0) == "x" * 1000
    assert clean_log(12345) == "12345"
    assert clean_log(None) == "None"


def test_module_is_single_source_of_forward_headers():
    assert set(FORWARD_HEADERS) == {"cf-connecting-ip", "cf-ray", "cf-visitor", "cdn-loop", "x-forwarded-for",
                                    "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded", "via"}
    assert netguard.LOOPBACK_NAMES == {"127.0.0.1", "localhost", "::1"}
