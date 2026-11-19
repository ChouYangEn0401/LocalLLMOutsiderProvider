"""記憶體中的狀態：已連線的 worker、進行中的請求、完成後暫存的結果，以及握手失敗的限流。

移植自 B 的 ``llmop.gateway.hub``，改成 FastAPI（Starlette）的 WebSocket，並把
「每台 worker 最多 8 × max_concurrency 筆」換成 T-3 的排程器：每筆請求在背景工作裡
``async with scheduler.slot(...)`` 拿到名額之後才轉送給 worker，結果回來（或失敗）才釋放。

這裡不碰磁碟（用量由 ``Job.settle`` 回呼寫入）。信封只在轉送時經過，不會留在任何表裡；
結果只保存 worker 封裝好的回覆字串，保留 RESULT_TTL_SECONDS 秒後丟棄。
工作表同時也是重複請求檢查。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from llmop import protocol
from llmop.protocol import RESULT_TTL_SECONDS, Envelope, ProtocolError

log = logging.getLogger(__name__)

# 握手失敗限流：同一個來源在 HANDSHAKE_WINDOW_SECONDS 內失敗 HANDSHAKE_FAILURE_LIMIT 次，
# 之後的連線在 WebSocket 握手階段就被拒絕（HTTP 403），直到最早的失敗滑出時間窗。
HANDSHAKE_FAILURE_LIMIT = 10
HANDSHAKE_WINDOW_SECONDS = 300.0
_THROTTLE_MAX_SOURCES = 10_000


@dataclass(eq=False)
class WorkerConn:
    """一條已完成握手的 worker 連線。"""

    worker_id: str
    name: str
    ws: Any = field(repr=False)  # starlette.websockets.WebSocket
    keys: dict[str, str]  # key_id → public_key（b64u），第一個是目前的金鑰
    models: list[str]
    max_concurrency: int
    pending: dict[str, Job] = field(default_factory=dict, repr=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def send(self, frame: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(frame, separators=(",", ":")))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()


@dataclass(eq=False)
class Job:
    """一筆已受理的請求。完成後 sealed 或 code 二擇一有值。

    ``settle`` 在工作結束時（不論成功、失敗、逾時、斷線、排隊被拒）剛好呼叫一次，
    負責釋放額度租約並記錄用量。``relayed`` 表示信封確實送到了 worker。
    """

    api_key_id: str
    request_id: str
    key_id: str  # 信封標頭裡的 worker key_id（路由用）
    model: str
    started_at: float
    settle: Callable[[Job], None] | None = field(default=None, repr=False)
    worker_id: str | None = None
    worker_name: str = ""
    conn: WorkerConn | None = field(default=None, repr=False)
    ticket: Any = field(default=None, repr=False)  # llmop.scheduler.Ticket
    task: asyncio.Task | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    relayed: bool = False
    sealed: str | None = field(default=None, repr=False)
    code: str | None = None
    message: str = ""
    retry_after: int | None = None
    usage: tuple[int, int] | None = None  # worker 回報的 (prompt, completion)
    finished_at: float | None = None

    def queue_info(self) -> dict | None:
        """``pending`` 回應的 ``queue``：位置（0 = 已在處理）與預估秒數。"""
        ticket = self.ticket
        if ticket is None:
            return None
        position = ticket.position()
        if position is None:
            return None
        eta = ticket.eta() or 0.0
        return {"position": position, "eta": int(round(eta))}


class HandshakeThrottle:
    """依來源（IP）計算握手失敗次數的滑動視窗。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _window(self, source: str, now: float) -> deque[float] | None:
        q = self._failures.get(source)
        if q is None:
            return None
        while q and q[0] <= now - HANDSHAKE_WINDOW_SECONDS:
            q.popleft()
        if not q:
            del self._failures[source]
            return None
        return q

    def blocked(self, source: str) -> bool:
        q = self._window(source, self.clock())
        return q is not None and len(q) >= HANDSHAKE_FAILURE_LIMIT

    def failed(self, source: str) -> None:
        now = self.clock()
        q = self._window(source, now)
        if q is None:
            if len(self._failures) >= _THROTTLE_MAX_SOURCES:
                self._prune(now)
            q = self._failures.setdefault(source, deque())
        q.append(now)

    def _prune(self, now: float) -> None:
        for source in list(self._failures):
            self._window(source, now)
        while len(self._failures) >= _THROTTLE_MAX_SOURCES:
            del self._failures[next(iter(self._failures))]


