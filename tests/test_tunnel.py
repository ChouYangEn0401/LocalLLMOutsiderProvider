"""TunnelManager with a fake cloudflared (a small Python script), find_cloudflared."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from llmop.config import TunnelSettings
from llmop.store import Store
from llmop.tunnel import (
    PUBLIC_URL_FILE,
    URL_RE,
    TunnelManager,
    clear_stale_public_url,
    conflicting_config_files,
    find_cloudflared,
)

LOCAL = "http://127.0.0.1:8780"

# Prints what cloudflared prints. FAKE_CF_MODE: stay (default) | exit | ignore-term.
# Every start gets a new URL (fake-tunnel-0, fake-tunnel-1, ...), like the real one.
FAKE_CLOUDFLARED = r'''
import os, signal, sys, time
from pathlib import Path

counter = Path(os.environ["FAKE_CF_COUNTER"])
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
mode = os.environ.get("FAKE_CF_MODE", "stay")
if mode == "ignore-term" and hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("2026-11-13T00:00:00Z INF Thank you for trying Cloudflare Tunnel.", flush=True)
if "--url" in sys.argv:
    print('2026-11-13T00:00:00Z ERR failed to reach Post "https://api.trycloudflare.com/tunnel", retrying', flush=True)
    print("2026-11-13T00:00:00Z INF +--------------------------------------------+", flush=True)
    print(f"2026-11-13T00:00:00Z INF |  https://fake-tunnel-{n}.trycloudflare.com  |", flush=True)
    print("2026-11-13T00:00:00Z INF +--------------------------------------------+", flush=True)
time.sleep(0.1)
print("2026-11-13T00:00:01Z INF Registered tunnel connection connIndex=0 location=tpe01 protocol=quic", flush=True)
if mode == "exit":
    time.sleep(0.3)
    sys.exit(3)
time.sleep(60)
'''


class FakePopen:
    """Runs the fake script instead of cloudflared, keeping cloudflared's arguments."""

    def __init__(self, script: Path):
        self.script = script
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.procs: list[subprocess.Popen] = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        self.kwargs.append(kwargs)
        proc = subprocess.Popen([sys.executable, str(self.script), *cmd[1:]], **kwargs)
        self.procs.append(proc)
        return proc


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_cloudflared.py"
    script.write_text(FAKE_CLOUDFLARED, encoding="utf-8")
    monkeypatch.setenv("FAKE_CF_COUNTER", str(tmp_path / "counter.txt"))
    monkeypatch.delenv("FAKE_CF_MODE", raising=False)
    return FakePopen(script)


@pytest.fixture
def exe(tmp_path) -> str:
    """An existing file for find_cloudflared (FakePopen never executes it)."""
    p = tmp_path / "bin" / "cloudflared.exe"
    p.parent.mkdir()
    p.write_text("not really cloudflared", encoding="utf-8")
    return str(p)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "llmop.sqlite3")
    yield s
    s.close()


def make(fake, exe, tmp_path, store=None, *, printed=None, urls=None, **kw):
    settings = kw.pop("settings", None) or TunnelSettings(enabled=True, cloudflared=exe)
    return TunnelManager(
        settings, LOCAL, store, tmp_path / "data",
        on_url=(urls.append if urls is not None else None),
        popen=fake,
        out=(printed.append if printed is not None else (lambda s: None)),
        home=tmp_path / "home",
        **kw,
    )


