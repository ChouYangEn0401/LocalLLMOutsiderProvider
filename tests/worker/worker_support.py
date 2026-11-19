"""worker 測試共用的假伺服器與小工具（假閘道、假 Ollama、假 OpenAI 相容後端）。"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from llmop import protocol
from llmop.crypto import public_raw, seal_request
from llmop.protocol import b64u_encode

PROMPT = "PLAINTEXT-PROMPT-7f3a9c 請幫我寫一首詩"
REPLY = "PLAINTEXT-REPLY-c41d02 春眠不覺曉"
MODEL = "qwen3:8b"


def sign_and_seal(worker_key, signing_key, *, body=None, model=MODEL, max_tokens=64, now=None):
    """以 llmop.crypto.seal_request 封裝一筆請求，回傳 (envelope, opener)。worker_key 是 WorkerKey。"""
    if body is None:
        body = {"messages": [{"role": "user", "content": PROMPT}]}
    worker_public = public_raw(worker_key.private_key)
    return seal_request(
        body, model=model, max_tokens=max_tokens, worker_public=worker_public, signing_key=signing_key, now=now
    )


def request_frame(envelope, signing_key, *, client_pub=None) -> dict:
    """閘道轉送給 worker 的 request 訊框（經過 parse_frame 檢查）。"""
    pub = client_pub if client_pub is not None else public_raw(signing_key)
    frame = protocol.request(envelope.header.request_id, envelope, b64u_encode(pub))
    return protocol.parse_frame(json.dumps(frame))


class FakeLLM:
    """假的本機 LLM 伺服器；kind 是 "ollama" 或 "openai"。記錄每次 chat 呼叫與同時進行的數量。"""

    def __init__(self, kind: str, models=(MODEL,)) -> None:
        self.kind = kind
        self.models = list(models)
        self.calls: list[dict] = []
        self.headers: list[dict] = []
        self.status = 200
        self.reply: dict | None = None  # 指定時直接回這個 JSON
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        self.spans: list[tuple[float, float]] = []
        self.base_url = ""

    def app(self) -> web.Application:
        app = web.Application()
        if self.kind == "ollama":
            app.router.add_get("/api/tags", self._list)
            app.router.add_post("/api/chat", self._chat)
        else:
            app.router.add_get("/v1/models", self._list)
            app.router.add_post("/v1/chat/completions", self._chat)
        return app

    async def _list(self, request: web.Request) -> web.Response:
        if self.kind == "ollama":
            return web.json_response({"models": [{"name": m, "model": m} for m in self.models]})
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})

    async def _chat(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.calls.append(payload)
        self.headers.append(dict(request.headers))
        loop = asyncio.get_running_loop()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        start = loop.time()
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
            self.spans.append((start, loop.time()))
        if self.status != 200:
            # 故意把提示內容放進錯誤訊息，用來確認它不會出現在紀錄或明文訊框裡。
            text = json.dumps(payload["messages"], ensure_ascii=False)
            error = {"error": f"boom {text}"} if self.kind == "ollama" else {"error": {"message": f"boom {text}"}}
            return web.json_response(error, status=self.status)
        if self.reply is not None:
            return web.json_response(self.reply)
        if self.kind == "ollama":
            return web.json_response(
                {
                    "model": payload["model"],
                    "created_at": "2026-11-18T00:00:00Z",
                    "message": {"role": "assistant", "content": REPLY},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 12,
                    "eval_count": 7,
                }
            )
        return web.json_response(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            }
        )


class FakeGateway:
    """假的閘道 /worker/ws：處理 register 與 hello，轉送 request，收集 worker 送來的每個訊框。"""

    def __init__(self) -> None:
        self.frames: asyncio.Queue = asyncio.Queue()  # worker 送來的訊框（都已通過 parse_frame）
        self.connections = 0
        self.hello_reply = protocol.ready()
        self.drop_after_ready = False  # 下一次握手成功後立刻斷線
        self.worker_id = "w_0123456789ab"
        self.worker_token = "llmop_wk_" + "A" * 43
        self.ws: web.WebSocketResponse | None = None
        self.url = ""
        self._pending: dict[str, asyncio.Future] = {}

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/worker/ws", self._handle)
        return app

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        async for msg in ws:
            frame = protocol.parse_frame(msg.data)
            await self.frames.put(frame)
            kind = frame["type"]
            if kind == "register":
                await ws.send_json(protocol.registered(self.worker_id, self.worker_token))
            elif kind == "hello":
                await ws.send_json(self.hello_reply)
                if self.hello_reply["type"] == "error" or self.drop_after_ready:
                    self.drop_after_ready = False
                    await ws.close()
                    break
                self.ws = ws
            elif kind == "response":
                future = self._pending.pop(frame["request_id"], None)
                if future is not None and not future.done():
                    future.set_result(frame)
        if self.ws is ws:
            self.ws = None
        return ws

    async def next_frame(self, kind: str | None = None, timeout: float = 5.0) -> dict:
        while True:
            frame = await asyncio.wait_for(self.frames.get(), timeout)
            if kind is None or frame["type"] == kind:
                return frame

    async def wait_hello(self, timeout: float = 5.0) -> dict:
        frame = await self.next_frame("hello", timeout)
        # ready 已經送出；等 worker 進入服務迴圈之前送出的請求也會留在緩衝區裡。
        while self.ws is None:
            await asyncio.sleep(0.01)
        return frame

    async def relay(self, envelope, signing_key, *, timeout: float = 10.0) -> dict:
        """像閘道一樣轉送請求，等待並回傳 worker 的 response 訊框。"""
        request_id = envelope.header.request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.ws.send_json(protocol.request(request_id, envelope, b64u_encode(public_raw(signing_key))))
        return await asyncio.wait_for(future, timeout)
