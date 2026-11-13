"""Tests for the core modules: config, security, store, quota, auth helpers."""

from __future__ import annotations

import time

import pytest
import yaml

from llmop import security
from llmop.config import ConfigError, ConfigHolder, load_config, model_allowed, parse_config
from llmop.quota import QuotaDenied, QuotaManager, day_start
from llmop.store import Store, UsageEvent

FAST_HASH = security.hash_password("pw", n=2**10)


def make_cfg(**quota):
    return parse_config({
        "backends": [{"name": "ollama", "type": "ollama"}],
        "default_quota": {"tokens_per_day": 100, **quota},
        "accounts": {
            "alice": {"password_hash": FAST_HASH},
            "root": {"password_hash": FAST_HASH, "role": "admin", "quota": {"tokens_per_day": 0}},
        },
    })


# ------------------------------------------------------------------ config
def test_example_config_parses_except_placeholder():
    cfg = load_config("config.example.yaml")
    assert cfg.backends[0].type == "ollama"
    assert cfg.accounts["admin"].is_admin
    assert cfg.accounts["admin"].quota.tokens_per_day == 0
    assert cfg.accounts["admin"].quota.requests_per_minute == 20  # from default_quota


def test_quota_inherits_and_overrides():
    cfg = make_cfg(requests_per_minute=5)
    assert cfg.accounts["alice"].quota.tokens_per_day == 100
    assert cfg.accounts["alice"].quota.requests_per_minute == 5
    assert cfg.accounts["root"].quota.tokens_per_day == 0


@pytest.mark.parametrize("raw, msg", [
    ({"backends": [{"type": "nope"}]}, "type"),
    ({"accounts": {"a": {}}}, "password_hash"),
    ({"accounts": {"a": {"password_hash": "x", "role": "god"}}}, "role"),
    ({"accounts": {"a b": {"password_hash": "x"}}}, "username"),
    ({"default_quota": {"bogus": 1}}, "quota"),
    ({"server": {"bogus": 1}}, "server"),
    ({"backends": [{"type": "ollama"}, {"type": "ollama"}]}, "unique"),
])
def test_config_errors(raw, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)


def test_backend_root_url_strips_v1():
    cfg = parse_config({"backends": [{"type": "vllm", "base_url": "http://h:8000/v1/"}]})
    assert cfg.backends[0].root_url == "http://h:8000"


def test_env_secret(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc")
    cfg = parse_config({"backends": [{"type": "vllm", "api_key": "env:MY_KEY"}]})
    assert cfg.backends[0].api_key == "abc"


def test_model_allowed():
    assert model_allowed("qwen3:8b", ["*"])
    assert model_allowed("qwen3:8b", ["qwen*"])
    assert model_allowed("llama3", ["llama3:latest"])
    assert model_allowed("llama3:latest", ["llama3"])
    assert not model_allowed("llama3", ["qwen*"])


def test_config_holder_reload_and_keeps_last_good(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"accounts": {"a": {"password_hash": "x"}}}), encoding="utf-8")
    holder = ConfigHolder(load_config(p))
    assert "a" in holder.get().accounts
    time.sleep(0.01)
    p.write_text(yaml.safe_dump({"accounts": {"b": {"password_hash": "x"}}}), encoding="utf-8")
    import os
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert "b" in holder.get().accounts
    p.write_text("accounts: {c: {}}", encoding="utf-8")  # invalid: no password_hash
    os.utime(p, (time.time() + 10, time.time() + 10))
    assert "b" in holder.get().accounts
    assert "password_hash" in holder.last_error


def test_data_path_relative_to_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("server: {data_dir: ./d}\n", encoding="utf-8")
    assert load_config(p).data_path == tmp_path / "d"


