"""測試工具本身的測試：確認錄音轉送器真的抓得到明文。

「閘道收送的位元組裡沒有標記字串」這種否定式的斷言，只有在錄音確實看得到內容時才有意義。
worker 送出的 WebSocket 訊框有遮罩（也可能被壓縮），原始位元組裡直接搜尋會漏掉，
所以這裡用三種方式各送一次標記：HTTP 本文、有遮罩的訊框、有遮罩又壓縮的訊框。
"""

from __future__ import annotations

import json

import aiohttp

from llmop import protocol

from .harness import MODELS


async def test_relay_sees_markers_in_http_and_in_masked_or_compressed_ws_frames(make_stack):
    stack = await make_stack()
    assert (await stack.chat(MODELS["ollama"]))[0] == 200
    markers = {"http": "PROBE-HTTP-5b1e", "masked": "PROBE-MASKED-8c2d", "deflate": "PROBE-DEFLATE-1f7a"}

    async with aiohttp.ClientSession() as session:
        async with session.post(stack.tunnel_url + "/v1/chat/completions", data=markers["http"]) as resp:
            assert resp.status == 400
        for name, compress in (("masked", 0), ("deflate", 15)):
            url = f"ws://127.0.0.1:{stack.relay.port}/worker/ws"
            async with session.ws_connect(url, compress=compress) as ws:
                await ws.send_str(json.dumps(protocol.register(markers[name] * 20)))
                assert json.loads((await ws.receive(timeout=5)).data) == protocol.error("unauthorized")

    ws_conns = [c for c in stack.relay.connections if c.is_websocket]
    assert len(ws_conns) == 3
    # 第一條是 worker 自己的連線；它的訊框有遮罩，但沒有協商壓縮。
    # 第二條探針沿用了先前 HTTP 請求的 keep-alive 連線才升級，錄音也要能處理。
    assert [c.compressed for c in ws_conns] == [False, False, True]
    assert ws_conns[1].http_exchanges()[0][0].method_path == ("POST", "/v1/chat/completions")
    for marker in markers.values():
        assert any(marker.encode() in blob for blob in stack.relay.blobs()), marker
    for name in ("masked", "deflate"):
        # 原始位元組裡找不到（遮罩或壓縮過），解框之後才找得到。
        assert not any(markers[name].encode() in bytes(c.to_gateway) for c in ws_conns)
        assert any(markers[name].encode() in p for c in ws_conns for p in c.ws_payloads())