def wait_for(cond, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ------------------------------------------------------------------ quick tunnel
def test_quick_tunnel_publishes_url_and_stop_kills_cloudflared(fake, exe, tmp_path, store):
    printed: list[str] = []
    urls: list[str] = []
    mgr = make(fake, exe, tmp_path, store, printed=printed, urls=urls)
    assert mgr.status == "off"
    try:
        assert mgr.start() is True
        assert mgr.wait_ready(15), (mgr.status, mgr.last_error, printed)
        url = "https://fake-tunnel-0.trycloudflare.com"
        assert mgr.url == url
        assert urls == [url]  # api.trycloudflare.com in the error line is not the URL
        assert store.kv_get("public_url")[0] == url
        assert (tmp_path / "data" / PUBLIC_URL_FILE).read_text(encoding="utf-8").strip() == url
        assert mgr.started_at is not None and mgr.ready_at is not None
        text = "\n".join(printed)
        assert url in text and f"{url}/v1" in text  # the banner
        assert "ERR failed to reach" in text  # cloudflared errors are shown
        assert fake.commands == [[exe, "tunnel", "--no-autoupdate", "--url", LOCAL]]
        assert fake.kwargs[0]["stderr"] == subprocess.STDOUT
        proc = fake.procs[0]
        assert proc.poll() is None  # still running
    finally:
        mgr.stop()
    assert mgr.status == "stopped"
    assert proc.poll() is not None
    assert mgr.url is None
    assert store.kv_get("public_url")[0] == ""
    assert not (tmp_path / "data" / PUBLIC_URL_FILE).exists()


def test_cloudflared_is_restarted_with_a_new_url_after_it_exits(fake, exe, tmp_path, store, monkeypatch):
    monkeypatch.setenv("FAKE_CF_MODE", "exit")
    printed: list[str] = []
    urls: list[str] = []
    mgr = make(fake, exe, tmp_path, store, printed=printed, urls=urls, backoff=(0.05, 0.2))
    try:
        mgr.start()
        assert wait_for(lambda: len(urls) >= 2), (urls, printed)
        assert urls[:2] == ["https://fake-tunnel-0.trycloudflare.com",
                            "https://fake-tunnel-1.trycloudflare.com"]
        assert mgr.restarts >= 1
        assert any("exited (code 3)" in line for line in printed)
        assert wait_for(lambda: store.kv_get("public_url")[0].startswith("https://fake-tunnel-"))
    finally:
        mgr.stop()
    assert mgr.status == "stopped"
    assert all(p.poll() is not None for p in fake.procs)


def test_cloudflared_that_cannot_start_is_retried_with_backoff(exe, tmp_path):
    calls: list[float] = []

    def broken_popen(cmd, **kw):
        calls.append(time.monotonic())
        raise OSError("boom")

    mgr = TunnelManager(TunnelSettings(cloudflared=exe), LOCAL, None, None, popen=broken_popen,
                        out=lambda s: None, backoff=(0.05, 0.1), home=tmp_path)
    try:
        assert mgr.start()
        assert wait_for(lambda: len(calls) >= 3)
        assert mgr.status == "error"
        assert "boom" in mgr.last_error
    finally:
        mgr.stop()
    assert mgr.status == "stopped"


@pytest.mark.skipif(sys.platform == "win32", reason="terminate() is already a hard kill on Windows")
def test_stop_kills_cloudflared_that_ignores_terminate(fake, exe, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CF_MODE", "ignore-term")
    mgr = make(fake, exe, tmp_path, kill_timeout=0.3)
    mgr.start()
    assert mgr.wait_ready(15)
    t0 = time.monotonic()
    mgr.stop()
    assert time.monotonic() - t0 < 5
    assert fake.procs[0].poll() is not None


# ------------------------------------------------------------------ named tunnel
def test_named_tunnel_uses_token_and_configured_public_url(fake, exe, tmp_path, store):
    settings = TunnelSettings(enabled=True, mode="named", cloudflared=exe, token="tok-123",
                              extra_args=["--protocol", "http2"])
    urls: list[str] = []
    mgr = make(fake, exe, tmp_path, store, urls=urls, settings=settings,
               public_url="https://llm.example.com/")
    try:
        mgr.start()
        assert mgr.wait_ready(15)
        assert fake.commands == [[exe, "tunnel", "--no-autoupdate", "--protocol", "http2",
                                  "run", "--token", "tok-123"]]
        assert mgr.url == "https://llm.example.com"
        assert urls == ["https://llm.example.com"]
        assert store.kv_get("public_url")[0] == "https://llm.example.com"
    finally:
        mgr.stop()


def test_named_tunnel_without_token_does_not_start(fake, exe, tmp_path):
    mgr = make(fake, exe, tmp_path, settings=TunnelSettings(mode="named", cloudflared=exe))
    assert mgr.start() is False
    assert mgr.status == "error"
    assert "token" in mgr.last_error
    assert fake.commands == []


def test_quick_tunnel_extra_args_come_before_url(fake, exe, tmp_path):
    mgr = make(fake, exe, tmp_path,
               settings=TunnelSettings(cloudflared=exe, extra_args=["--edge-ip-version", "4"]))
    assert mgr.command("cf") == ["cf", "tunnel", "--no-autoupdate", "--edge-ip-version", "4",
                                 "--url", LOCAL]


# ------------------------------------------------------------------ refusing to start
def test_missing_cloudflared_gives_the_winget_hint(fake, tmp_path, monkeypatch):
    _empty_search_path(tmp_path, monkeypatch)
    printed: list[str] = []
    mgr = make(fake, str(tmp_path / "nope" / "cloudflared.exe"), tmp_path, printed=printed,
               settings=TunnelSettings(cloudflared="cloudflared"))
    assert mgr.start() is False
    assert mgr.status == "error"
    assert "winget install --id Cloudflare.cloudflared" in mgr.last_error
    assert any("winget install" in line for line in printed)
    assert fake.commands == []


def test_second_tunnel_for_the_same_data_dir_is_refused(fake, exe, tmp_path):
    first = make(fake, exe, tmp_path)
    second = make(fake, exe, tmp_path)
    try:
        assert first.start()
        assert second.start() is False
        assert "already running" in second.last_error
        first.stop()
        assert second.start() is True  # the lock is released by stop()
    finally:
        first.stop()
        second.stop()


def test_warns_when_cloudflared_has_its_own_config_file(fake, exe, tmp_path):
    cf_cfg = tmp_path / "home" / ".cloudflared" / "config.yaml"
    cf_cfg.parent.mkdir(parents=True)
    cf_cfg.write_text("tunnel: abc\n", encoding="utf-8")
    printed: list[str] = []
    mgr = make(fake, exe, tmp_path, printed=printed)
    try:
        mgr.start()
        assert any("config.yaml exists" in line for line in printed)
    finally:
        mgr.stop()
    assert conflicting_config_files(tmp_path / "home")[0] == cf_cfg


def test_stale_url_is_cleared_only_when_no_tunnel_runs(fake, exe, tmp_path, store):
    data = tmp_path / "data"
    data.mkdir()
    store.kv_set("public_url", "https://dead-old-tunnel.trycloudflare.com")
    (data / PUBLIC_URL_FILE).write_text("https://dead-old-tunnel.trycloudflare.com\n", encoding="utf-8")
    assert clear_stale_public_url(store, data) is True
    assert store.kv_get("public_url")[0] == ""
    assert not (data / PUBLIC_URL_FILE).exists()
    assert clear_stale_public_url(store, data) is False  # nothing left to clear

    mgr = make(fake, exe, tmp_path, store)
    try:
        mgr.start()
        assert mgr.wait_ready(15)
        assert clear_stale_public_url(store, data) is False  # a live tunnel keeps its URL
        assert store.kv_get("public_url")[0] == mgr.url
        assert (data / PUBLIC_URL_FILE).exists()
    finally:
        mgr.stop()


def test_stop_before_start_is_harmless(fake, exe, tmp_path):
    mgr = make(fake, exe, tmp_path)
    mgr.stop()
    assert mgr.status == "off"
    assert mgr.as_dict()["status"] == "off"


# ------------------------------------------------------------------ find_cloudflared
def _empty_search_path(tmp_path, monkeypatch):
    empty = tmp_path / "empty-path"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    for var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(empty)  # Windows' shutil.which also looks in the current folder


def test_find_cloudflared_explicit_path(tmp_path, monkeypatch, exe):
    _empty_search_path(tmp_path, monkeypatch)
    assert find_cloudflared(exe) == exe
    assert find_cloudflared(f'"{exe}"') == exe  # quotes pasted from Explorer


def test_find_cloudflared_on_path(tmp_path, monkeypatch):
    _empty_search_path(tmp_path, monkeypatch)
    bindir = tmp_path / "onpath"
    bindir.mkdir()
    for name in ("cloudflared", "cloudflared.exe"):
        p = bindir / name
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    found = find_cloudflared("cloudflared")
    assert found and Path(found).parent == bindir
    # a configured path that does not exist falls back to PATH
    assert find_cloudflared(str(tmp_path / "gone" / "cloudflared.exe")) == found


def test_find_cloudflared_in_program_files(tmp_path, monkeypatch):
    _empty_search_path(tmp_path, monkeypatch)
    pf86 = tmp_path / "Program Files (x86)"
    target = pf86 / "cloudflared" / "cloudflared.exe"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles(x86)", str(pf86))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    assert find_cloudflared("cloudflared") == str(target)


def test_find_cloudflared_missing(tmp_path, monkeypatch):
    _empty_search_path(tmp_path, monkeypatch)
    assert find_cloudflared("cloudflared") is None
    assert find_cloudflared("") is None


def test_url_regex():
    line = "2026 INF |  https://pink-cat-1a2b.trycloudflare.com  |"
    assert URL_RE.search(line).group(0) == "https://pink-cat-1a2b.trycloudflare.com"
    assert URL_RE.search("https://example.com") is None


def test_no_console_window_flag_only_on_windows():
    from llmop.tunnel import _creationflags

    if sys.platform != "win32":
        assert _creationflags() == 0
    else:
        assert _creationflags() in (0, getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


# ------------------------------------------------------------------ v0.2: two named tunnels (T-9)
def test_ui_and_api_tunnels_are_independent(fake, exe, tmp_path, store):
    """"ui" keeps the v0.1 store key / file / lock; "api" has its own, so both run at
    once and stopping one never touches the other's URL."""
    ui = make(fake, exe, tmp_path, store, name="ui")
    api = TunnelManager(TunnelSettings(enabled=True, cloudflared=exe), "http://127.0.0.1:8782",
                        store, tmp_path / "data", popen=fake, out=lambda s: None,
                        home=tmp_path / "home", name="api", single_instance=False)
    try:
        assert ui.start() and wait_for(lambda: ui.url)  # the fake numbers its URLs in order
        assert api.start() and wait_for(lambda: api.url)
        assert ui.url != api.url
        assert store.kv_get("public_url")[0] == ui.url
        assert store.kv_get("api_tunnel_url")[0] == api.url
        assert (tmp_path / "data" / "api_tunnel_url.txt").read_text().strip() == api.url
        assert fake.commands[-1][-1] == "http://127.0.0.1:8782"
        api.stop()
        assert store.kv_get("public_url")[0] == ui.url and ui.running
        assert store.kv_get("api_tunnel_url")[0] == ""
    finally:
        ui.stop()
        api.stop()


def test_a_replaced_tunnel_does_not_erase_its_successors_url(fake, exe, tmp_path, store):
    def api_tunnel():
        return TunnelManager(TunnelSettings(enabled=True, cloudflared=exe), "http://127.0.0.1:8782",
                             store, tmp_path / "data", popen=fake, out=lambda s: None,
                             home=tmp_path / "home", name="api", single_instance=False)

    old, new = api_tunnel(), api_tunnel()
    try:
        assert old.start() and wait_for(lambda: old.url)
        assert new.start() and wait_for(lambda: new.url)
        old.stop()  # new first, then the old one (API link host change)
        assert store.kv_get("api_tunnel_url")[0] == new.url
        assert (tmp_path / "data" / "api_tunnel_url.txt").read_text().strip() == new.url
    finally:
        old.stop()
        new.stop()


def test_refuses_to_tunnel_to_the_console_or_a_backend(fake, exe, tmp_path):
    from llmop.config import parse_config
    from llmop.tunnel import forbidden_tunnel_ports

    cfg = parse_config({"backends": [{"type": "ollama"},
                                     {"name": "v", "type": "vllm", "base_url": "http://127.0.0.1:8000/v1"}],
                        "listeners": {"local": 9781}})
    ports = forbidden_tunnel_ports(cfg)
    assert ports == {9781, 11434, 8000}
    for url in ("http://127.0.0.1:9781", "http://localhost:11434/", "http://127.0.0.1:8000"):
        mgr = TunnelManager(TunnelSettings(enabled=True, cloudflared=exe), url, None,
                            tmp_path / "data", popen=fake, out=lambda s: None,
                            home=tmp_path / "home", forbidden_ports=ports)
        assert mgr.start() is False
        assert mgr.status == "error" and "refusing" in mgr.last_error
    assert fake.commands == []  # cloudflared was never started
    ok = TunnelManager(TunnelSettings(enabled=True, cloudflared=exe), "http://127.0.0.1:8782", None,
                       tmp_path / "data", popen=fake, out=lambda s: None,
                       home=tmp_path / "home", forbidden_ports=ports)
    try:
        assert ok.start()
    finally:
        ok.stop()


def test_clear_stale_url_per_tunnel_name(tmp_path, store):
    data = tmp_path / "data"
    data.mkdir()
    store.kv_set("public_url", "https://ui.trycloudflare.com")
    store.kv_set("api_tunnel_url", "https://api.trycloudflare.com")
    assert clear_stale_public_url(store, data, "api")
    assert store.kv_get("api_tunnel_url")[0] == ""
    assert store.kv_get("public_url")[0] == "https://ui.trycloudflare.com"
    assert clear_stale_public_url(store, data, "ui")
    assert store.kv_get("public_url")[0] == ""
