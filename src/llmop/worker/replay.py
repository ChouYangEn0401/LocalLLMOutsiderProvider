"""worker 端的重放防禦（docs/SECURITY.md「重放防禦」）。

三道檢查，依序：
1. 時間窗：|worker_now − ts| 超過 CLOCK_SKEW_SECONDS 就拒絕（stale_timestamp）。
2. 開機門檻：ts 早於 worker 啟動時間就拒絕（stale_timestamp）。已見 ID 只在記憶體裡，
   重啟就遺失；這條規則讓重啟前簽出的請求全部失效。
3. 已見 ID：(sig_key_id, request_id) 保留到 ts + CLOCK_SKEW_SECONDS，重複出現就拒絕（replayed）。
   插入時順便清掉過期的項目。
"""

from __future__ import annotations

import heapq
import time
from collections.abc import Callable

from llmop.protocol import CLOCK_SKEW_SECONDS, Header, ProtocolError, check_timestamp


class ReplayGuard:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._started_at = clock()
        self._seen: dict[tuple[str, str], int] = {}  # (sig_key_id, request_id) → 到期時間
        self._expiry: list[tuple[int, tuple[str, str]]] = []  # 依到期時間排序的最小堆積

    def __len__(self) -> int:
        return len(self._seen)

    def check(self, header: Header) -> None:
        """通過就記錄這筆請求；不通過丟 ProtocolError（stale_timestamp 或 replayed）。"""
        # 時間窗與清除過期項目用同一個整數秒，確保「時間窗還接受」的請求一定還在已見集合裡。
        now = int(self._clock())
        check_timestamp(header.ts, now)
        if header.ts < self._started_at:
            raise ProtocolError("stale_timestamp", "請求早於 worker 這次啟動的時間")
        self._purge(now)
        key = (header.sig_key_id, header.request_id)
        if key in self._seen:
            raise ProtocolError("replayed", "這個 request_id 已經處理過")
        expires = header.ts + CLOCK_SKEW_SECONDS
        self._seen[key] = expires
        heapq.heappush(self._expiry, (expires, key))

    def _purge(self, now: int) -> None:
        # 過了 ts + 300 的項目，時間窗檢查本來就會擋下，不必再記住。
        while self._expiry and self._expiry[0][0] < now:
            _, key = heapq.heappop(self._expiry)
            del self._seen[key]
