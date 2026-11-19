"""The API link: ``<api-host>/<gate>`` in front of the public API listener (MERGE_PLAN 9.4).

* The **gate** is a random 256-bit secret path segment. The database keeps only its
  sha256 (``store.api_link``: current hash, previous hash, grace end); the plaintext
  lives in memory and in ``<data_dir>/api_gate.secret`` (mode 0600) so a restart keeps
  the same link. Requests without a valid gate get 404 before anything else
  (:class:`llmop.netguard.GateMiddleware` reads :meth:`ApiLink.gate_state`).
* **Rotate** (local console button or ``llmop tunnel rotate-api``):
  - path only: a new gate, instantly;
  - with a new host name: the API tunnel is replaced first (the new cloudflared is
    started, and the old one stopped only after the new one has a URL), then a new
    gate. The UI tunnel is never touched.
  The old gate stops working at once, or after ``grace_seconds``. API keys are not
  affected. Every rotation appends one line to ``<data_dir>/api_link_audit.jsonl``
  (time, reason, whether the host changed, hash of the old gate).
* Another process (the CLI) may rotate the path: the node notices the new hash in the
  database within ``sync_interval`` seconds and re-reads the gate file. A host change
  requested by the CLI goes through ``store.kv("api_tunnel_restart")``, which the node
  polls (:meth:`ApiLink.poll_requests`).

Never log the gate, only its hash prefix.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .netguard import GateState, gate_hash

log = logging.getLogger("llmop.apilink")

GATE_FILE = "api_gate.secret"
AUDIT_FILE = "api_link_audit.jsonl"
API_URL_KV = "api_tunnel_url"  # written by the "api" TunnelManager
RESTART_KV = "api_tunnel_restart"  # a host change requested by another process
GATE_BYTES = 32  # 256 bits


def new_gate() -> str:
    """43 URL-safe characters (256 random bits)."""
    return secrets.token_urlsafe(GATE_BYTES)


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(fh.fileno(), 0o600)
            fh.write(text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_gate(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    ok = 16 <= len(text) <= 128 and all(c.isalnum() or c in "-_" for c in text) and text.isascii()
    return text if ok else ""


def api_base_url(cfg: Any, tunnel: Any = None, store: Any = None) -> str:
    """Where the API listener is reachable: the live API tunnel URL, else the named
    tunnel's ``tunnels.api.public_url``, else (another process) the URL the API tunnel
    published in the store, else the loopback address of ``listeners.public_api``."""
    url = getattr(tunnel, "url", None) if tunnel is not None else None
    if url:
        return str(url).rstrip("/")
    tunnels = getattr(cfg, "tunnels", None)
    fixed = getattr(getattr(tunnels, "api", None), "public_url", "") or ""
    if fixed:
        return fixed.rstrip("/")
    if store is not None and tunnel is None:
        try:
            got = store.kv_get(API_URL_KV)
        except Exception:
            got = None
        if got and got[0]:
            return got[0].rstrip("/")
    port = getattr(getattr(cfg, "listeners", None), "public_api", 8782)
    return f"http://127.0.0.1:{port}"


class ApiLink:
    """See the module docstring. ``base_url`` returns the API host (no gate);
    ``restart_tunnel`` (async or sync, optional) replaces the API tunnel."""

    def __init__(
        self,
        store: Any,
        data_dir: str | os.PathLike | None,
        *,
        base_url: Callable[[], str] | None = None,
        restart_tunnel: Callable[[], Any] | None = None,
        grace_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
        sync_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.base_url = base_url or (lambda: "http://127.0.0.1:8782")
        self.restart_tunnel = restart_tunnel
        self.grace_seconds = float(grace_seconds)
        self.clock = clock
        self.sync_interval = sync_interval
        self._lock = threading.Lock()
        self._alock: asyncio.Lock | None = None
        self._gate = ""
        self._state: GateState | None = None
        self._synced_at = -1e18
        self.last_rotation: dict | None = None
        self._load()

    # ------------------------------------------------------------ persistence
    @property
    def gate_file(self) -> Path | None:
        return self.data_dir / GATE_FILE if self.data_dir is not None else None

    def _load(self) -> None:
        row = self.store.get_api_link()
        gate = _read_gate(self.gate_file) if self.gate_file is not None else ""
        if row and gate and gate_hash(gate) == row["current_gate_hash"]:
            self._gate = gate
            self._state = self._row_state(row)
            self._synced_at = time.monotonic()
            return
        # First start, lost file, or a database from somewhere else: a fresh gate.
        self._rotate_locked(reason="initial", change_host=False, grace=0.0, audit=row is not None)

    @staticmethod
    def _row_state(row: dict) -> GateState:
        return GateState(current_hash=row["current_gate_hash"] or "",
                         previous_hash=row.get("previous_gate_hash") or None,
                         grace_until=float(row.get("grace_until") or 0.0))

    def sync(self, force: bool = False) -> GateState | None:
        """Re-read the hashes from the database (at most every ``sync_interval``
        seconds unless ``force``); pick up a gate rotated by another process."""
        now = time.monotonic()
        if not force and now - self._synced_at < self.sync_interval and self._state is not None:
            return self._state
        with self._lock:
            self._synced_at = now
            try:
                row = self.store.get_api_link()
            except Exception as exc:  # keep serving with the last known state
                log.warning("api link: cannot read the database (%s)", type(exc).__name__)
                return self._state
            if row is None:
                return self._state
            state = self._row_state(row)
            if not self._gate or gate_hash(self._gate) != state.current_hash:
                gate = _read_gate(self.gate_file) if self.gate_file is not None else ""
                self._gate = gate if gate and gate_hash(gate) == state.current_hash else ""
                if self._gate:
                    log.info("api link: picked up a gate rotated elsewhere (hash %s…)",
                             state.current_hash[:8])
            self._state = state
            return state

    def gate_state(self) -> GateState | None:
        """For :class:`GateMiddleware` (called once per request)."""
        return self.sync()

    # ------------------------------------------------------------ reading
    @property
    def gate(self) -> str:
        """The plaintext gate ("" when unknown). Never log this."""
        self.sync()
        return self._gate

    def current_url(self) -> str:
        """``<api-host>/<gate>`` (the base for ``/v1`` and ``/e2e/v1``), or ""."""
        gate = self.gate
        if not gate:
            return ""
        try:
            base = (self.base_url() or "").rstrip("/")
        except Exception:
            base = ""
        return f"{base}/{gate}" if base else ""

    @property
    def url(self) -> str:
        return self.current_url()

    def as_dict(self) -> dict:
        state = self.sync()
        now = self.clock()
        return {
            "url": self.current_url(),
            "grace_until": state.grace_until if state and state.previous_hash and
            now < state.grace_until else None,
            "last_rotation": self.last_rotation,
        }

    # ------------------------------------------------------------ rotation
    def rotate_path(self, reason: str = "", grace_seconds: float | None = None, *,
                    change_host: bool = False) -> dict:
        """Synchronous path-only rotation (also what the CLI uses)."""
        grace = self.grace_seconds if grace_seconds is None else max(0.0, float(grace_seconds))
        with self._lock:
            return self._rotate_locked(reason=reason, change_host=change_host, grace=grace)

    async def rotate(self, change_host: bool = False, reason: str = "",
                     grace_seconds: float | None = None) -> dict:
        """Rotate the API link. ``change_host`` also replaces the API tunnel (new
        cloudflared first, old one stopped after the new URL is known)."""
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            host_changed = False
            if change_host:
                if self.restart_tunnel is None:
                    log.warning("api link: no API tunnel to restart; rotating the path only")
                else:
                    result = self.restart_tunnel()
                    if inspect.isawaitable(result):
                        result = await result
                    host_changed = result is not False
            return self.rotate_path(reason, grace_seconds, change_host=host_changed)

    def _rotate_locked(self, *, reason: str, change_host: bool, grace: float,
                       audit: bool = True) -> dict:
        old = self.store.get_api_link()
        old_hash = old["current_gate_hash"] if old else None
        gate = new_gate()
        now = self.clock()
        grace_until = now + grace if (grace > 0 and old_hash) else 0.0
        if self.gate_file is not None:
            _write_secret(self.gate_file, gate)
        self.store.set_api_link(gate_hash(gate), old_hash if grace_until else None, grace_until,
                                now=now)
        self._gate = gate
        self._state = GateState(gate_hash(gate), old_hash if grace_until else None, grace_until)
        self._synced_at = time.monotonic()
        entry = {
            "ts": round(now, 3),
            "reason": (reason or "")[:200],
            "change_host": bool(change_host),
            "old_gate_sha256": old_hash,
            "grace_until": grace_until or None,
        }
        self.last_rotation = {k: entry[k] for k in ("ts", "reason", "change_host", "grace_until")}
        if audit:
            self._audit(entry)
        log.info("api link rotated: change_host=%s grace=%gs old_hash=%s… new_hash=%s…",
                 change_host, grace, (old_hash or "-")[:8], gate_hash(gate)[:8])
        return {"url": self.current_url(), "grace_until": grace_until or None,
                "change_host": bool(change_host)}

    def _audit(self, entry: dict) -> None:
        if self.data_dir is None:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / AUDIT_FILE).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("api link: cannot write the audit log (%s)", type(exc).__name__)

    def audit_entries(self) -> list[dict]:
        if self.data_dir is None:
            return []
        try:
            lines = (self.data_dir / AUDIT_FILE).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------ cross-process requests
    async def poll_requests(self) -> bool:
        """Node side: carry out a host change requested by ``llmop tunnel rotate-api
        --change-host`` (kv ``api_tunnel_restart``). Returns True if one was handled."""
        self.sync()
        try:
            got = self.store.kv_get(RESTART_KV)
        except Exception:
            return False
        if not got or not got[0]:
            return False
        self.store.kv_set(RESTART_KV, "")
        if self.restart_tunnel is not None:
            result = self.restart_tunnel()
            if inspect.isawaitable(result):
                await result
            self._audit({"ts": round(self.clock(), 3), "reason": "host change requested by CLI",
                         "change_host": True, "old_gate_sha256": None, "grace_until": None})
        return True


def worker_ws_url(link_url: str) -> str:
    """``<api-host>/<gate>`` -> the ``gateway_url`` a remote worker needs
    (``wss://<api-host>/<gate>/worker/ws``). Never log the result."""
    url = (link_url or "").rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return f"{url}/worker/ws" if url else ""


REMOTE_WORKER_REMINDER = (
    "Remote workers (llmop worker run on another computer) connect through the API "
    "link: set gateway_url in their worker config.json to the new address and restart "
    "them. Workers still connected through the old link are disconnected once the old "
    "link stops working. A worker on this computer is not affected."
)


def request_host_change(store: Any) -> None:
    """CLI side: ask a running node to replace its API tunnel."""
    store.kv_set(RESTART_KV, str(time.time()))


def host_change_pending(store: Any) -> bool:
    got = store.kv_get(RESTART_KV)
    return bool(got and got[0])
