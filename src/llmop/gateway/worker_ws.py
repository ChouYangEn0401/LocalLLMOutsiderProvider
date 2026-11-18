"""GET /worker/ws：worker 的 WebSocket 連線（註冊、hello、模型更新、轉送與回覆）。

憑證只出現在第一個訊框裡。紀錄只寫 worker_id、request_id、狀態與大小，
絕不寫訊框內容、權杖或封裝好的回覆。
"""

from __future__ import annotations

import json
import logging

from aiohttp import WSMsgType, web

from llmop import protocol
from llmop.crypto import worker_key_id
from llmop.protocol import MAX_BODY_BYTES, WS_FIRST_FRAME_SECONDS, WS_HEARTBEAT_SECONDS, ProtocolError, b64u_decode

from . import HUB, STORE
from .hub import Hub, WorkerConn
from .store import Store

log = logging.getLogger(__name__)

# 握手訊框的時限；測試可以改小。
FIRST_FRAME_SECONDS: float = WS_FIRST_FRAME_SECONDS


class _Closed(Exception):
    """對方在握手完成前就關閉了連線。"""


async def handle(request: web.Request) -> web.WebSocketResponse:
    store = request.app[STORE]
    hub = request.app[HUB]
    ws = web.WebSocketResponse(heartbeat=WS_HEARTBEAT_SECONDS, max_msg_size=MAX_BODY_BYTES)
    await ws.prepare(request)
    try:
        conn = await _handshake(ws, store, hub)
    except _Closed:
        return ws
    except ProtocolError as exc:
        log.info("worker 握手失敗 code=%s", exc.code)
        if not ws.closed:
            await _send(ws, protocol.error(exc.code))
            await ws.close()
        return ws

    log.info(
        "worker 已連線 worker_id=%s keys=%d models=%d max_concurrency=%d",
        conn.worker_id, len(conn.keys), len(conn.models), conn.max_concurrency,
    )
    try:
        await _serve(conn, store, hub)
    finally:
        hub.detach(conn)
        store.touch_worker(conn.worker_id)
        log.info("worker 已斷線 worker_id=%s", conn.worker_id)
    return ws


async def _handshake(ws: web.WebSocketResponse, store: Store, hub: Hub) -> WorkerConn:
    frame = protocol.parse_frame(await _receive_text(ws))
    if frame["type"] == "register":
        worker_id, worker_token = store.register_worker(frame["reg_token"])
        log.info("worker 完成註冊 worker_id=%s", worker_id)
        await _send(ws, protocol.registered(worker_id, worker_token))
        frame = protocol.parse_frame(await _receive_text(ws))
    if frame["type"] != "hello":
        raise ProtocolError("bad_envelope", "握手的第一個訊框必須是 register 或 hello")

    worker = store.authenticate_worker(frame["worker_id"], frame["worker_token"])
    keys: dict[str, str] = {}
    for item in frame["keys"]:
        if worker_key_id(b64u_decode(item["public_key"])) != item["key_id"]:
            raise ProtocolError("bad_envelope", "key_id 與 public_key 不符")
        keys[item["key_id"]] = item["public_key"]
    store.record_worker_keys(worker.id, keys)
    models = list(dict.fromkeys(frame["models"]))
    store.set_worker_models(worker.id, models)
    store.touch_worker(worker.id)

    conn = WorkerConn(
        worker_id=worker.id,
        name=worker.name,
        ws=ws,
        keys=keys,
        models=models,
        max_concurrency=frame["max_concurrency"],
    )
    if hub.attach(conn):
        log.info("worker 的舊連線被取代 worker_id=%s", worker.id)
    await _send(ws, protocol.ready())
    return conn


async def _serve(conn: WorkerConn, store: Store, hub: Hub) -> None:
    async for msg in conn.ws:
        if msg.type != WSMsgType.TEXT:
            log.warning("忽略非文字訊框 worker_id=%s", conn.worker_id)
            continue
        try:
            frame = protocol.parse_frame(msg.data)
        except ProtocolError as exc:
            log.warning("忽略無效訊框 worker_id=%s code=%s bytes=%d", conn.worker_id, exc.code, len(msg.data))
            continue
        if frame["type"] == "models":
            conn.models = list(dict.fromkeys(frame["models"]))
            store.set_worker_models(conn.worker_id, conn.models)
            log.info("worker 更新模型清單 worker_id=%s models=%d", conn.worker_id, len(conn.models))
        elif frame["type"] == "response":
            _on_response(conn, frame, store, hub)
        else:
            log.warning("忽略非預期的訊框 worker_id=%s type=%s", conn.worker_id, frame["type"])


def _on_response(conn: WorkerConn, frame: dict, store: Store, hub: Hub) -> None:
    job = conn.pending.get(frame["request_id"])
    if job is None:
        log.warning("忽略不在等待中的回覆 worker_id=%s request_id=%s", conn.worker_id, frame["request_id"])
        return
    if frame["ok"]:
        usage = frame["usage"]
        hub.finish(job, frame["sealed"])
        store.add_tokens(job.api_key_id, job.day, usage["prompt_tokens"], usage["completion_tokens"])
        status, size = "done", len(frame["sealed"])
    else:
        hub.finish(job, None, frame["error"], f"worker 回報錯誤：{frame['error']}")
        status, size = frame["error"], 0
    log.info(
        "worker 回覆 request_id=%s api_key_id=%s worker_id=%s model=%s status=%s sealed_bytes=%d duration_ms=%d",
        job.request_id, job.api_key_id, conn.worker_id, job.model, status, size,
        (job.finished_at - job.started_at) * 1000,
    )


async def _receive_text(ws: web.WebSocketResponse) -> str:
    try:
        msg = await ws.receive(timeout=FIRST_FRAME_SECONDS)
    except TimeoutError:
        raise ProtocolError("unauthorized", "握手訊框沒有在時限內到達") from None
    if msg.type == WSMsgType.TEXT:
        return msg.data
    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
        raise _Closed
    raise ProtocolError("bad_envelope", "握手只接受文字訊框")


async def _send(ws: web.WebSocketResponse, frame: dict) -> None:
    await ws.send_str(json.dumps(frame, separators=(",", ":")))
