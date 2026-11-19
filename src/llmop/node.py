"""The v0.2 node: everything this machine runs, in one process and one event loop.

``llmop serve`` calls :func:`run_node`. Depending on ``roles`` and ``listeners``:

* three uvicorn servers (all on 127.0.0.1): ``local`` (console + key-less ``/v1`` +
  ``/worker/ws``; ``proxy_headers`` off), ``public-ui`` (account website) and
  ``public-api`` (``/<gate>/v1``, ``/<gate>/e2e/v1``, ``/<gate>/worker/ws``);
* when ``roles`` has ``worker``: B's worker in-process, connected to the local
  listener over loopback (``ws://127.0.0.1:<local>/worker/ws``). Its keys and state
  stay in ``$LLMOP_HOME/worker`` (``llmop worker init`` once); a worker that was never
  registered is registered automatically with a fresh one-time token;
* two independent :class:`~llmop.tunnel.TunnelManager` (``ui`` -> public-ui,
  ``api`` -> public-api). Replacing the API tunnel never touches the UI tunnel;
* startup checks (slots vs. the backends' parallel requests, context splitting).

Shutdown (Ctrl+C, SIGTERM or :meth:`Node.request_stop`): the servers finish their
requests, the worker task is cancelled, the tunnels are stopped, the database closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from . import __version__
from .config import Config, TunnelSettings

log = logging.getLogger("llmop.node")

KEEP_ALIVE_SECONDS = 100  # > cloudflared's 90 s idle keep-alive (see cli.py)
TUNNEL_URL_WAIT = 60.0  # seconds to wait for a replacement API tunnel's URL
SECURE_HINT = 'pip install "localllm-outsider-provider[secure]"'


class NodeError(Exception):
    """The node cannot start (shown to the operator without a traceback)."""


# --------------------------------------------------------------------------- checks
def startup_warnings(cfg: Config) -> list[str]:
    """TIERS 7.1: the slots must match what the backend can really run at once, and
    llama.cpp / LM Studio split their context over the parallel slots (TODO T1)."""
    out: list[str] = []
    slots = int(cfg.queue.slots)
    capacity = sum(int(b.max_concurrency or 0) for b in cfg.backends)
    if "gateway" in cfg.roles and cfg.backends and slots > capacity:
        out.append(
            f"queue.slots is {slots} but the backends accept only {capacity} request(s) at "
            "once (backends[].max_concurrency); the extra requests wait at the backend. "
            "Make queue.slots match the backend's parallel slots."
        )
    split = [b.name for b in cfg.backends if b.type in ("llamacpp", "lmstudio")]
    if "gateway" in cfg.roles and slots > 1 and split:
        out.append(
            f"backend(s) {', '.join(split)}: llama.cpp / LM Studio divide the context over "
            f"the parallel slots; with {slots} slots each request gets about 1/{slots} of "
            "it. Long prompts then fail with context_length_exceeded. Start the backend "
            "with a context of (slots x per-request context), or lower queue.slots."
        )
    if "gateway" in cfg.roles and not cfg.model:
        out.append("model is not set in config.yaml: requests keep the model the client "
                   "asks for. Set model: <name> to use one model for everyone.")
    return out


def port_free(port: int, host: str = "127.0.0.1") -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as exc:
            return False, exc.strerror or str(exc)
    return True, ""


class _RedactWebSocketPath(logging.Filter):
    """uvicorn logs every WebSocket handshake as ``"WebSocket <path>" [accepted]`` on
    ``uvicorn.error`` (not controlled by ``access_log``); on the public API listener the
    first path segment is the secret gate. Mask it like the access log does."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and '"WebSocket %s"' in record.msg \
                and isinstance(record.args, tuple) and len(record.args) >= 2:
            from .netguard import redact_path

            args = list(record.args)
            args[1] = redact_path(str(args[1]))
            record.args = tuple(args)
        return True