class Hub:
    def __init__(self, request_timeout_seconds: float) -> None:
        self.request_timeout_seconds = request_timeout_seconds
        self.workers: dict[str, WorkerConn] = {}
        self.jobs: dict[tuple[str, str], Job] = {}  # (api_key_id, request_id) → Job
        # worker 的回覆只帶 request_id，所以進行中的 request_id 在整個閘道內必須唯一。
        self._inflight: dict[str, Job] = {}
        self._closing: set[asyncio.Task] = set()
        self.throttle = HandshakeThrottle()

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    # ------------------------------------------------------------------
    # worker 連線
    # ------------------------------------------------------------------

    def attach(self, conn: WorkerConn) -> bool:
        """登記新連線。同一個 worker_id 的舊連線被取代並關閉，回傳是否有舊連線。"""
        old = self.workers.get(conn.worker_id)
        self.workers[conn.worker_id] = conn
        if old is not None:
            self._fail_pending(old, "worker_disconnected", "worker 的連線被新的連線取代")
            self._close_later(old)
        return old is not None

    def detach(self, conn: WorkerConn) -> None:
        """連線結束。如果它仍是這台 worker 的目前連線就移除，並讓已轉送給它的請求失敗。"""
        if self.workers.get(conn.worker_id) is conn:
            del self.workers[conn.worker_id]
        self._fail_pending(conn, "worker_disconnected", "worker 已斷線")

    def disconnect(self, worker_id: str) -> None:
        """主動切斷某台 worker（被撤銷或閘道停止時）。"""
        conn = self.workers.pop(worker_id, None)
        if conn is not None:
            self._fail_pending(conn, "worker_disconnected", "閘道切斷了 worker 的連線")
            self._close_later(conn)

    async def close_all(self) -> None:
        """關閉所有 worker 連線並結束所有工作（閘道停止時）。"""
        for worker_id in list(self.workers):
            self.disconnect(worker_id)
        for job in list(self._inflight.values()):
            self.finish(job, None, "worker_disconnected", "閘道正在停止")
            if job.task is not None:
                job.task.cancel()  # 還在排隊的工作離開隊伍
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    def _close_later(self, conn: WorkerConn) -> None:
        # close() 可能要等對方回應；舊連線常常已經失聯，所以不在呼叫端等它。
        task = asyncio.get_running_loop().create_task(conn.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def route(self, key_id: str, model: str) -> WorkerConn:
        """找出公告了 key_id 的線上 worker，並確認它提供 model。"""
        for conn in self.workers.values():
            if key_id in conn.keys:
                if model not in conn.models:
                    raise ProtocolError("model_unavailable", "這台 worker 沒有提供這個模型")
                return conn
        raise ProtocolError("unknown_key_id", "沒有線上的 worker 持有這個 key_id")

    # ------------------------------------------------------------------
    # 工作
    # ------------------------------------------------------------------

    def is_duplicate(self, api_key_id: str, request_id: str) -> bool:
        return (api_key_id, request_id) in self.jobs or request_id in self._inflight

    def get(self, api_key_id: str, request_id: str) -> Job | None:
        return self.jobs.get((api_key_id, request_id))

    def submit(self, job: Job, envelope: Envelope, client_sig_pub: str, slot: Any) -> Job:
        """登記一筆已通過驗證與准入的請求，並在背景排隊、轉送、等待結果。

        ``slot`` 是 ``scheduler.slot(account, tier, zone)`` 的回傳值（還沒進入）。"""
        job.ticket = getattr(slot, "ticket", None)
        self.jobs[(job.api_key_id, job.request_id)] = job
        self._inflight[job.request_id] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job, envelope, client_sig_pub, slot))
        return job

    async def _run(self, job: Job, envelope: Envelope, client_sig_pub: str, slot: Any) -> None:
        from llmop.scheduler import QueueFull, QueueTimeout

        try:
            await asyncio.wait_for(self._dispatch(job, envelope, client_sig_pub, slot),
                                   self.request_timeout_seconds)
        except TimeoutError:
            self.finish(job, None, "timeout", "worker 在時限內沒有回覆")
        except QueueFull as exc:
            if exc.reason == "account":
                self.finish(job, None, "queue_wait", "這個帳號排隊中的請求已達上限，請稍後再試",
                            retry_after=exc.retry_after)
            else:
                self.finish(job, None, "gateway_busy", "排隊已滿，請稍後再試", retry_after=exc.retry_after)
            # 根本沒有排進隊伍：忘掉這筆，之後可以用同一個 request_id 重送。
            self.jobs.pop((job.api_key_id, job.request_id), None)
        except QueueTimeout as exc:
            self.finish(
                job, None, "queue_wait",
                f"目前排第 {exc.position} 位，約 {exc.retry_after} 秒後再試", retry_after=exc.retry_after,
            )
        except ProtocolError as exc:
            self.finish(job, None, exc.code, exc.message)
        except asyncio.CancelledError:
            self.finish(job, None, "worker_disconnected", "閘道正在停止")
            raise
        except Exception as exc:
            # 只記例外類型，不記訊息：訊息裡可能夾帶輸入資料。
            log.error("工作內部錯誤 request_id=%s error=%s", job.request_id, type(exc).__name__)
            self.finish(job, None, "internal_error", "閘道內部錯誤")

    async def _dispatch(self, job: Job, envelope: Envelope, client_sig_pub: str, slot: Any) -> None:
        async with slot:
            if job.done.is_set():
                return
            # 排到名額時重新路由：排隊期間 worker 可能重新連線（換了連線物件）或改了模型清單。
            conn = self.route(job.key_id, job.model)
            job.conn, job.worker_id, job.worker_name = conn, conn.worker_id, conn.name
            conn.pending[job.request_id] = job
            frame = protocol.request(job.request_id, envelope, client_sig_pub)
            try:
                await conn.send(frame)
            except Exception:
                self.finish(job, None, "worker_disconnected", "無法把請求送給 worker")
                return
            job.relayed = True
            await job.done.wait()

    def finish(self, job: Job, sealed: str | None, code: str | None = None, message: str = "",
               *, retry_after: int | None = None) -> None:
        """記錄結果並喚醒等待者。已經完成的工作不會被改寫（所以 settle 只會呼叫一次）。"""
        if job.finished_at is not None:
            return
        job.sealed, job.code, job.message, job.retry_after = sealed, code, message, retry_after
        job.finished_at = asyncio.get_running_loop().time()
        self._inflight.pop(job.request_id, None)
        if job.conn is not None:
            job.conn.pending.pop(job.request_id, None)
        if job.settle is not None:
            try:
                job.settle(job)
            except Exception as exc:
                log.error("記錄用量失敗 request_id=%s error=%s", job.request_id, type(exc).__name__)
        job.done.set()

    async def wait(self, job: Job, timeout: float) -> bool:
        """最多等 timeout 秒，回傳工作是否已完成。"""
        if not job.done.is_set():
            try:
                await asyncio.wait_for(job.done.wait(), timeout)
            except TimeoutError:
                pass
        return job.done.is_set()

    def purge(self) -> int:
        """丟掉完成超過 RESULT_TTL_SECONDS 的結果，回傳丟掉幾筆。

        結果至少保留到 ts + 300 之後（工作建立時 ts 不早於 now − 300），所以丟掉之後
        同一個 request_id 也已經過了時間窗，不會被當成新請求接受。
        """
        cutoff = asyncio.get_running_loop().time() - RESULT_TTL_SECONDS
        expired = [k for k, job in self.jobs.items() if job.finished_at is not None and job.finished_at < cutoff]
        for key in expired:
            del self.jobs[key]
        return len(expired)

    def _fail_pending(self, conn: WorkerConn, code: str, message: str) -> None:
        for job in list(conn.pending.values()):
            self.finish(job, None, code, message)
