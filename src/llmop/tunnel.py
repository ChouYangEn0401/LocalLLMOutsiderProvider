"""Cloudflare Tunnel supervisor: runs ``cloudflared`` in a background thread.

* ``quick`` mode: ``cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8780``
  gives a random ``https://xxx.trycloudflare.com`` address (no account, no domain,
  no open port). The address changes every time cloudflared starts.
* ``named`` mode: ``cloudflared tunnel --no-autoupdate run --token <TOKEN>`` runs a
  tunnel created in the Cloudflare dashboard (your own, stable hostname). The
  public address then comes from ``server.public_url`` in config.yaml.

cloudflared's output is read line by line here in Python (never through a shell
or PowerShell pipeline, which buffers and hides it). When the quick-tunnel URL
shows up it is stored in ``store.kv("public_url")`` (the web UI shows it) and in
``<data_dir>/public_url.txt``. If cloudflared exits it is restarted with a
backoff (5 s doubling to 60 s) until :meth:`TunnelManager.stop` is called.

Things worth knowing about quick tunnels (Cloudflare documentation / experience):

* a brand-new URL takes 10-60 s before it resolves everywhere;
* streaming (SSE) is not officially supported, and at most 200 requests may be in
  flight; a request that gets no response byte for ~100 s fails with HTTP 524;
* they refuse to start when ``~/.cloudflared/config.yaml`` exists (we warn);
* running two tunnels for the same server is pointless: a lock file in the data
  folder makes the second one refuse to start.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Callable

from .config import TunnelSettings

log = logging.getLogger("llmop.tunnel")

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_RE = re.compile(r"Registered tunnel connection")
# cloudflared log levels: DBG INF WRN ERR FTL
ERROR_RE = re.compile(r"\b(ERR|FTL)\b")
# api.trycloudflare.com is the service that hands out quick tunnels; it appears in
# error messages ("failed to request quick Tunnel: Post https://api.trycloudflare.com")
NOT_A_TUNNEL_HOSTS = {"api"}

WINGET_HINT = "winget install --id Cloudflare.cloudflared"
STATUSES = ("off", "starting", "ready", "restarting", "error", "stopped")
PUBLIC_URL_FILE = "public_url.txt"
LOCK_FILE = "tunnel.lock"


# ---------------------------------------------------------------- locating cloudflared
def _windows_candidates() -> list[Path]:
    """Where the winget / MSI installer puts cloudflared.exe (PATH is only refreshed
    for *new* windows, so right after ``winget install`` it is not on PATH yet)."""
    out: list[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            out.append(Path(base, "cloudflared", "cloudflared.exe"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local, "Microsoft", "WinGet", "Links", "cloudflared.exe"))
    return out


def find_cloudflared(configured: str = "cloudflared") -> str | None:
    """Return the path of the cloudflared executable, or ``None``.

    Order: an explicit path from the config (``tunnel.cloudflared``), the PATH, then
    the usual Windows install folders.
    """
    name = (configured or "").strip().strip('"') or "cloudflared"
    expanded = os.path.expandvars(os.path.expanduser(name))
    is_path = os.path.isabs(expanded) or os.sep in expanded or (
        os.altsep is not None and os.altsep in expanded
    )
    if is_path:
        if Path(expanded).is_file():
            return str(Path(expanded))
        log.warning("tunnel.cloudflared=%r does not exist; searching PATH instead", configured)
    else:
        found = shutil.which(expanded)
        if found:
            return found
    found = shutil.which("cloudflared")
    if found:
        return found
    for cand in _windows_candidates():
        if cand.is_file():
            return str(cand)
    return None


def conflicting_config_files(home: Path | None = None) -> list[Path]:
    """cloudflared's own config files. If one exists, ``cloudflared tunnel --url``
    (quick tunnel) refuses to start / starts the configured tunnel instead."""
    home = home if home is not None else Path.home()
    dirs = [home / ".cloudflared", home / ".cloudflare-warp", home / "cloudflare-warp"]
    if os.name != "nt":
        dirs += [Path("/etc/cloudflared"), Path("/usr/local/etc/cloudflared")]
    found = []
    for d in dirs:
        for fname in ("config.yml", "config.yaml"):
            p = d / fname
            try:
                if p.is_file():
                    found.append(p)
            except OSError:
                continue
    return found


def _creationflags() -> int:
    """On Windows, don't pop up a black console window for cloudflared when we have
    no console ourselves (pythonw / service). With a console (Start.bat) cloudflared
    shares it, so closing that window also ends cloudflared."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        has_console = False
    return 0 if has_console else getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _default_out(text: str) -> None:
    try:
        print(text, flush=True)
    except (OSError, ValueError):  # closed / missing console (pythonw)
        pass


