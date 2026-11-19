"""/worker/ws：worker 的 WebSocket 連線（註冊、hello、模型更新、轉送與回覆），FastAPI 版。

移植自 B 的 ``llmop.gateway.worker_ws``。憑證只出現在第一個訊框裡。紀錄只寫 worker_id、
request_id、狀態與大小，絕不寫訊框內容、權杖或封裝好的回覆。

握手失敗依來源限流（``hub.HandshakeThrottle``）：被限流的來源在 WebSocket 握手階段
就被拒絕（HTTP 403），worker 會當成暫時性錯誤退避重試，不會誤以為被撤銷。
"""

from __future__ import annotations

import asyncio
import json
import logging

from starlette.websockets import WebSocket

from llmop import protocol
from llmop.crypto import worker_key_id
from llmop.protocol import MAX_BODY_BYTES, WS_FIRST_FRAME_SECONDS, ProtocolError, b64u_decode

from .api import SecureContext
from .hub import Hub, WorkerConn

log = logging.getLogger(__name__)

# 握手訊框的時限；測試可以改小。
FIRST_FRAME_SECONDS: float = WS_FIRST_FRAME_SECONDS

_HANDSHAKE_CODES = frozenset({"unauthorized", "unsupported_version", "bad_envelope", "key_conflict"})


class _Closed(Exception):
    """對方在握手完成前就關閉了連線。"""


def _source(ctx: SecureContext, ws: WebSocket) -> str:
    """限流用的來源：經過可信代理（cloudflared）時取真正的用戶端 IP。"""
    cfg = ctx.config
    if cfg is not None and getattr(cfg, "server", None) is not None:
        from llmop.auth import client_ip

        return client_ip(ws, cfg)  # type: ignore[arg-type]
    return ws.client.host if ws.client else ""


async def handle(ctx: SecureContext, ws: WebSocket) -> None:
    store, hub = ctx.store, ctx.hub
    source = _source(ctx, ws)
    if hub.throttle.blocked(source):
        log.warning("worker 握手失敗次數過多，拒絕連線")
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        conn = await _handshake(ws, store, hub)
    except _Closed:
        return
    except ProtocolError as exc:
        hub.throttle.failed(source)
        code = exc.code if exc.code in _HANDSHAKE_CODES else "bad_envelope"
        log.info("worker 握手失敗 code=%s", code)
        try:
            await _send(ws, protocol.error(code))
            await ws.close()
        except Exception:
            pass
        return

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


async def _handshake(ws: WebSocket, store, hub: Hub) -> WorkerConn:
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
        try:
            computed = worker_key_id(b64u_decode(item["public_key"]))
        except ValueError:
            raise ProtocolError("bad_envelope", "public_key 格式錯誤") from None
        if computed != item["key_id"]:
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
        gate_hash=_gate_hash(ws),
    )
    if hub.attach(conn):
        log.info("worker 的舊連線被取代 worker_id=%s", worker.id)
    await conn.send(protocol.ready())
    return conn


def _gate_hash(ws: WebSocket) -> str | None:
    """Hash of the API-link gate this connection used (None on the local listener)."""
    from llmop.netguard import GATE_HASH_SCOPE_KEY

    try:
        value = ws.scope.get(GATE_HASH_SCOPE_KEY)
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


async def _serve(conn: WorkerConn, store, hub: Hub) -> None:
    while True:
        try:
            msg = await conn.ws.receive()
        except Exception:
            return  # 連線已被關閉（例如被新連線取代）
        if msg["type"] == "websocket.disconnect":
            return
        text = msg.get("text")
        if text is None:
            log.warning("忽略非文字訊框 worker_id=%s", conn.worker_id)
            continue
        if len(text) > MAX_BODY_BYTES:
            log.warning("忽略過大的訊框 worker_id=%s bytes=%d", conn.worker_id, len(text))
            continue
        try:
            frame = protocol.parse_frame(text)
        except ProtocolError as exc:
            log.warning("忽略無效訊框 worker_id=%s code=%s bytes=%d", conn.worker_id, exc.code, len(text))
            continue
        if frame["type"] == "models":
            conn.models = list(dict.fromkeys(frame["models"]))
            store.set_worker_models(conn.worker_id, conn.models)
            log.info("worker 更新模型清單 worker_id=%s models=%d", conn.worker_id, len(conn.models))
        elif frame["type"] == "response":
            _on_response(conn, frame, hub)
        else:
            log.warning("忽略非預期的訊框 worker_id=%s type=%s", conn.worker_id, frame["type"])


def _on_response(conn: WorkerConn, frame: dict, hub: Hub) -> None:
    job = conn.pending.get(frame["request_id"])
    if job is None:
        log.warning("忽略不在等待中的回覆 worker_id=%s request_id=%s", conn.worker_id, frame["request_id"])
        return
    if frame["ok"]:
        usage = frame["usage"]
        job.usage = (usage["prompt_tokens"], usage["completion_tokens"])
        hub.finish(job, frame["sealed"])
        status, size = "done", len(frame["sealed"])
    else:
        hub.finish(job, None, frame["error"], f"worker 回報錯誤：{frame['error']}")
        status, size = frame["error"], 0
    log.info(
        "worker 回覆 request_id=%s api_key_id=%s worker_id=%s model=%s status=%s sealed_bytes=%d duration_ms=%d",
        job.request_id, job.api_key_id, conn.worker_id, job.model, status, size,
        (job.finished_at - job.started_at) * 1000,
    )


async def _receive_text(ws: WebSocket) -> str:
    try:
        msg = await asyncio.wait_for(ws.receive(), FIRST_FRAME_SECONDS)
    except TimeoutError:
        raise ProtocolError("unauthorized", "握手訊框沒有在時限內到達") from None
    if msg["type"] == "websocket.disconnect":
        raise _Closed
    text = msg.get("text")
    if text is None:
        raise ProtocolError("bad_envelope", "握手只接受文字訊框")
    if len(text) > MAX_BODY_BYTES:
        raise ProtocolError("bad_envelope", "握手訊框過大")
    return text


async def _send(ws: WebSocket, frame: dict) -> None:
    await ws.send_text(json.dumps(frame, separators=(",", ":")))
