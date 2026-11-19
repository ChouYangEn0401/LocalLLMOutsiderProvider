"""閘道測試共用的 fixture：臨時 LLMOP_HOME、測試用閘道，以及用 llmop.crypto 實作的假 worker。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from llmop import crypto, protocol
from llmop.gateway import HUB
from llmop.gateway.app import build_app
from llmop.gateway.config import GatewayConfig
from llmop.gateway.hub import Hub
from llmop.gateway.store import Store
from llmop.protocol import Envelope, b64u_decode, b64u_encode

ADMIN_PASSWORD = "correct horse battery staple"
DEFAULT_USAGE = (7, 11)


@pytest.fixture
def llmop_home(tmp_path, monkeypatch):
    home = tmp_path / "llmop-home"
    monkeypatch.setenv("LLMOP_HOME", str(home))
    return home


@dataclass
class ApiClient:
    """一把 API 金鑰與它登記的簽章私鑰。"""

    api_key: str
    key_id: str
    signing_key: Ed25519PrivateKey

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def seal(self, worker_public: bytes, *, model: str = "m1", max_tokens: int = 16, body: dict | None = None,
             now: int | None = None) -> tuple[Envelope, crypto.ReplyOpener]:
        body = body if body is not None else {"messages": [{"role": "user", "content": "秘密的問題"}]}
        return crypto.seal_request(
            body, model=model, max_tokens=max_tokens, worker_public=worker_public,
            signing_key=self.signing_key, now=now,
        )


@dataclass(eq=False)
class FakeWorker:
    """以 WebSocket 連到閘道的假 worker。

    預設會驗簽、解開請求、把 body 原樣封裝回去。silent=True 時只記錄收到的請求、不回覆，
    可以之後用 answer() 手動回覆；error_code 有值時回覆裸錯誤碼。
    """

    ws: object
    worker_id: str
    worker_token: str
    worker_key: X25519PrivateKey
    silent: bool = False
    error_code: str | None = None
    usage: tuple[int, int] = DEFAULT_USAGE
    received: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None

    @property
    def public(self) -> bytes:
        return crypto.public_raw(self.worker_key)

    @property
    def key_id(self) -> str:
        return crypto.worker_key_id(self.public)

    def start(self) -> None:
        self.task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            if frame["type"] != "request":
                continue
            self.received.append(frame)
            if not self.silent:
                await self.answer(frame)

    async def answer(self, frame: dict) -> None:
        if self.error_code is not None:
            await self.ws.send_json(protocol.response_error(frame["request_id"], self.error_code))
            return
        envelope = Envelope.from_json(frame["envelope"])
        crypto.verify_request(envelope, b64u_decode(frame["client_sig_pub"]))
        body, sealer = crypto.open_request(envelope, self.worker_key)
        sealed = sealer.seal({"ok": True, "body": {"object": "chat.completion", "echo": body}})
        await self.ws.send_json(protocol.response_ok(frame["request_id"], sealed, *self.usage))

    async def wait_received(self, count: int, timeout: float = 5) -> None:
        async with asyncio.timeout(timeout):
            while len(self.received) < count:
                await asyncio.sleep(0.01)

    async def close(self) -> None:
        await self.ws.close()
        if self.task is not None:
            await self.task


def hello_frame(worker_id: str, worker_token: str, keys: list[X25519PrivateKey], models, max_concurrency=2) -> dict:
    entries = [
        {"key_id": crypto.worker_key_id(crypto.public_raw(k)), "public_key": b64u_encode(crypto.public_raw(k))}
        for k in keys
    ]
    return protocol.hello(worker_id, worker_token, entries, list(models), max_concurrency)


@dataclass
class Gateway:
    client: TestClient
    store: Store
    config: GatewayConfig

    @property
    def hub(self) -> Hub:
        return self.client.server.app[HUB]

    def new_api_client(self, *, request_limit: int | None = None, token_limit: int | None = None) -> ApiClient:
        signing_key = crypto.generate_signing_key()
        key, secret = self.store.create_api_key(
            "測試", sig_public_key=b64u_encode(crypto.public_raw(signing_key)),
            daily_request_limit=request_limit, daily_token_limit=token_limit,
        )
        return ApiClient(api_key=secret, key_id=key.id, signing_key=signing_key)

    async def register(self, name: str = "worker", **hello_args) -> tuple[object, str, str]:
        """註冊一台新 worker（register → registered），回傳 (ws, worker_id, worker_token)。"""
        _, reg_token = self.store.create_worker(name)
        ws = await self.client.ws_connect("/worker/ws")
        await ws.send_json(protocol.register(reg_token))
        frame = await ws.receive_json(timeout=5)
        assert frame["type"] == "registered", frame
        return ws, frame["worker_id"], frame["worker_token"]

    async def connect_worker(self, *, models=("m1",), max_concurrency: int = 2, name: str = "worker",
                             worker_key: X25519PrivateKey | None = None, **behaviour) -> FakeWorker:
        """註冊並完成 hello，回傳已開始服務的假 worker。"""
        worker_key = worker_key or crypto.generate_worker_key()
        ws, worker_id, worker_token = await self.register(name)
        await ws.send_json(hello_frame(worker_id, worker_token, [worker_key], models, max_concurrency))
        assert (await ws.receive_json(timeout=5)) == {"type": "ready"}
        worker = FakeWorker(ws, worker_id, worker_token, worker_key, **behaviour)
        worker.start()
        return worker

    async def reconnect(self, worker: FakeWorker, *, models=("m1",), max_concurrency: int = 2,
                        **behaviour) -> FakeWorker:
        """用同一組 worker_id 與權杖開第二條連線。"""
        ws = await self.client.ws_connect("/worker/ws")
        await ws.send_json(hello_frame(worker.worker_id, worker.worker_token, [worker.worker_key], models,
                                       max_concurrency))
        assert (await ws.receive_json(timeout=5)) == {"type": "ready"}
        new = FakeWorker(ws, worker.worker_id, worker.worker_token, worker.worker_key, **behaviour)
        new.start()
        return new

    async def post(self, api: ApiClient, envelope: Envelope) -> tuple[int, dict]:
        resp = await self.client.post("/v1/chat/completions", json=envelope.to_json(), headers=api.headers)
        return resp.status, await resp.json()

    async def poll(self, api: ApiClient, request_id: str) -> tuple[int, dict]:
        resp = await self.client.get(f"/v1/requests/{request_id}", headers=api.headers)
        return resp.status, await resp.json()


@pytest.fixture
def make_gateway(aiohttp_client, llmop_home):
    async def factory(**overrides) -> Gateway:
        config = GatewayConfig(**overrides)
        store = Store.open_default()
        store.set_admin_password(ADMIN_PASSWORD)
        app = build_app(config, store)

        async def close_store(app) -> None:
            store.close()

        app.on_cleanup.append(close_store)
        client = await aiohttp_client(app)
        return Gateway(client=client, store=store, config=config)

    return factory


@pytest.fixture
async def gateway(make_gateway) -> Gateway:
    return await make_gateway()