def _install_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _RedactWebSocketPath) for f in logger.filters):
        logger.addFilter(_RedactWebSocketPath())


def _tunnel_settings(settings: TunnelSettings, force: bool | None) -> TunnelSettings | None:
    """None = do not start this tunnel."""
    if force is False:
        return None
    if force is None and (not settings.enabled or settings.mode == "off"):
        return None
    return settings if settings.mode != "off" else replace(settings, mode="quick")


# --------------------------------------------------------------------------- the node
class _Server:
    """uvicorn.Server without its own signal handling (the node owns the signals;
    three servers capturing them would leave all but one running)."""

    def __init__(self, name: str, app: Any, port: int, *, proxy_headers: bool,
                 forwarded_allow_ips: str, log_level: str, access_log: bool) -> None:
        import uvicorn

        class Server(uvicorn.Server):
            def capture_signals(self):  # type: ignore[override]
                return contextlib.nullcontext()

        self.name, self.port = name, port
        self.config = uvicorn.Config(
            app, host="127.0.0.1", port=port, proxy_headers=proxy_headers,
            forwarded_allow_ips=forwarded_allow_ips, log_level=log_level,
            access_log=access_log, timeout_keep_alive=KEEP_ALIVE_SECONDS,
            server_header=False, lifespan="on", ws="auto",
        )
        self.server = Server(self.config)
        self.task: asyncio.Task | None = None
        self.exit_code: int | None = None

    async def run(self) -> None:
        try:
            await self.server.serve()
        except SystemExit as exc:  # uvicorn exits like this when it cannot start
            self.exit_code = exc.code if isinstance(exc.code, int) else 1

    @property
    def started(self) -> bool:
        return bool(self.server.started)


