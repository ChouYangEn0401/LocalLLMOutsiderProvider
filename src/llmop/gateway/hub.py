"""記憶體中的狀態：已連線的 worker、進行中的請求，以及完成後暫存的結果。

這裡不碰磁碟。信封只在轉送時經過，不會留在任何表裡；結果只保存 worker 封裝好的回覆字串，
保留 RESULT_TTL_SECONDS 秒後丟棄。工作表同時也是閘道的重複請求檢查。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from aiohttp import web

from llmop import protocol
from llmop.protocol import RESULT_TTL_SECONDS, Envelope, ProtocolError

from .store import Store

log = logging.getLogger(__name__)

# 每台 worker 最多可以排隊的請求數 = 這個倍數 × max_concurrency。
PENDING_PER_CONCURRENCY = 8


@dataclass(eq=False)
class WorkerConn:
    """一條已完成握手的 worker 連線。"""

    worker_id: str
    name: str
    ws: web.WebSocketResponse = field(repr=False)
    keys: dict[str, str]  # key_id → public_key（b64u），第一個是目前的金鑰
    models: list[str]
    max_concurrency: int
    pending: dict[str, Job] = field(default_factory=dict, repr=False)

    @property
    def capacity(self) -> int:
        return PENDING_PER_CONCURRENCY * self.max_concurrency


@dataclass(eq=False)
class Job:
    """一筆已轉送的請求。完成後 sealed 或 code 二擇一有值。"""

    api_key_id: str
    request_id: str
    worker_id: str
    model: str
    day: str  # 計入配額的 UTC 日期，token 用量也記在這一天
    max_tokens: int  # admit 保留的量；工作結束時 finish() 一定要釋放剛好這麼多
    started_at: float
    conn: WorkerConn = field(repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    sealed: str | None = field(default=None, repr=False)
    code: str | None = None
    message: str = ""
    finished_at: float | None = None
    timer: asyncio.TimerHandle | None = field(default=None, repr=False)


class Hub:
    def __init__(self, store: Store, max_inflight: int, request_timeout_seconds: float) -> None:
        self.store = store
        self.max_inflight = max_inflight
        self.request_timeout_seconds = request_timeout_seconds
        self.workers: dict[str, WorkerConn] = {}
        self.jobs: dict[tuple[str, str], Job] = {}  # (api_key_id, request_id) → Job
        # worker 的回覆只帶 request_id，所以進行中的 request_id 在整個閘道內必須唯一。
        self._inflight: dict[str, Job] = {}
        self._closing: set[asyncio.Task] = set()

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
        """連線結束。如果它仍是這台 worker 的目前連線就移除，並讓它的待處理請求失敗。"""
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
        """關閉所有 worker 連線（閘道停止時）。"""
        for worker_id in list(self.workers):
            self.disconnect(worker_id)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    def _close_later(self, conn: WorkerConn) -> None:
        # close() 會等對方回應關閉訊框（最多數秒）；舊連線常常已經失聯，所以不在呼叫端等它。
        task = asyncio.get_running_loop().create_task(conn.ws.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    # ------------------------------------------------------------------
    # 路由與容量
    # ------------------------------------------------------------------

    def route(self, key_id: str, model: str) -> WorkerConn:
        """找出公告了 key_id 的線上 worker，並確認它提供 model。"""
        for conn in self.workers.values():
            if key_id in conn.keys:
                if model not in conn.models:
                    raise ProtocolError("model_unavailable", "這台 worker 沒有提供這個模型")
                return conn
        raise ProtocolError("unknown_key_id", "沒有線上的 worker 持有這個 key_id")

    def check_capacity(self, conn: WorkerConn) -> None:
        if self.inflight >= self.max_inflight:
            raise ProtocolError("gateway_busy", "閘道進行中的請求已滿，請稍後再試")
        if len(conn.pending) >= conn.capacity:
            raise ProtocolError("worker_busy", "worker 的佇列已滿，請稍後再試")

    # ------------------------------------------------------------------
    # 工作
    # ------------------------------------------------------------------

    def is_duplicate(self, api_key_id: str, request_id: str) -> bool:
        return (api_key_id, request_id) in self.jobs or request_id in self._inflight

    def get(self, api_key_id: str, request_id: str) -> Job | None:
        return self.jobs.get((api_key_id, request_id))

    def submit(self, conn: WorkerConn, api_key_id: str, request_id: str, model: str, day: str, max_tokens: int) -> Job:
        """登記一筆即將轉送的請求，並排定逾時。呼叫前必須已通過 route、check_capacity 與 store.admit
        （max_tokens 必須是 admit 剛保留的那個量，finish() 會原封不動釋放它）。"""
        loop = asyncio.get_running_loop()
        job = Job(
            api_key_id=api_key_id,
            request_id=request_id,
            worker_id=conn.worker_id,
            model=model,
            day=day,
            max_tokens=max_tokens,
            started_at=loop.time(),
            conn=conn,
        )
        self.jobs[(api_key_id, request_id)] = job
        self._inflight[request_id] = job
        conn.pending[request_id] = job
        job.timer = loop.call_later(
            self.request_timeout_seconds, self.finish, job, None, "timeout", "worker 在時限內沒有回覆"
        )
        return job

    async def relay(self, job: Job, envelope: Envelope, client_sig_pub: str) -> None:
        """把信封送給 worker。送不出去就讓這筆工作以 worker_disconnected 失敗。"""
        frame = protocol.request(job.request_id, envelope, client_sig_pub)
        try:
            await job.conn.ws.send_str(json.dumps(frame, separators=(",", ":")))
        except (ConnectionError, RuntimeError):
            self.finish(job, None, "worker_disconnected", "無法把請求送給 worker")

    def finish(self, job: Job, sealed: str | None, code: str | None = None, message: str = "") -> None:
        """記錄結果並喚醒等待者。已經完成的工作不會被改寫（所以下面的釋放一定只做一次）。

        不管工作是成功、失敗、逾時還是因為斷線而結束，都要把 admit 當初保留的 max_tokens
        還回去；真正的用量是另外由呼叫端在 finish() 之後自己呼叫 store.add_tokens 加上去的
        （只有成功拿到 worker 回覆的那一條路才會加）。
        """
        if job.finished_at is not None:
            return
        if job.timer is not None:
            job.timer.cancel()
        job.sealed, job.code, job.message = sealed, code, message
        job.finished_at = asyncio.get_running_loop().time()
        self._inflight.pop(job.request_id, None)
        job.conn.pending.pop(job.request_id, None)
        self.store.release_reservation(job.api_key_id, job.day, job.max_tokens)
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
