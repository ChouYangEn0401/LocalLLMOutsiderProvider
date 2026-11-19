"""與閘道之間的 WebSocket 連線：註冊、hello、接收請求、定期更新模型清單、斷線重連。

worker 只主動撥出連線，不開任何監聽 socket，所以在 NAT 後面也能運作。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from llmop import protocol
from llmop.protocol import MAX_BODY_BYTES, WS_HEARTBEAT_SECONDS, ProtocolError, b64u_decode, loads_strict
from llmop.worker.backends import Backend, collect_models, make_backend
from llmop.worker.config import WorkerConfig
from llmop.worker.handler import Handler
from llmop.worker.keystore import KeyStore, WorkerState, load_state, save_state
from llmop.worker.replay import ReplayGuard

log = logging.getLogger(__name__)

MODEL_REFRESH_SECONDS = 60.0
HANDSHAKE_TIMEOUT = 30.0  # 等待 registered／ready 的上限
CONNECT_TIMEOUT = 30.0


class LinkStopped(Exception):
    """閘道明確拒絕了這台 worker（unauthorized）或缺少註冊所需的資料；重試沒有意義，worker 應停止。"""


class HandshakeFailed(Exception):
    """握手沒有完成，但可能只是暫時的問題，稍後重連。"""


_UNAUTHORIZED = {
    "register": "閘道拒絕了註冊權杖（可能已使用過或已過期）。請在閘道管理介面產生新的權杖，再以 --reg-token 執行。",
    "hello": "閘道拒絕了這台 worker 的憑證。如果 worker 已在閘道被刪除，請刪除 state.json 並以新的 --reg-token 重新註冊。",
}

_RETRYABLE = (aiohttp.ClientError, OSError, asyncio.TimeoutError, ProtocolError, HandshakeFailed)


@dataclass
class Link:
    config: WorkerConfig
    keys: KeyStore
    state_path: Path
    handler: Handler
    backends: list[Backend]
    reg_token: str | None = None
    refresh_seconds: float = MODEL_REFRESH_SECONDS
    min_backoff: float = 1.0
    max_backoff: float = 60.0
    _announced: list[str] = field(default_factory=list)  # 閘道目前知道的模型清單
    _warned: set[str] = field(default_factory=set)  # 已警告過的重複模型名稱

    async def run(self, session: aiohttp.ClientSession) -> None:
        """連線並服務，斷線就以指數退避（加隨機抖動）重連；只有 LinkStopped 會讓它結束。"""
        delay = self.min_backoff
        while True:
            try:
                await self._connect(session)
                delay = self.min_backoff  # 曾經握手成功，從頭開始退避
            except _RETRYABLE as exc:
                log.warning("與閘道的連線失敗：%s", _describe(exc))
            # 抖動讓多台 worker 不會同時湧回閘道；等待時間落在 [min_backoff, max_backoff]。
            wait = max(self.min_backoff, random.uniform(delay / 2, delay))
            log.info("%.1f 秒後重新連線", wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, self.max_backoff)

    async def _connect(self, session: aiohttp.ClientSession) -> None:
        # 先問好後端的模型再連線：閘道限定第一個訊框要在 WS_FIRST_FRAME_SECONDS 內到達，
        # 不能讓緩慢的後端拖到 hello。
        models = await self.refresh_routes()
        async with session.ws_connect(
            self.config.gateway_url, heartbeat=WS_HEARTBEAT_SECONDS, max_msg_size=MAX_BODY_BYTES
        ) as ws:
            await self._handshake(ws, models)
            await self._serve(ws)
        log.warning("與閘道的連線已中斷（close code %s）", ws.close_code)

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse, models: list[str]) -> None:
        state = load_state(self.state_path)
        if state is None:
            if not self.reg_token:
                raise LinkStopped("這台 worker 尚未註冊：第一次執行請加上 --reg-token")
            await ws.send_json(protocol.register(self.reg_token))
            frame = await self._expect(ws, "registered", stage="register")
            state = WorkerState(worker_id=frame["worker_id"], worker_token=frame["worker_token"])
            save_state(self.state_path, state)
            log.info("已向閘道註冊，worker_id=%s", state.worker_id)
        keys = [key.announce() for key in self.keys.ordered()]
        await ws.send_json(
            protocol.hello(state.worker_id, state.worker_token, keys, models, self.config.max_concurrency)
        )
        await self._expect(ws, "ready", stage="hello")
        self._announced = models
        log.info("已連上閘道：worker_id=%s，%d 把金鑰，%d 個模型", state.worker_id, len(keys), len(models))

    async def _expect(self, ws: aiohttp.ClientWebSocketResponse, expected: str, *, stage: str) -> dict:
        msg = await ws.receive(timeout=HANDSHAKE_TIMEOUT)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise HandshakeFailed("閘道在握手期間關閉了連線")
        frame = protocol.parse_frame(msg.data)
        if frame["type"] == "error":
            if frame["code"] == "unauthorized":
                raise LinkStopped(_UNAUTHORIZED[stage])
            raise HandshakeFailed(f"閘道拒絕握手：{frame['code']}")
        if frame["type"] != expected:
            raise HandshakeFailed(f"握手時預期 {expected}，收到 {frame['type']}")
        return frame

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        send_lock = asyncio.Lock()
        tasks: set[asyncio.Task] = set()

        def spawn(coro) -> None:
            task = asyncio.create_task(coro)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        refresher = asyncio.create_task(self._refresh_loop(ws, send_lock))
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = protocol.parse_frame(msg.data)
                except ProtocolError as exc:
                    request_id = _request_id_of(msg.data)
                    log.warning("收到格式錯誤的訊框（%s），request_id=%s", exc.code, request_id or "-")
                    if request_id:
                        spawn(self._send(ws, send_lock, protocol.response_error(request_id, exc.code)))
                    continue
                if frame["type"] == "request":
                    # 每筆請求各自一個 task，慢的請求不會卡住讀取迴圈。
                    spawn(self._respond(ws, send_lock, frame))
                elif frame["type"] == "error" and frame["code"] == "unauthorized":
                    raise LinkStopped(_UNAUTHORIZED["hello"])
                else:
                    log.warning("忽略非預期的訊框：%s", frame["type"])
        finally:
            # 連線已斷，閘道會讓掛著的請求以 worker_disconnected 失敗；不必再算下去。
            refresher.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(refresher, *tasks, return_exceptions=True)

    async def _respond(self, ws: aiohttp.ClientWebSocketResponse, lock: asyncio.Lock, frame: dict) -> None:
        await self._send(ws, lock, await self.handler.handle(frame))

    @staticmethod
    async def _send(ws: aiohttp.ClientWebSocketResponse, lock: asyncio.Lock, frame: dict) -> None:
        try:
            async with lock:
                await ws.send_json(frame)
        except (ConnectionError, aiohttp.ClientError) as exc:
            log.warning("無法送出 %s 訊框（%s）", frame["type"], type(exc).__name__)

    async def _refresh_loop(self, ws: aiohttp.ClientWebSocketResponse, lock: asyncio.Lock) -> None:
        while True:
            await asyncio.sleep(self.refresh_seconds)
            models = await self.refresh_routes()
            if models != self._announced:
                await self._send(ws, lock, protocol.models(models))
                self._announced = models
                log.info("模型清單已更新：%s", ", ".join(models) or "（無）")

    async def refresh_routes(self) -> list[str]:
        """重新查詢後端的模型、更新請求處理器的路由，回傳排序後的模型名稱。"""
        routes = await collect_models(self.backends, self.config.expose_models, self._warned)
        self.handler.routes = routes
        return sorted(routes)


def _request_id_of(text: str) -> str | None:
    """格式錯誤的 request 訊框若帶有合法的 request_id，就取出來，好回覆裸錯誤碼讓閘道不必空等。"""
    try:
        obj = loads_strict(text)
        request_id = obj.get("request_id") if isinstance(obj, dict) and obj.get("type") == "request" else None
        if isinstance(request_id, str) and len(b64u_decode(request_id)) == 16:
            return request_id
    except ProtocolError:
        pass
    return None


# 錯誤訊息（例如 aiohttp 的 WSServerHandshakeError）會帶完整網址；網址的第一段路徑可能是
# 秘密的 gate，進紀錄前遮掉（T-R）。
_URL_FIRST_SEGMENT = re.compile(r"(\b(?:wss?|https?)://[^/\s'\"]+/)[^/\s'\"]+")


def _describe(exc: BaseException) -> str:
    text = _URL_FIRST_SEGMENT.sub(r"\1***", str(exc))
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def serve(
    config: WorkerConfig,
    keys: KeyStore,
    home: Path,
    reg_token: str | None = None,
    *,
    clock: Callable[[], float] = time.time,
    **link_options,
) -> None:
    """worker 的主迴圈。home 是 $LLMOP_HOME/worker；link_options 只給測試調整退避與更新間隔。"""
    # trust_env=False：後端都在迴圈位址，絕不經過系統代理。
    async with (
        aiohttp.ClientSession(trust_env=False) as backend_session,
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT)) as gateway_session,
    ):
        backends = [make_backend(b, backend_session, config.request_timeout) for b in config.backends]
        handler = Handler(
            keys=keys,
            replay=ReplayGuard(clock),
            max_concurrency=config.max_concurrency,
            pinned_clients=config.pinned_clients,
        )
        link = Link(
            config=config,
            keys=keys,
            state_path=home / "state.json",
            handler=handler,
            backends=backends,
            reg_token=reg_token,
            **link_options,
        )
        await link.run(gateway_session)