# ---------------------------------------------------------------- the manager
class TunnelManager:
    """Keeps one cloudflared process running and publishes its public URL.

    Attributes read by the web UI / CLI: ``url`` (str or None), ``status`` (one of
    :data:`STATUSES`), ``last_error``, ``started_at`` (when the current cloudflared
    process was started), ``ready_at``, ``restarts``, ``mode``.
    """

    def __init__(
        self,
        settings: TunnelSettings,
        local_url: str,
        store=None,
        data_dir: Path | str | None = None,
        on_url: Callable[[str], None] | None = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        *,
        public_url: str = "",
        out: Callable[[str], None] | None = None,
        backoff: tuple[float, float] = (5.0, 60.0),
        kill_timeout: float = 5.0,
        home: Path | None = None,
    ):
        self.settings = settings
        self.local_url = local_url.rstrip("/")
        self.store = store
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.on_url = on_url
        self.public_url_hint = (public_url or "").rstrip("/")
        self._popen = popen
        self._out = out or _default_out
        self.backoff_start, self.backoff_max = backoff
        self.kill_timeout = kill_timeout
        self._home = home

        self.url: str | None = None
        self.status: str = "off"
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.ready_at: float | None = None
        self.restarts: int = 0
        self.exe: str | None = None

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lockfile: IO | None = None
        self._ready_seen = False

    # ------------------------------------------------------------ public API
    @property
    def mode(self) -> str:
        return "named" if self.settings.mode == "named" else "quick"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def command(self, exe: str) -> list[str]:
        extra = [str(a) for a in (self.settings.extra_args or [])]
        if self.mode == "named":
            return [exe, "tunnel", "--no-autoupdate", *extra, "run", "--token", self.settings.token]
        return [exe, "tunnel", "--no-autoupdate", *extra, "--url", self.local_url]

    def start(self) -> bool:
        """Start the background thread. Returns False (and sets ``status="error"``
        with a readable ``last_error``) when it cannot start at all."""
        if self.running:
            return True
        if self.mode == "named" and not self.settings.token:
            return self._fail(
                "tunnel.mode is 'named' but tunnel.token is empty. Put the token from the "
                "Cloudflare dashboard in config.yaml (token: env:CLOUDFLARE_TUNNEL_TOKEN) "
                "or use mode: quick."
            )
        exe = find_cloudflared(self.settings.cloudflared)
        if exe is None:
            return self._fail(
                "cloudflared was not found. Install it once with:  "
                f"{WINGET_HINT}   (then open a new window), or set tunnel.cloudflared "
                "in config.yaml to the full path of cloudflared.exe."
            )
        if self.mode == "quick":
            for cfg_file in conflicting_config_files(self._home):
                self._say(
                    f"[tunnel] WARNING: {cfg_file} exists. Quick tunnels do not start while "
                    "cloudflared has its own config file; rename it (e.g. config.yaml.off) "
                    "or use tunnel.mode: named."
                )
        if not self._acquire_lock():
            return self._fail(
                "another llmop tunnel is already running for this data folder "
                f"({self.data_dir}). Two tunnels for one server are pointless; close the "
                "other window (llmop serve --tunnel / llmop tunnel) first."
            )
        self.exe = exe
        self._stop.clear()
        self.status = "starting"
        self.last_error = None
        self._thread = threading.Thread(target=self._run, name="llmop-tunnel", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 15.0) -> None:
        """Stop cloudflared (terminate, wait, kill) and forget the public URL."""
        was_running = self._thread is not None
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._clear_url()
        self._release_lock()
        if was_running:
            self.status = "stopped"

    def wait(self) -> None:
        """Block until the manager stops (used by ``llmop tunnel``). The short join
        timeout keeps Ctrl+C working on Windows."""
        while self._thread is not None and self._thread.is_alive():
            self._thread.join(0.5)

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status == "ready":
                return True
            time.sleep(0.05)
        return self.status == "ready"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "url": self.url,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "ready_at": self.ready_at,
            "restarts": self.restarts,
        }

    # ------------------------------------------------------------ internals
    def _say(self, text: str) -> None:
        log.debug("%s", text)  # the console gets it via out(); avoid printing it twice
        self._out(text)

    def _fail(self, message: str) -> bool:
        self.status = "error"
        self.last_error = message
        self._say(f"[tunnel] ERROR: {message}")
        return False

    def _run(self) -> None:
        delay = self.backoff_start
        while not self._stop.is_set():
            t0 = time.monotonic()
            code = self._run_once()
            if self._stop.is_set():
                break
            # Ctrl+C reaches cloudflared too; give stop() a moment before "restarting".
            if self._stop.wait(min(0.3, self.backoff_start)):
                break
            if time.monotonic() - t0 >= 60:
                delay = self.backoff_start  # it ran fine for a while: restart quickly
            self._clear_url()
            self.restarts += 1
            if code is None:
                self.status = "error"  # could not even start it; keep trying slowly
            else:
                self.status = "restarting"
                self._say(f"[tunnel] cloudflared exited (code {code}); restarting in {delay:g}s")
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.backoff_max)

    def _run_once(self) -> int | None:
        cmd = self.command(self.exe or "cloudflared")
        kwargs: dict = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        flags = _creationflags()
        if flags:
            kwargs["creationflags"] = flags
        try:
            proc = self._popen(cmd, **kwargs)
        except OSError as exc:
            self.last_error = f"cannot start cloudflared ({self.exe}): {exc}"
            self._say(f"[tunnel] ERROR: {self.last_error}")
            return None
        with self._lock:
            self._proc = proc
        if self._stop.is_set():  # stop() raced with the start
            self._terminate(proc)
        self.started_at = time.time()
        self.ready_at = None
        self._ready_seen = False
        if self.status == "error":  # an earlier attempt could not start it
            self.status = "restarting"
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._handle_line(line)
            return proc.wait()
        except (OSError, ValueError):  # pipe closed under us during stop()
            return proc.poll()
        finally:
            self._terminate(proc)
            with self._lock:
                if self._proc is proc:
                    self._proc = None

    def _handle_line(self, raw: str) -> None:
        line = raw.rstrip()
        if not line:
            return
        log.debug("cloudflared: %s", line)
        if self.mode == "quick" and self.url is None:
            for m in URL_RE.finditer(line):
                host = m.group(0)[len("https://"):].split(".", 1)[0]
                if host not in NOT_A_TUNNEL_HOSTS:
                    self._publish(m.group(0))
                    return
        if not self._ready_seen and READY_RE.search(line):
            self._ready_seen = True
            self._on_ready()
            return
        if ERROR_RE.search(line):
            self.last_error = line[-500:]
            self._say(f"[cloudflared] {line}")

    def _on_ready(self) -> None:
        self.status = "ready"
        self.ready_at = time.time()
        self.last_error = None
        if self.mode == "named":
            if self.public_url_hint:
                self._publish(self.public_url_hint)
            else:
                self._say(
                    "[tunnel] named tunnel connected. Set server.public_url in config.yaml "
                    "to your tunnel's hostname so the web page shows the right address."
                )
        else:
            self._say("[tunnel] connected to Cloudflare (ready).")

    def _publish(self, url: str) -> None:
        url = url.rstrip("/")
        self.url = url
        if self.store is not None:
            try:
                self.store.kv_set("public_url", url)
            except Exception as exc:  # never let bookkeeping kill the tunnel
                log.warning("could not store public_url: %s", exc)
        if self.data_dir is not None:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                (self.data_dir / PUBLIC_URL_FILE).write_text(url + "\n", encoding="utf-8")
            except OSError as exc:
                log.warning("could not write %s: %s", PUBLIC_URL_FILE, exc)
        self._banner(url)
        if self.on_url is not None:
            try:
                self.on_url(url)
            except Exception:
                log.exception("tunnel on_url callback failed")

    def _clear_url(self) -> None:
        had = self.url is not None
        self.url = None
        if not had:
            return
        if self.store is not None:
            try:
                self.store.kv_set("public_url", "")
            except Exception:
                pass
        if self.data_dir is not None:
            try:
                (self.data_dir / PUBLIC_URL_FILE).unlink()
            except OSError:
                pass

    def _banner(self, url: str) -> None:
        line = "=" * 66
        where = (
            str(self.data_dir / PUBLIC_URL_FILE) if self.data_dir is not None else PUBLIC_URL_FILE
        )
        rows = [
            "",
            line,
            f"  PUBLIC URL ({'Cloudflare quick tunnel' if self.mode == 'quick' else 'named tunnel'}):",
            "",
            f"      {url}",
            "",
            f"  Web login : {url}/ui/login",
            f"  API base  : {url}/v1      (OpenAI-compatible, needs an API key)",
        ]
        if self.mode == "quick":
            rows += [
                "",
                "  - A brand-new address can take 10-60 seconds to work everywhere.",
                "  - It changes every time the tunnel restarts. The current one is",
                f"    always in {where}",
                "    and on the web dashboard.",
                "  - Quick tunnels: streaming is best effort, max 200 requests at",
                "    once, and a request with no reply for ~100 s fails (error 524).",
            ]
        rows += [line, ""]
        for r in rows:
            self._out(r)
        log.info("public URL: %s", url)

    def _terminate(self, proc: subprocess.Popen) -> None:
        """terminate -> wait(kill_timeout) -> kill. Safe to call twice."""
        try:
            if proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=self.kill_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    # ------------------------------------------------------------ single-instance lock
    def _acquire_lock(self) -> bool:
        if self.data_dir is None:
            return True
        result = _try_lock(self.data_dir)
        if result is False:
            return False
        self._lockfile = result
        return True

    def _release_lock(self) -> None:
        fh, self._lockfile = self._lockfile, None
        _unlock(fh)


def _try_lock(data_dir: Path) -> "IO | None | bool":
    """Lock ``<data_dir>/tunnel.lock`` without waiting. Returns the open file (keep it
    to hold the lock), ``False`` if another process holds it, or ``None`` if locking
    is not possible here (then we carry on without a lock)."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fh = open(data_dir / LOCK_FILE, "a+")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    return fh


def _unlock(fh: "IO | None") -> None:
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fh.close()


def clear_stale_public_url(store, data_dir: Path | str | None) -> bool:
    """Forget a quick-tunnel URL left behind by a tunnel that was killed (window
    closed, crash). Only when no tunnel holds the lock for ``data_dir``: a separate
    ``llmop tunnel`` that is still running keeps its URL. Returns True if cleared."""
    if data_dir is None:
        return False
    data_dir = Path(data_dir)
    fh = _try_lock(data_dir)
    if fh is False:
        return False  # a live tunnel owns the URL
    try:
        cleared = False
        if store is not None:
            got = store.kv_get("public_url")
            if got and got[0]:
                store.kv_set("public_url", "")
                cleared = True
        try:
            (data_dir / PUBLIC_URL_FILE).unlink()
            cleared = True
        except OSError:
            pass
        return cleared
    finally:
        _unlock(fh)