class Node:
    """One node. ``tunnels`` / ``worker``: None = follow the config, True / False =
    force. ``state``: an :class:`~llmop.app.AppState` to use (tests); by default
    one is built from ``config``. ``tunnel_factory`` replaces TunnelManager (tests)."""

    def __init__(
        self,
        config: Config,
        *,
        tunnels: bool | None = None,
        worker: bool | None = None,
        log_level: str = "info",
        state: Any = None,
        tunnel_factory: Callable[..., Any] | None = None,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = config
        self.want_tunnels = tunnels
        self.want_worker = worker
        self.log_level = log_level
        self.state = state
        self.out = out or _print
        if tunnel_factory is None:
            from .tunnel import TunnelManager

            tunnel_factory = TunnelManager
        self.tunnel_factory = tunnel_factory
        self.servers: list[_Server] = []
        self.worker_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._stopped = False
        self._tunnel_lock = asyncio.Lock()
        self._gate_seen: str | None = None  # current gate hash (rotation reminder)

    # ------------------------------------------------------------ helpers
    @property
    def gateway(self) -> bool:
        return "gateway" in self.cfg.roles

    @property
    def run_worker(self) -> bool:
        if self.want_worker is not None:
            return self.want_worker
        return "worker" in self.cfg.roles

    def local_url(self, name: str) -> str:
        port = {"local": self.cfg.listeners.local, "ui": self.cfg.listeners.public_ui,
                "api": self.cfg.listeners.public_api}[name]
        return f"http://127.0.0.1:{port}"

    def _forbidden(self) -> set[int]:
        from .tunnel import forbidden_tunnel_ports

        return forbidden_tunnel_ports(self.cfg)

    def _make_tunnel(self, name: str, settings: TunnelSettings):
        state = self.state
        on_url = None
        if name == "api":
            def on_url(_url: str) -> None:
                link = getattr(state, "api_link", None)
                if link is not None:
                    self.out(f"  API link  : {link.current_url()}/v1   (and /e2e/v1)")
        public = settings.public_url or (self.cfg.server.public_url if name == "ui" else "")
        return self.tunnel_factory(
            settings, self.local_url("ui" if name == "ui" else "api"), state.store,
            self.cfg.data_path, on_url=on_url, public_url=public, name=name,
            forbidden_ports=self._forbidden(), single_instance=(name == "ui"),
        )

    # ------------------------------------------------------------ start
    async def start(self) -> None:
        from .app import build_state, create_app

        cfg = self.cfg
        if self.gateway:
            ports = [cfg.listeners.local, cfg.listeners.public_ui, cfg.listeners.public_api]
            for port in ports:
                ok, why = port_free(port)
                if not ok:
                    raise NodeError(
                        f"cannot listen on 127.0.0.1:{port} ({why}). Is llmop already running "
                        "in another window? Otherwise change listeners: in config.yaml.")
        for warning in startup_warnings(cfg):
            log.warning("%s", warning)
            self.out(f"WARNING: {warning}")
        if self.state is None:
            self.state = build_state(cfg)
        state = self.state
        if self.gateway:
            _install_log_redaction()
            self._clear_stale_urls()
            forwarded = ",".join(dict.fromkeys(["127.0.0.1", "::1", *cfg.server.trusted_proxies]))
            access = bool(cfg.server.access_log)
            apps = [
                ("local", create_app(state=state, surface="local", port=cfg.listeners.local),
                 cfg.listeners.local, False, access),
                ("public-ui", create_app(state=state, surface="public-ui"),
                 cfg.listeners.public_ui, True, access),
                # the public API logs through RedactedAccessLog (the path holds the gate)
                ("public-api", create_app(state=state, surface="public-api"),
                 cfg.listeners.public_api, True, False),
            ]
            for name, app, port, proxy, acc in apps:
                srv = _Server(name, app, port, proxy_headers=proxy, forwarded_allow_ips=forwarded,
                              log_level=self.log_level, access_log=acc)
                srv.task = asyncio.create_task(srv.run(), name=f"llmop-{name}")
                self.servers.append(srv)
            await self._wait_started()
            link = getattr(state, "api_link", None)
            if link is not None:
                link.restart_tunnel = self.restart_api_tunnel
            self.watch_task = asyncio.create_task(self._watch(), name="llmop-watch")
            self._start_tunnels()
        if self.run_worker:
            self.worker_task = self._start_worker()
        self._banner()

    async def _wait_started(self) -> None:
        while True:
            failed = [s for s in self.servers if s.task.done() and not s.started]
            if failed:
                names = ", ".join(f"{s.name} (127.0.0.1:{s.port})" for s in failed)
                await self.stop()
                raise NodeError(f"could not start {names}; see the messages above.")
            if all(s.started for s in self.servers):
                return
            await asyncio.sleep(0.02)

    def _clear_stale_urls(self) -> None:
        from .tunnel import clear_stale_public_url

        for name in ("ui", "api"):
            try:
                clear_stale_public_url(self.state.store, self.cfg.data_path, name)
            except Exception:
                pass

    def _start_tunnels(self) -> None:
        state = self.state
        for name in ("ui", "api"):
            settings = _tunnel_settings(getattr(self.cfg.tunnels, name), self.want_tunnels)
            if settings is None:
                continue
            tunnel = self._make_tunnel(name, settings)
            state.tunnels[name] = tunnel
            if name == "ui":
                state.tunnel = tunnel  # v0.1 readers (public_url, admin page)
            if not tunnel.start():
                self.out(f"WARNING: the {name} tunnel did not start; continuing without it")

    async def restart_api_tunnel(self) -> bool:
        """Replace only the API tunnel: start a new cloudflared, wait for its URL,
        then stop the old one. False when there is no API tunnel to replace (or it is
        a named tunnel, whose host name never changes)."""
        async with self._tunnel_lock:
            state = self.state
            settings = _tunnel_settings(self.cfg.tunnels.api, self.want_tunnels)
            if settings is None or settings.mode == "named":
                return False
            old = state.tunnels.get("api")
            new = self._make_tunnel("api", settings)
            if not new.start():
                raise RuntimeError("the new API tunnel could not start")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + TUNNEL_URL_WAIT
            while not getattr(new, "url", None):
                if loop.time() > deadline or getattr(new, "status", "") == "error":
                    await asyncio.to_thread(new.stop)
                    raise RuntimeError("the new API tunnel got no URL in time")
                await asyncio.sleep(0.2)
            state.tunnels["api"] = new
            if old is not None:
                await asyncio.to_thread(old.stop)
            log.info("api tunnel replaced")
            return True

    def check_api_link(self) -> None:
        """R-08: after a rotation (here or by the CLI) remind the operator about remote
        workers, and close worker WebSockets that came in through a gate that no
        longer works (after the grace period). Also R-13: follow config reloads."""
        state = self.state
        link = getattr(state, "api_link", None)
        gates = link.gate_state() if link is not None else None
        if gates is not None and gates.current_hash:
            if self._gate_seen is None:
                self._gate_seen = gates.current_hash
            elif gates.current_hash != self._gate_seen:
                self._gate_seen = gates.current_hash
                from .apilink import REMOTE_WORKER_REMINDER

                # The new link itself is not printed here (this output often ends up in
                # a log file); it is in the local console and `llmop tunnel show-api`.
                self.out("API link changed. Remote workers need gateway_url = "
                         "wss://<new API link>/worker/ws (see the local console or "
                         "llmop tunnel show-api).")
                self.out(f"REMINDER: {REMOTE_WORKER_REMINDER}")
        hub = getattr(state, "hub", None)
        if hub is not None and gates is not None:
            now = time.time()
            closed = hub.disconnect_stale_gates(lambda h: gates.allows_hash(h, now))
            for conn in closed:
                self.out(f"WARNING: worker '{conn.name}' was connected through the old API "
                         "link and has been disconnected; update its gateway_url.")
        if getattr(state, "scheduler", None) is not None:
            from .app import sync_scheduler

            sync_scheduler(state.scheduler, state.config)

    async def _watch(self) -> None:
        """Every second: pick up a gate / host change made by ``llmop tunnel rotate-api``,
        close worker connections of an expired gate and notice a listener that died."""
        link = getattr(self.state, "api_link", None)
        while not self._stop.is_set():
            try:
                if link is not None:
                    await link.poll_requests()
            except Exception as exc:
                log.warning("api link request failed: %s", type(exc).__name__)
            try:
                self.check_api_link()
            except Exception as exc:
                log.warning("api link check failed: %s", type(exc).__name__)
            if any(s.task.done() for s in self.servers):
                log.error("a listener stopped unexpectedly; shutting down")
                self._stop.set()
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 1.0)

    # ------------------------------------------------------------ worker
    def _start_worker(self) -> asyncio.Task | None:
        try:
            from . import paths
            from .worker.config import ConfigError as WorkerConfigError
            from .worker.config import load_config as load_worker_config
            from .worker.keystore import load_keys, load_state
            from .worker.link import LinkStopped, serve
        except ImportError:
            self.out(f"WARNING: roles has 'worker' but the secure extra is missing: {SECURE_HINT}")
            return None
        if not self.gateway:
            self.out("WARNING: roles has 'worker' without 'gateway'; run the worker against a "
                     "remote gateway with:  llmop worker run")
            return None
        home = paths.home("worker")
        try:
            wcfg = load_worker_config(home / "config.json")
            keys = load_keys(home / "keys.json")
        except (WorkerConfigError, OSError, ValueError) as exc:
            self.out(f"WARNING: worker not started ({type(exc).__name__}); run once:  llmop worker init")
            return None
        if keys is None:
            self.out("WARNING: worker not started: no keys yet; run once:  llmop worker init")
            return None
        wcfg = replace(wcfg, gateway_url=f"ws://127.0.0.1:{self.cfg.listeners.local}/worker/ws")
        reg_token = None
        if load_state(home / "state.json") is None:
            _, reg_token = self.state.store.create_worker("this computer")
        key = keys.current_key()
        self.out(f"  Worker    : in-process, key_id {key.key_id} (clients pin this key)")

        async def run() -> None:
            try:
                await serve(wcfg, keys, home, reg_token)
            except LinkStopped as exc:
                log.warning("worker stopped: %s", exc)
                self.out(f"WARNING: the worker stopped: {exc}")

        return asyncio.create_task(run(), name="llmop-worker")

    # ------------------------------------------------------------ banner
    def _banner(self) -> None:
        cfg, line = self.cfg, "=" * 66
        rows = ["", line, f"  llmop {__version__} - Local LLM Outsider Provider (node)", ""]
        if self.gateway:
            link = getattr(self.state, "api_link", None)
            rows += [
                f"  Console   : {self.local_url('local')}/   (this computer only)",
                f"  Local API : {self.local_url('local')}/v1   (no key needed)",
                f"  Accounts  : {self.local_url('ui')}/   (tunnel 'ui')",
            ]
            if link is not None and link.current_url():
                rows.append(f"  API link  : {link.current_url()}/v1   (tunnel 'api'; keep it private)")
            for name in ("ui", "api"):
                t = self.state.tunnels.get(name)
                if t is not None:
                    status = "NOT running - see above" if t.status == "error" else \
                        f"{t.mode} tunnel starting"
                    rows.append(f"  Tunnel {name:<3}: {status}")
        rows += [
            f"  Config    : {cfg.path}",
            f"  Data      : {cfg.data_path}",
            "  Stop      : press Ctrl+C or close this window",
            line, "",
        ]
        for r in rows:
            self.out(r)

    # ------------------------------------------------------------ run / stop
    def request_stop(self) -> None:
        self._stop.set()

    async def wait(self) -> None:
        await self._stop.wait()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        for srv in self.servers:
            srv.server.should_exit = True
        if self.watch_task is not None:
            self.watch_task.cancel()
            with contextlib.suppress(BaseException):
                await self.watch_task
        if self.worker_task is not None:
            self.worker_task.cancel()
            with contextlib.suppress(BaseException):
                await self.worker_task
        tasks = [s.task for s in self.servers if s.task is not None]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=15)
            if pending:
                for srv in self.servers:
                    srv.server.force_exit = True
                await asyncio.wait(pending, timeout=5)
        state = self.state
        if state is not None:
            for name, tunnel in list(state.tunnels.items()):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(tunnel.stop)
            with contextlib.suppress(Exception):
                await state.router.aclose()
            with contextlib.suppress(Exception):
                state.store.close()


def _print(text: str) -> None:
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def _install_signals(node: Node) -> Callable[[], None]:
    """Ctrl+C / SIGTERM -> graceful stop; a second Ctrl+C forces the servers down."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    loop = asyncio.get_running_loop()
    hits = {"n": 0}

    def handler(signum, frame) -> None:
        hits["n"] += 1
        if hits["n"] > 1:
            for srv in node.servers:
                srv.server.force_exit = True
        loop.call_soon_threadsafe(node.request_stop)

    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)  # Windows: closing the console window
    old = {}
    for sig in sigs:
        with contextlib.suppress(ValueError, OSError):
            old[sig] = signal.signal(sig, handler)

    def restore() -> None:
        for sig, h in old.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, h)

    return restore


async def run_node(config: Config, **kwargs) -> int:
    """Start the node, run until Ctrl+C / SIGTERM, stop cleanly. Returns an exit code."""
    node = Node(config, **kwargs)
    restore = _install_signals(node)
    try:
        try:
            await node.start()
        except NodeError as exc:
            log.error("%s", exc)
            _print(f"ERROR: {exc}")
            return 2
        await node.wait()
        return 0
    finally:
        await node.stop()
        restore()
