"""測試工具本身的測試：確認錄音轉送器真的抓得到明文。

「閘道收送的位元組裡沒有標記字串」這種否定式的斷言，只有在錄音確實看得到內容時才有意義。
worker 送出的 WebSocket 訊框有遮罩（也可能被壓縮），原始位元組裡直接搜尋會漏掉，
所以這裡用三種方式各送一次標記：HTTP 本文、有遮罩的訊框、有遮罩又壓縮的訊框。
v0.2：探針走跟 worker 一樣的路徑（``/<gate>/worker/ws``），閘道是統一節點的 uvicorn。
"""

from __future__ import annotations

import json

import aiohttp

from llmop import protocol

from .harness import MODELS, FakeCloudflared


async def test_relay_sees_markers_in_http_and_in_masked_or_compressed_ws_frames(make_stack):
    stack = await make_stack()
    assert (await stack.chat(MODELS["ollama"]))[0] == 200
    markers = {"http": "PROBE-HTTP-5b1e", "masked": "PROBE-MASKED-8c2d", "deflate": "PROBE-DEFLATE-1f7a"}

    async with aiohttp.ClientSession() as session:
        async with session.post(stack.tunnel_url + "/e2e/v1/chat/completions", data=markers["http"],
                                headers=stack.client.auth) as resp:
            assert resp.status == 400
        for name, compress in (("masked", 0), ("deflate", 15)):
            url = f"ws://127.0.0.1:{stack.relay.port}/{stack.gate}/worker/ws"
            async with session.ws_connect(url, compress=compress) as ws:
                await ws.send_str(json.dumps(protocol.register(markers[name] * 20)))
                assert json.loads((await ws.receive(timeout=5)).data) == protocol.error("unauthorized")

    ws_conns = [c for c in stack.relay.connections if c.is_websocket]
    assert len(ws_conns) == 3
    # 第一條是 worker 自己的連線；它的訊框有遮罩，但沒有協商壓縮。
    # 第二條探針沿用了先前 HTTP 請求的 keep-alive 連線才升級，錄音也要能處理。
    assert [c.compressed for c in ws_conns] == [False, False, True]
    method, path = ws_conns[1].http_exchanges()[0][0].method_path
    assert method == "POST" and path.endswith("/e2e/v1/chat/completions")
    for marker in markers.values():
        assert any(marker.encode() in blob for blob in stack.relay.blobs()), marker
    for name in ("masked", "deflate"):
        # 原始位元組裡找不到（遮罩或壓縮過），解框之後才找得到。
        assert not any(markers[name].encode() in bytes(c.to_gateway) for c in ws_conns)
        assert any(markers[name].encode() in p for c in ws_conns for p in c.ws_payloads())


async def test_fake_cloudflared_adds_forwarding_headers(make_stack, aiohttp_server):
    """假 cloudflared 真的加上了轉送標頭與公開的 Host（否則「被拒絕」的測試沒有意義）。"""
    from aiohttp import web

    seen: list[dict] = []

    async def echo(request: web.Request) -> web.Response:
        seen.append(dict(request.headers))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", echo)
    server = await aiohttp_server(app)
    stack = await make_stack()
    cf: FakeCloudflared = await stack.start_cloudflared(server.port, "ui-e2e.trycloudflare.com")
    async with stack.http.post(cf.url + "/ui/api/x?y=1", json={"a": 1}, headers={"Cookie": "c=1"}) as resp:
        assert resp.status == 200
    (headers,) = seen
    assert headers["Host"] == "ui-e2e.trycloudflare.com"
    assert headers["Cf-Connecting-Ip"] == "203.0.113.7"
    assert headers["X-Forwarded-For"] == "203.0.113.7"
    assert headers["X-Forwarded-Proto"] == "https"
    assert headers["Cookie"] == "c=1"
    assert cf.seen == [("POST", "/ui/api/x", 200)]
