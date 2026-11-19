"""T-F: regression tests for the review findings fixed after T-R
(docs/security/REVIEW_v0.2.md, section "修正後狀態").

R-03 / R-04 also have their original tests in ``test_sec_known_gaps.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
import yaml
from aiohttp import web

from llmop import netguard
from llmop.api import forward
from llmop.api.tokens import estimate_prompt_tokens, text_chars
from llmop.apilink import REMOTE_WORKER_REMINDER, ApiLink, worker_ws_url
from llmop.app import build_state, make_scheduler, sync_scheduler
from llmop.config import TunnelSettings, parse_config
from llmop.node import Node, startup_warnings
from llmop.quota import QuotaDenied, QuotaManager
from llmop.scheduler import Scheduler
from llmop.store import Store
from llmop.tunnel import TunnelManager
from llmop.usage import UsageManager

from .conftest import CHAT, FAST_HASH, login, raw_config

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- R-03
async def test_r03_ticket_override_wins_and_low_keeps_the_low_limit():
    sched = Scheduler(slots=6)
    async with sched.slot("bob", "normal", "local", max_concurrent=2):
        second = sched.slot("bob", "normal", "local", max_concurrent=2)
        await asyncio.wait_for(second.__aenter__(), 1)  # normal = 1, but bob may run 2
        assert sched.running == 2
        await second.__aexit__(None, None, None)
    # LOW always uses the low tier's limit (1), whatever the account's own value
    async with sched.slot("bob", "low", "local", max_concurrent=3):
        third = asyncio.ensure_future(sched.slot("bob", "low", "local", max_concurrent=3).__aenter__())
        await asyncio.sleep(0.05)
        try:
            assert sched.running == 1 and sched.waiting == 1
        finally:
            third.cancel()
            await asyncio.gather(third, return_exceptions=True)


def test_r03_lease_carries_the_accounts_own_max_concurrent():
    cfg = parse_config({"accounts": {"bob": {"password_hash": FAST_HASH, "max_concurrent": 2},
                                     "eve": {"password_hash": FAST_HASH}}})
    store = Store(":memory:")
    quota = QuotaManager(store, tables=UsageManager(store, cfg))
    bob, eve = quota.acquire(cfg.accounts["bob"]), quota.acquire(cfg.accounts["eve"])
    assert (bob.max_concurrent, eve.max_concurrent) == (2, 1)
    bob.release()
    eve.release()


async def test_r03_direct_v1_passes_the_override_to_the_scheduler(fake_net):
    cfg = parse_config(raw_config(accounts={
        "bob": {"password_hash": FAST_HASH, "max_concurrent": 2},
        "root": {"password_hash": FAST_HASH, "role": "admin"}}))
    state = build_state(cfg, store=Store(":memory:"), transport=fake_net.transport, scheduler=True)
    fake_net.ollama.first_delay = 0.3
    try:
        acc = state.config.accounts["bob"]
        calls = [forward(state, "chat.completions", dict(CHAT), account=acc, key_id=None,
                         zone="local") for _ in range(2)]
        results = await asyncio.gather(*calls)
        assert [r.status_code for r in results] == [200, 200]
        assert fake_net.ollama.max_active == 2  # both ran at once (NORMAL alone = 1)
    finally:
        await state.router.aclose()


# --------------------------------------------------------------------------- R-04
def test_r04_example_config_keeps_only_requests_per_minute():
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert raw["default_quota"] == {"requests_per_minute": 20}


def test_r04_tables_ignore_the_v01_default_concurrency_but_v01_keeps_it():
    cfg = parse_config({"accounts": {"amy": {"password_hash": FAST_HASH, "tier": "vip1"}}})
    acc = cfg.accounts["amy"]
    store = Store(":memory:")
    tiered = QuotaManager(store, tables=UsageManager(store, cfg))
    leases = [tiered.acquire(acc) for _ in range(5)]  # the scheduler limits concurrency
    for lease in leases:
        lease.release()
    v01 = QuotaManager(store)  # no usage tables: the v0.1 default of 2 still applies
    leases = [v01.acquire(acc), v01.acquire(acc)]
    with pytest.raises(QuotaDenied, match="same time"):
        v01.acquire(acc)
    for lease in leases:
        lease.release()


def test_r04_an_explicit_legacy_cap_still_applies_with_tables():
    cfg = parse_config({"default_quota": {"max_concurrent": 1},
                        "accounts": {"amy": {"password_hash": FAST_HASH, "tier": "vip3"}}})
    store = Store(":memory:")
    quota = QuotaManager(store, tables=UsageManager(store, cfg))
    lease = quota.acquire(cfg.accounts["amy"])
    with pytest.raises(QuotaDenied):
        quota.acquire(cfg.accounts["amy"])
    lease.release()


# --------------------------------------------------------------------------- R-08 / R-19
async def test_r08_websocket_scope_remembers_the_gate_hash():
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)

    state = netguard.GateState(current_hash=netguard.gate_hash("g" * 43))
    mw = netguard.GateMiddleware(app, lambda: state)
    await mw({"type": "websocket", "path": "/" + "g" * 43 + "/worker/ws"}, None, None)
    assert seen[netguard.GATE_HASH_SCOPE_KEY] == netguard.gate_hash("g" * 43)
    assert seen["path"] == "/worker/ws"
    seen.clear()
    await mw({"type": "http", "path": "/" + "g" * 43 + "/v1/models"}, None, None)
    assert netguard.GATE_HASH_SCOPE_KEY not in seen


class _FakeWs:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_r08_node_disconnects_old_gate_workers_after_rotation_and_reminds(tmp_path):
    from llmop.secure.hub import Hub, WorkerConn

    store = Store(":memory:")
    link = ApiLink(store, None, base_url=lambda: "https://api.example")
    hub = Hub(request_timeout_seconds=10)
    old = WorkerConn("w_old", "remote-pc", _FakeWs(), {}, ["m"], 1,
                     gate_hash=netguard.gate_hash(link.gate))
    local = WorkerConn("w_local", "this computer", _FakeWs(), {}, ["m"], 1, gate_hash=None)
    hub.attach(old)
    hub.attach(local)
    out: list[str] = []
    state = SimpleNamespace(api_link=link, hub=hub, scheduler=None, config=None)
    node = Node(parse_config({}), state=state, out=out.append)

    node.check_api_link()  # first look: nothing changed
    assert out == [] and set(hub.workers) == {"w_old", "w_local"}

    link.rotate_path("test", grace_seconds=60)  # old gate still valid during the grace
    node.check_api_link()
    assert any(REMOTE_WORKER_REMINDER in line for line in out)
    assert any("wss://<new API link>/worker/ws" in line for line in out)
    assert not any(link.gate in line for line in out)  # the gate is not printed (log files)
    assert set(hub.workers) == {"w_old", "w_local"}

    link.rotate_path("test again", grace_seconds=0)  # now the first gate is gone
    node.check_api_link()
    await asyncio.sleep(0)
    assert set(hub.workers) == {"w_local"}  # the local worker (no gate) is kept
    assert old.ws.closed and not local.ws.closed
    assert any("remote-pc" in line and "gateway_url" in line for line in out)
    store.close()


def test_r08_cli_rotate_prints_the_worker_reminder(tmp_path, capsys):
    from llmop import cli

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"server": {"data_dir": str(tmp_path / "data")},
                                    "accounts": {"root": {"password_hash": FAST_HASH,
                                                          "role": "admin"}}}), encoding="utf-8")
    assert cli.main(["--config", str(path), "tunnel", "rotate-api"]) == 0
    out = capsys.readouterr().out
    assert REMOTE_WORKER_REMINDER in out and "/worker/ws" in out


def test_r08_worker_ws_url():
    assert worker_ws_url("https://a.example/GATE") == "wss://a.example/GATE/worker/ws"
    assert worker_ws_url("http://127.0.0.1:8782/GATE/") == "ws://127.0.0.1:8782/GATE/worker/ws"


async def test_r19_gate_404_says_the_api_link_may_have_changed(aiohttp_server):
    from llmop.client import sdk

    async def not_found(request):
        return web.Response(status=404, text="Not Found")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", not_found)
    server = await aiohttp_server(app)
    config = SimpleNamespace(base_url=f"http://127.0.0.1:{server.port}/oldgate", api_key="k")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(sdk.ClientError) as err:
            await sdk._call(session, config, "GET", "/models")  # type: ignore[arg-type]
    assert "API 連結可能已更換" in str(err.value.message)


# --------------------------------------------------------------------------- R-10
async def test_r10_vip_keys_only_on_the_local_console(sec):
    csrf = await login(sec.ui, "root")  # root is vip3
    r = await sec.ui.post("/ui/api/keys", json={"name": "vip"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403 and r.json()["error"]["code"] == "local_only"
    csrf = await login(sec.ui, "alice")  # normal: still allowed on the account website
    r = await sec.ui.post("/ui/api/keys", json={"name": "a"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201
    csrf = await login(sec.local, "root")
    r = await sec.local.post("/ui/api/keys", json={"name": "vip"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text


def test_r10_readme_mentions_the_rule():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "本機控制台" in text and "VIP 帳號的 API 金鑰" in text


# --------------------------------------------------------------------------- R-11
def test_r11_worker_init_points_at_the_local_listener(tmp_path, capsys):
    from llmop.worker import cli as worker_cli
    from llmop.worker.config import default_config

    assert default_config()["gateway_url"] == f"ws://127.0.0.1:{parse_config({}).listeners.local}/worker/ws"
    assert worker_cli.cmd_init(SimpleNamespace(), tmp_path) == 0
    out = capsys.readouterr().out
    assert "wss://<API 連結>/worker/ws" in out
    written = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert written["gateway_url"] == "ws://127.0.0.1:8781/worker/ws"


# --------------------------------------------------------------------------- R-12
def test_r12_example_config_starts_without_a_slots_warning():
    from llmop.config import load_config

    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.queue.slots == sum(b.max_concurrency for b in cfg.backends)
    assert not any("queue.slots" in w for w in startup_warnings(cfg))
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "split the context" in text
    mismatch = parse_config({"backends": [{"name": "o", "type": "ollama",
                                           "base_url": "http://127.0.0.1:11434",
                                           "max_concurrency": 2}]})
    assert any("queue.slots is 6" in w for w in startup_warnings(mismatch))


# --------------------------------------------------------------------------- R-13
async def test_r13_scheduler_follows_config_reload():
    cfg = parse_config({"queue": {"slots": 1}})
    sched = make_scheduler(cfg)
    first = sched.slot("a", "vip3", "local")
    await first.__aenter__()
    waiting = asyncio.ensure_future(sched.slot("b", "vip3", "local").__aenter__())
    await asyncio.sleep(0.02)
    assert (sched.running, sched.waiting) == (1, 1)
    new = parse_config({"queue": {"slots": 2, "max_length": 5},
                        "tiers": {"normal": {"max_concurrent": 2}},
                        "accounts": {"bob": {"password_hash": FAST_HASH, "max_concurrent": 3}}})
    assert sync_scheduler(sched, new) is True
    await asyncio.wait_for(waiting, 1)  # the second slot opened at once
    assert (sched.slots, sched.max_length, sched.running) == (2, 5, 2)
    assert sched.max_concurrent["normal"] == 2 and sched.account_max_concurrent["bob"] == 3
    assert first.ticket.state == "running"  # running requests untouched
    assert sync_scheduler(sched, new) is False  # same config: nothing to do
    await first.__aexit__(None, None, None)
    sched._release(waiting.result(), "done")
    assert sync_scheduler(Scheduler(slots=3), new) is False  # not built from a config


# --------------------------------------------------------------------------- R-15
async def test_r15_stream_cancelled_before_the_first_token_charges_the_prompt(fake_net):
    cfg = parse_config(raw_config())
    state = build_state(cfg, store=Store(":memory:"), transport=fake_net.transport, scheduler=True)
    fake_net.ollama.first_delay = 5.0  # the backend is still reading the prompt
    acc = state.config.accounts["alice"]
    body = {**CHAT, "stream": True,
            "messages": [{"role": "user", "content": "long prompt " * 200}]}
    try:
        resp = await forward(state, "chat.completions", body, account=acc, key_id=None,
                             zone="local")

        async def send(message):
            pass

        async def receive():
            await asyncio.sleep(0.2)
            return {"type": "http.disconnect"}

        started = time.monotonic()
        await resp({"type": "http", "asgi": {"spec_version": "2.3"}, "method": "POST",
                    "path": "/"}, receive, send)
        await asyncio.sleep(0)
        assert time.monotonic() - started < 2
        [row] = state.store.recent_usage("alice")
        assert row["status"] == "cancelled" and row["usage_source"] == "estimate"
        assert row["prompt_tokens"] >= estimate_prompt_tokens("chat.completions", body) > 500
        assert row["completion_tokens"] == 0
        assert state.usage.status(acc)["bucket"]["level"] < 80_000  # charged to the table
    finally:
        await state.router.aclose()


# --------------------------------------------------------------------------- R-17
async def test_r17_public_health_has_no_version(sec):
    from llmop import __version__

    assert (await sec.ui.get("/health")).json() == {"status": "ok"}
    assert (await sec.api.get("/health")).json() == {"status": "ok"}
    assert (await sec.local.get("/health")).json()["version"] == __version__
    page = await sec.ui.get("/ui/login")
    assert page.status_code == 200 and "?v=" in page.text and __version__ not in page.text


# --------------------------------------------------------------------------- R-20
def test_r20_named_tunnel_token_is_not_on_the_command_line(tmp_path):
    settings = TunnelSettings(enabled=True, mode="named", cloudflared="cf", token="tok-SECRET")
    mgr = TunnelManager(settings, "http://127.0.0.1:8780", None, tmp_path)
    assert "tok-SECRET" not in " ".join(mgr.command("cf"))
    assert mgr.environment()["TUNNEL_TOKEN"] == "tok-SECRET"
    quick = TunnelManager(TunnelSettings(enabled=True), "http://127.0.0.1:8780", None, tmp_path)
    assert quick.environment() is None


# --------------------------------------------------------------------------- R-21
def test_r21_deeply_nested_content_does_not_recurse():
    deep: list = ["abcd"]
    for _ in range(100_000):
        deep = [deep]
    assert text_chars(deep) == 4
    body = {"messages": [{"role": "user", "content": deep}]}
    assert estimate_prompt_tokens("chat.completions", body) == (16 + 4 + 3) // 4


async def test_r21_deep_content_is_not_a_500(sec):
    deep: list = [{"type": "text", "text": "hi"}]
    for _ in range(900):  # passes json.loads, used to RecursionError in the estimate
        deep = [deep]
    r = await sec.local.post("/v1/chat/completions",
                             json={"model": "x", "messages": [{"role": "user", "content": deep}]})
    assert r.status_code != 500, r.text
