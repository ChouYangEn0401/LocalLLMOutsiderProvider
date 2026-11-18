"""測試用的假閘道：持有 worker 私鑰，用 llmop.crypto 驗簽、解開請求並封裝回覆。

它同時扮演閘道與 worker，讓用戶端的測試不需要真的閘道或後端。
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable

from aiohttp import web

from llmop import crypto
from llmop.protocol import HTTP_STATUS, Envelope, Header, ProtocolError, b64u_decode, b64u_encode

API_KEY = "llmop_sk_" + "A" * 43
PROMPT = "請只回答秘密代號：藍色鸚鵡"
REPLY_TEXT = "秘密回覆：紫色大象"


def completion(header: Header, message: dict | None = None, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 1795000000,
        "model": header.model,
        "choices": [
            {
                "index": 0,
                "message": message or {"role": "assistant", "content": REPLY_TEXT},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    }


def ok_reply(body: dict, header: Header) -> dict:
    return {"ok": True, "body": completion(header)}


class FakeGateway:
    def __init__(self, client_sig_pub: bytes, model: str = "qwen3:8b") -> None:
        self.client_sig_pub = client_sig_pub
        self.model = model
        self.worker_key = crypto.generate_worker_key()
        self.public_key = b64u_encode(crypto.public_raw(self.worker_key))
        self.key_id = crypto.worker_key_id(crypto.public_raw(self.worker_key))
        self.models = [self.entry(model)]
        # 行為設定
        self.reply: Callable[[dict, Header], dict] = ok_reply
        self.pending_polls = 0  # 完成前先回幾次 202
        self.delay = 0.0  # POST 回應前等多久
        self.poll_delay = 0.0  # 每次輪詢回應前等多久（模擬長輪詢）
        self.gateway_error: str | None = None  # 直接回這個閘道錯誤碼
        self.wrong_request = False  # 用錯的 request_hash 封裝回覆
        self.drop_post = None  # "before"：POST 沒處理就回非 JSON 502；"after"：處理完才回
        self.flaky_polls = 0  # 前幾次輪詢回非 JSON 502
        # 記錄
        self.received: list[tuple[Header, dict]] = []
        self.posts = 0
        self.polls = 0
        self._results: dict[str, str] = {}
        self._pending: dict[str, int] = {}

    def entry(self, model: str, public_key: str | None = None, key_id: str | None = None) -> dict:
        public_key = public_key or self.public_key
        if key_id is None:
            key_id = crypto.worker_key_id(b64u_decode(public_key))
        return {"id": model, "object": "model", "owned_by": "fake", "llmop": {"key_id": key_id, "public_key": public_key}}

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/chat/completions", self._post)
        app.router.add_get("/v1/requests/{request_id}", self._poll)
        return app

    @staticmethod
    def _error(code: str, request_id: str | None = None) -> web.Response:
        body = {"status": "error", "error": {"code": code, "message": f"假閘道錯誤 {code}"}}
        if request_id:
            body["request_id"] = request_id
        return web.json_response(body, status=HTTP_STATUS[code])

    def _authorized(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {API_KEY}"

    async def _models(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._error("unauthorized")
        return web.json_response({"object": "list", "data": self.models})

    async def _post(self, request: web.Request) -> web.Response:
        self.posts += 1
        if self.drop_post == "before":
            return web.Response(status=502, text="bad gateway")
        if not self._authorized(request):
            return self._error("unauthorized")
        try:
            envelope = Envelope.from_json(await request.json())
            header = crypto.verify_request(envelope, self.client_sig_pub)
            body, sealer = crypto.open_request(envelope, self.worker_key)
        except ProtocolError as exc:
            return self._error(exc.code)
        self.received.append((header, body))
        rid = header.request_id
        if self.gateway_error:
            return self._error(self.gateway_error, rid)
        if self.wrong_request:
            sealer = dataclasses.replace(sealer, request_hash=bytes(32))
        self._results[rid] = sealer.seal(self.reply(body, header))
        self._pending[rid] = self.pending_polls
        await asyncio.sleep(self.delay)
        if self.drop_post == "after":
            return web.Response(status=502, text="bad gateway")
        return self._answer(rid)

    async def _poll(self, request: web.Request) -> web.Response:
        self.polls += 1
        rid = request.match_info["request_id"]
        await asyncio.sleep(self.poll_delay)
        if self.flaky_polls > 0:
            self.flaky_polls -= 1
            return web.Response(status=502, text="bad gateway")
        if not self._authorized(request) or rid not in self._results:
            return self._error("unknown_request", rid)
        if self._pending[rid] > 0:
            self._pending[rid] -= 1
        return self._answer(rid)

    def _answer(self, rid: str) -> web.Response:
        if self._pending[rid] > 0:
            return web.json_response({"request_id": rid, "status": "pending"}, status=202)
        return web.json_response({"request_id": rid, "status": "done", "sealed": self._results[rid]})