# ------------------------------------------------------------------ security
def test_password_hash_roundtrip():
    h = security.hash_password("秘密", n=2**10)
    assert h.startswith("scrypt$1024$")
    assert security.verify_password("秘密", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("x", "garbage")


def test_api_key_format():
    k = security.new_api_key()
    assert k.startswith("sk-llop-") and security.looks_like_api_key(k)
    assert security.key_display(k).endswith(k[-4:])


# ------------------------------------------------------------------ store
def test_api_key_lifecycle():
    s = Store(":memory:")
    key, rec = s.create_api_key("alice", "laptop")
    assert s.find_api_key(key).id == rec.id
    assert s.find_api_key(key + "x") is None
    assert s.count_active_keys("alice") == 1
    assert not s.revoke_api_key(rec.id, username="bob")
    assert s.revoke_api_key(rec.id, username="alice")
    assert s.find_api_key(key) is None
    assert s.list_api_keys("alice") == []
    assert len(s.list_api_keys("alice", include_revoked=True)) == 1


def test_expired_key_rejected():
    s = Store(":memory:")
    key, _ = s.create_api_key("alice", expires_at=time.time() - 1)
    assert s.find_api_key(key) is None


def test_sessions():
    s = Store(":memory:")
    sid, sess = s.create_session("alice", 60)
    assert s.get_session(sid).username == "alice"
    assert s.get_session("nope") is None
    s.delete_session(sid)
    assert s.get_session(sid) is None
    sid2, _ = s.create_session("alice", -1)
    assert s.get_session(sid2) is None


def test_usage_aggregation():
    s = Store(":memory:")
    now = time.time()
    s.record_usage(UsageEvent("alice", "chat.completions", "m1", "ollama", 10, 5, ts=now))
    s.record_usage(UsageEvent("alice", "chat.completions", "m2", "ollama", 1, 1, ts=now))
    s.record_usage(UsageEvent("bob", "chat.completions", "m1", "ollama", 100, 0, ts=now))
    assert s.tokens_since("alice", now - 10) == 17
    assert s.requests_since("alice", now - 10) == 2
    assert s.usage_summary(None, now - 10)["total"] == 117
    assert s.usage_by_model("alice", now - 10)[0]["model"] == "m1"
    assert len(s.usage_by_day("alice", now - 86400)) == 1
    assert s.usage_by_user(now - 10)[0]["username"] == "bob"
    assert len(s.recent_usage("alice")) == 2


def test_kv():
    s = Store(":memory:")
    assert s.kv_get("x") is None
    s.kv_set("x", "1")
    s.kv_set("x", "2")
    assert s.kv_get("x")[0] == "2"


# ------------------------------------------------------------------ quota
def test_quota_daily_tokens_and_clamp():
    cfg = make_cfg(max_tokens_per_request=50)
    s = Store(":memory:")
    qm = QuotaManager(s)
    alice = cfg.accounts["alice"]
    lease = qm.acquire(alice)
    assert lease.max_tokens == 50
    lease.release()
    s.record_usage(UsageEvent("alice", "chat.completions", "m", "b", 80, 0))
    lease = qm.acquire(alice)
    assert lease.max_tokens == 20  # 100 - 80 left today
    lease.release()
    s.record_usage(UsageEvent("alice", "chat.completions", "m", "b", 30, 0))
    with pytest.raises(QuotaDenied) as e:
        qm.acquire(alice)
    assert e.value.code == "insufficient_quota" and e.value.retry_after > 0


def test_quota_unlimited_admin():
    cfg = make_cfg()
    qm = QuotaManager(Store(":memory:"))
    lease = qm.acquire(cfg.accounts["root"])
    assert lease.max_tokens is None
    lease.release()


def test_quota_rpm_and_concurrency():
    cfg = make_cfg(requests_per_minute=2, max_concurrent=1)
    t = [1000.0]
    qm = QuotaManager(Store(":memory:"), clock=lambda: t[0])
    a = cfg.accounts["alice"]
    l1 = qm.acquire(a)
    with pytest.raises(QuotaDenied, match="same time"):
        qm.acquire(a)
    l1.release()
    l1.release()  # idempotent
    qm.acquire(a).release()
    with pytest.raises(QuotaDenied) as e:
        qm.acquire(a)
    assert e.value.code == "rate_limit_exceeded"
    t[0] += 61
    qm.acquire(a).release()


def test_quota_disabled_account():
    cfg = parse_config({"accounts": {"x": {"password_hash": "h", "disabled": True}}})
    with pytest.raises(QuotaDenied) as e:
        QuotaManager(Store(":memory:")).acquire(cfg.accounts["x"])
    assert e.value.code == "account_disabled"


def test_quota_usage_and_headers():
    cfg = make_cfg(requests_per_minute=10)
    s = Store(":memory:")
    s.record_usage(UsageEvent("alice", "chat.completions", "m", "b", 30, 10))
    qm = QuotaManager(s)
    info = qm.usage(cfg.accounts["alice"])
    assert info["tokens_today"] == 40 and info["remaining"]["tokens_today"] == 60
    h = qm.headers(cfg.accounts["alice"])
    assert h["x-ratelimit-remaining-tokens"] == "60"
    assert h["x-ratelimit-limit-requests"] == "10"


def test_day_start_is_midnight():
    ds = day_start()
    assert ds <= time.time() < ds + 86400 + 3600
