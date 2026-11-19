"""DISPATCH T-9: llmop.apilink (the secret path in front of the public API listener)."""

from __future__ import annotations

import os
import re
import sqlite3

import pytest

from llmop.apilink import (
    AUDIT_FILE,
    GATE_FILE,
    RESTART_KV,
    ApiLink,
    api_base_url,
    host_change_pending,
    new_gate,
    request_host_change,
)
from llmop.config import parse_config
from llmop.netguard import gate_hash
from llmop.store import Store


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def store(data):
    data.mkdir()
    s = Store(data / "llmop.sqlite3")
    yield s
    s.close()


def link(store, data, **kw):
    kw.setdefault("base_url", lambda: "https://api.example.test")
    return ApiLink(store, data, **kw)


def db_text(data) -> str:
    con = sqlite3.connect(data / "llmop.sqlite3")
    try:
        return " ".join(str(v) for row in con.execute("SELECT * FROM api_link") for v in row)
    finally:
        con.close()


def test_new_gate_is_256_bits_url_safe():
    gates = {new_gate() for _ in range(50)}
    assert len(gates) == 50
    for g in gates:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", g)


def test_first_start_creates_a_gate_and_stores_only_its_hash(store, data):
    al = link(store, data)
    gate = al.gate
    assert al.current_url() == f"https://api.example.test/{gate}" == al.url
    row = store.get_api_link()
    assert row["current_gate_hash"] == gate_hash(gate)
    assert gate not in db_text(data)
    assert (data / GATE_FILE).read_text().strip() == gate
    if os.name != "nt":
        assert (data / GATE_FILE).stat().st_mode & 0o777 == 0o600
    state = al.gate_state()
    assert state.allows(gate, 0) and not state.allows("other", 0)


def test_restart_keeps_the_link(store, data):
    gate = link(store, data).gate
    assert link(store, data).gate == gate


def test_lost_gate_file_or_foreign_db_gives_a_new_gate(store, data):
    gate = link(store, data).gate
    (data / GATE_FILE).unlink()
    again = link(store, data)
    assert again.gate and again.gate != gate
    assert not again.gate_state().allows(gate, 0)


def test_rotate_path_invalidates_the_old_gate(store, data):
    al = link(store, data)
    old = al.gate
    result = al.rotate_path("test")
    assert al.gate != old and result["url"] == al.current_url()
    state = al.gate_state()
    assert not state.allows(old, 0) and state.allows(al.gate, 0)


def test_grace_period(store, data):
    now = [1000.0]
    al = link(store, data, clock=lambda: now[0], grace_seconds=600)
    old = al.gate
    result = al.rotate_path("with grace")
    assert result["grace_until"] == 1600.0
    state = al.gate_state()
    assert state.allows(old, 1599.0) and not state.allows(old, 1600.0)
    assert state.allows(al.gate, 5000.0)
    al.rotate_path("no grace", grace_seconds=0)
    assert not al.gate_state().allows(old, 1100.0)


async def test_rotate_with_host_change_calls_the_tunnel_first(store, data):
    calls = []

    async def restart():
        calls.append(link_obj.gate)  # the gate is changed only after the new tunnel
        return True

    link_obj = link(store, data, restart_tunnel=restart)
    old = link_obj.gate
    result = await link_obj.rotate(change_host=True, reason="r")
    assert calls == [old] and result["change_host"] is True and link_obj.gate != old

    result = await link(store, data).rotate(change_host=True)  # no tunnel: path only
    assert result["change_host"] is False


async def test_failed_tunnel_restart_keeps_the_gate(store, data):
    def broken():
        raise RuntimeError("no url")

    al = link(store, data, restart_tunnel=broken)
    old = al.gate
    with pytest.raises(RuntimeError):
        await al.rotate(change_host=True)
    assert al.gate == old


def test_audit_log_has_hashes_never_gates(store, data):
    al = link(store, data)
    gates = [al.gate]
    al.rotate_path("first")
    gates.append(al.gate)
    al.rotate_path("second", change_host=True)
    entries = al.audit_entries()
    assert [e["reason"] for e in entries] == ["first", "second"]
    assert entries[0]["old_gate_sha256"] == gate_hash(gates[0])
    assert entries[1]["change_host"] is True
    text = (data / AUDIT_FILE).read_text()
    for g in gates + [al.gate]:
        assert g not in text


def test_another_process_rotation_is_picked_up(store, data):
    node = link(store, data, sync_interval=3600)
    old = node.gate
    other_store = Store(data / "llmop.sqlite3")
    try:
        link(other_store, data).rotate_path("cli")
    finally:
        other_store.close()
    assert node.gate == old  # cached until the next sync
    node.sync(force=True)
    assert node.gate != old and not node.gate_state().allows(old, 0)


async def test_cli_host_change_request_is_handled_by_the_node(store, data):
    restarted = []
    al = link(store, data, restart_tunnel=lambda: restarted.append(1))
    assert await al.poll_requests() is False
    request_host_change(store)
    assert host_change_pending(store)
    assert await al.poll_requests() is True
    assert restarted == [1] and not host_change_pending(store)
    assert store.kv_get(RESTART_KV)[0] == ""


def test_memory_only_gate_without_data_dir():
    store = Store(":memory:")
    try:
        al = ApiLink(store, None)
        assert al.gate and al.current_url() == f"http://127.0.0.1:8782/{al.gate}"
    finally:
        store.close()


def test_api_base_url_precedence(store):
    cfg = parse_config({"listeners": {"public_api": 9999}})
    assert api_base_url(cfg) == "http://127.0.0.1:9999"
    store.kv_set("api_tunnel_url", "https://published.trycloudflare.com")
    assert api_base_url(cfg, None, store) == "https://published.trycloudflare.com"

    class T:
        url = "https://live.trycloudflare.com/"

    assert api_base_url(cfg, T(), store) == "https://live.trycloudflare.com"
    named = parse_config({"tunnels": {"api": {"mode": "named", "public_url": "https://api.me.test/"}}})
    assert api_base_url(named, None, store) == "https://api.me.test"
