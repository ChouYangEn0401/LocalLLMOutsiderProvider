"""加密面（llmop.secure）測試共用的 fixture。

由 B 的 ``tests/gateway/conftest.py`` 移植：閘道改成 A 的 FastAPI app 掛上 ``build_secure_router``，
在 loopback 上用真的 uvicorn 跑；用戶端與假 worker 用 aiohttp（假 worker 以 llmop.crypto 實作，
跟 B 的一樣會驗簽、解開請求、把 body 封裝回去）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import uvicorn
from aiohttp import WSMsgType
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI

from llmop import crypto, protocol
from llmop.config import parse_config
from llmop.protocol import Envelope, b64u_decode, b64u_encode
from llmop.quota import QuotaManager
from llmop.scheduler import Scheduler
from llmop.secure import api as secure_api
from llmop.secure import build_secure_router
from llmop.secure.hub import Hub
from llmop.store import Store
from llmop.usage import UsageManager

DEFAULT_USAGE = (7, 11)
PASSWORD_HASH = "scrypt$not-used-in-these-tests"


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
    """以 WebSocket 連到閘道的假 worker（與 B 的相同）。

    預設會驗簽、解開請求、把 body 原樣封裝回去。silent=True 時只記錄收到的請求、不回覆，
    可以之後用 answer() 手動回覆；error_code 有值時回覆裸錯誤碼。
    """

    ws: Any
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


async def wait_until(predicate, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@dataclass
class Gateway:
    http: aiohttp.ClientSession
    base: str
    store: Store
    state: SimpleNamespace
    router: Any

    @property
    def hub(self) -> Hub:
        return self.router.secure.hub

    @property
    def config(self):
        return self.state.config

    def new_api_client(self, username: str | None = "alice", *, signing: bool = True, guest: bool = False,
                       **key_args) -> ApiClient:
        signing_key = crypto.generate_signing_key()
        sig = b64u_encode(crypto.public_raw(signing_key)) if signing else None
        if guest:
            secret, key = self.store.create_guest_key("訪客", sig_public_key=sig, **key_args)
        else:
            secret, key = self.store.create_api_key(username, "測試", sig_public_key=sig, **key_args)
        return ApiClient(api_key=secret, key_id=key.id, signing_key=signing_key)

    def ws_connect(self, path: str = "/worker/ws", **kwargs):
        return self.http.ws_connect(self.base + path, **kwargs)

    async def register(self, name: str = "worker") -> tuple[Any, str, str]:
        """註冊一台新 worker（register → registered），回傳 (ws, worker_id, worker_token)。"""
        _, reg_token = self.store.create_worker(name)
        ws = await self.ws_connect()
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
        ws = await self.ws_connect()
        await ws.send_json(hello_frame(worker.worker_id, worker.worker_token, [worker.worker_key], models,
                                       max_concurrency))
        assert (await ws.receive_json(timeout=5)) == {"type": "ready"}
        new = FakeWorker(ws, worker.worker_id, worker.worker_token, worker.worker_key, **behaviour)
        new.start()
        return new

    async def post(self, api: ApiClient, envelope: Envelope) -> tuple[int, dict]:
        async with self.http.post(self.base + "/e2e/v1/chat/completions", json=envelope.to_json(),
                                  headers=api.headers) as resp:
            return resp.status, await resp.json()

    async def post_raw(self, data=None, json_body=None, headers=None):
        async with self.http.post(self.base + "/e2e/v1/chat/completions", data=data, json=json_body,
                                  headers=headers or {}) as resp:
            return resp.status, await resp.json(), dict(resp.headers)

    async def get(self, path: str, headers=None) -> tuple[int, dict]:
        async with self.http.get(self.base + path, headers=headers or {}) as resp:
            return resp.status, await resp.json()

    async def poll(self, api: ApiClient, request_id: str) -> tuple[int, dict]:
        return await self.get(f"/e2e/v1/requests/{request_id}", api.headers)

    def usage_events(self, key_id: str | None = None) -> list[dict]:
        rows = self.store._all("SELECT * FROM usage_events ORDER BY id")
        return [dict(r) for r in rows if key_id is None or r["key_id"] == key_id]


def make_config(*, quota: dict | None = None, tier: str = "normal", queue: dict | None = None,
                usage: dict | None = None, tiers: dict | None = None, disabled: bool = False):
    raw: dict = {
        # A 的舊版預設同時上限是 2；這裡關掉，讓排程器（T-3）負責同時與排隊。
        "default_quota": {"max_concurrent": 0, **(quota or {})},
        "accounts": {
            "alice": {"password_hash": PASSWORD_HASH, "tier": tier, "disabled": disabled},
            "bob": {"password_hash": PASSWORD_HASH, "tier": tier},
        },
    }
    if queue:
        raw["queue"] = queue
    if usage:
        raw["usage"] = usage
    if tiers:
        raw["tiers"] = tiers
    return parse_config(raw)


async def start_server(app: FastAPI) -> tuple[uvicorn.Server, asyncio.Task, int]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="on"))
    server.install_signal_handlers = lambda: None  # 舊版 uvicorn；新版 serve() 不在測試裡攔訊號
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started or task.done(), timeout=10)
    if task.done():
        task.result()
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


@pytest.fixture
async def make_gateway(tmp_path):
    """factory(**options) → Gateway。options：sync_wait_seconds、request_timeout_seconds、
    scheduler（傳給 Scheduler 的參數）、tables（是否用用量表），其餘傳給 make_config。"""
    started: list[tuple[uvicorn.Server, asyncio.Task, aiohttp.ClientSession, Store]] = []

    async def factory(*, sync_wait_seconds: float = 80, request_timeout_seconds: float = 1800,
                      scheduler: dict | None = None, tables: bool = True, **cfg_args) -> Gateway:
        cfg = make_config(**cfg_args)
        store = Store(tmp_path / f"llmop-{len(started)}.sqlite3")
        clock = lambda: secure_api._now()  # noqa: E731  跟著 _now 的替身走（換日測試）
        usage = UsageManager(store, cfg, clock=clock) if tables else None
        state = SimpleNamespace(
            store=store,
            config=cfg,
            usage=usage,
            quota=QuotaManager(store, clock=clock, tables=usage),
            scheduler=Scheduler(**(scheduler or {})),
        )
        router = build_secure_router(state, sync_wait_seconds=sync_wait_seconds,
                                     request_timeout_seconds=request_timeout_seconds)
        app = FastAPI()
        app.include_router(router)
        server, task, port = await start_server(app)
        http = aiohttp.ClientSession()
        started.append((server, task, http, store))
        return Gateway(http=http, base=f"http://127.0.0.1:{port}", store=store, state=state, router=router)

    yield factory

    for server, task, http, store in started:
        await http.close()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        store.close()


@pytest.fixture
async def gateway(make_gateway) -> Gateway:
    return await make_gateway()
