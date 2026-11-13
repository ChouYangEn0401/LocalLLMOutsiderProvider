"""In-memory login throttle: at most ``max_failures`` failed logins per client IP and
per username inside a sliding ``window`` (seconds).

Kept in memory on purpose: it only needs to slow down password guessing against
a small, single-process gateway; a restart resetting the counters is acceptable.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque


class LoginThrottle:
    def __init__(
        self,
        max_failures: int = 10,
        window: float = 15 * 60,
        *,
        clock=time.monotonic,
        max_keys: int = 20000,
    ):
        self.max_failures = max_failures
        self.window = window
        self.clock = clock
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._fails: dict[str, deque] = {}

    @staticmethod
    def _keys(ip: str, username: str) -> tuple[str, str]:
        return f"ip:{ip}", f"user:{username.strip().lower()[:64]}"

    def _window(self, key: str, now: float) -> deque | None:
        q = self._fails.get(key)
        if q is None:
            return None
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self._fails[key]
            return None
        return q

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        now = self.clock()
        wait = 0.0
        with self._lock:
            for key in self._keys(ip, username):
                q = self._window(key, now)
                if q is not None and len(q) >= self.max_failures:
                    # Allowed again once enough old failures fall out of the window.
                    oldest_relevant = q[len(q) - self.max_failures]
                    wait = max(wait, oldest_relevant + self.window - now)
        return int(math.ceil(wait)) if wait > 0 else 0

    def failure(self, ip: str, username: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._fails) >= self.max_keys:
                self._gc(now)
            for key in self._keys(ip, username):
                self._fails.setdefault(key, deque()).append(now)

    def forgive(self, ip: str, username: str) -> None:
        """Undo one :meth:`failure` for the IP and clear the username's record
        (called after a successful login that was pre-counted as a failure)."""
        ip_key, user_key = self._keys(ip, username)
        with self._lock:
            q = self._fails.get(ip_key)
            if q:
                q.pop()
                if not q:
                    del self._fails[ip_key]
            self._fails.pop(user_key, None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()

    def _gc(self, now: float) -> None:
        for key in list(self._fails):
            self._window(key, now)
        # Still too many distinct keys (e.g. a username-spraying flood): drop the
        # oldest entries. Dicts keep insertion order.
        excess = len(self._fails) - self.max_keys // 2
        if excess > 0:
            for key in list(self._fails)[:excess]:
                del self._fails[key]
