"""Tests for the v0.2 config sections (T-1): roles, model, usage, tiers, queue,
listeners, tunnels, accounts.*.tier / max_api_keys, local_account, and configedit."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llmop import configedit
from llmop.config import ConfigError, TunnelSettings, default_tiers, load_config, parse_config

ROOT = Path(__file__).resolve().parents[1]

# A's config.example.yaml as it was before v0.2 (must keep loading unchanged).
OLD_EXAMPLE = """# LocalLLMOutsiderProvider (llmop) - example config
# Copy to config.yaml and edit:   copy config.example.yaml config.yaml   (Windows)
# The file is re-read automatically when it changes; no restart needed for
# accounts / quotas / backends.
# config.yaml is in .gitignore - never commit real password hashes.

server:
  host: 127.0.0.1          # keep 127.0.0.1; the tunnel connects locally. Docker sets 0.0.0.0.
  port: 8780
  data_dir: ./data         # SQLite file (keys, sessions, usage records) lives here
  public_url: ""           # fixed public URL (named tunnel / own domain). Empty = use quick tunnel URL
  session_minutes: 720     # web login lasts 12 hours
  cookie_secure: auto      # auto | always | never
  title: "Local LLM Outsider Provider"

# Local LLM servers behind the gateway. type: ollama | vllm | lmstudio | llamacpp | openai
# All are reached through their OpenAI-compatible /v1 API; Ollama also gets
# native extras (model listing from /api/tags, context length from /api/show).
backends:
  - name: ollama
    type: ollama
    base_url: http://127.0.0.1:11434   # in Docker compose: http://ollama:11434
    models: ["*"]                      # expose all models, or list e.g. ["qwen3.5:9b"]
    max_concurrency: 2                 # requests sent to this server at once (protect the GPU)
    timeout: 600                       # seconds without progress before giving up
    # Ollama's default context is small (4096 on many builds) and it SILENTLY drops
    # the start of long prompts. Ollama's /v1 API ignores per-request num_ctx, so set
    # it on the Ollama side: env OLLAMA_CONTEXT_LENGTH=8192 (or Modelfile num_ctx).
    context_size: 0                    # sent as options.num_ctx (best effort; see above)
  # - name: vllm
  #   type: vllm
  #   base_url: http://127.0.0.1:8000
  #   api_key: env:VLLM_API_KEY        # if vLLM was started with --api-key
  #   models: ["*"]

# Optional friendly names:  "model": "fast"  ->  qwen3.5:9b
model_aliases:
  # fast: qwen3.5:9b

# Applied to every account unless the account overrides a value. 0 = unlimited.
default_quota:
  tokens_per_day: 200000
  tokens_per_month: 3000000
  requests_per_minute: 20
  requests_per_day: 0
  max_concurrent: 2
  max_tokens_per_request: 4096

# Accounts. max_api_keys: 0 = this account cannot create API keys (web playground only).
# Create a password hash with:   llmop hash-password
# (or: python -m llmop hash-password)
accounts:
  admin:
    role: admin
    display_name: Owner
    password_hash: "REPLACE_ME_run_llmop_hash-password"
    quota:
      tokens_per_day: 0        # unlimited for the owner
      tokens_per_month: 0
  # friend1:
  #   role: user
  #   display_name: Friend
  #   password_hash: "scrypt$32768$8$1$...."
  #   models: ["qwen3.5:9b"]   # which models this account may use ("*" = all)
  #   max_api_keys: 3
  #   quota:
  #     tokens_per_day: 50000

# Cloudflare tunnel (needs cloudflared: winget install --id Cloudflare.cloudflared)
tunnel:
  enabled: false
  mode: quick              # quick = random https://xxx.trycloudflare.com (changes every start)
                           # named = your own domain, stable URL (needs token)
  cloudflared: cloudflared # path to cloudflared(.exe) if not on PATH
  token: ""                # mode: named -> env:CLOUDFLARE_TUNNEL_TOKEN
"""

HASH = "scrypt$x"


def acc(**extra):
    return {"password_hash": HASH, **extra}


def cfg_with(**raw):
    return parse_config(raw)


# ------------------------------------------------------------------ old files still load
def test_old_example_still_loads_with_defaults():
    cfg = parse_config(yaml.safe_load(OLD_EXAMPLE))
    assert cfg.accounts["admin"].is_admin
    assert cfg.accounts["admin"].quota.tokens_per_day == 0
    assert cfg.tunnel.mode == "quick" and cfg.tunnel.enabled is False
    assert cfg.roles == ["gateway"]
    assert cfg.model == ""
    assert cfg.usage.window_seconds == 5400
    assert cfg.queue.slots == 6
    assert (cfg.listeners.local, cfg.listeners.public_ui, cfg.listeners.public_api) == (8781, 8780, 8782)
    assert cfg.tunnels.ui == TunnelSettings() and cfg.tunnels.api == TunnelSettings()
    assert cfg.tiers == default_tiers()
    assert cfg.accounts["admin"].tier == "normal"
    assert cfg.accounts["admin"].max_api_keys == 1
    assert cfg.local_account == "admin"


def test_empty_config_has_every_default():
    cfg = parse_config({})
    assert cfg.roles == ["gateway"] and cfg.model == "" and cfg.local_account == ""
    u = cfg.usage
    assert (u.window_seconds, u.recover_at, u.debt_floor, u.low_mode) == (5400, 0.25, 0.25, "wait")
    assert (u.week_reset.weekday, u.week_reset.time, u.week_reset.timezone) == ("sun", "04:00", "local")
    q = cfg.queue
    assert (q.slots, q.max_length, q.max_per_account, q.fair_share_every) == (6, 30, 2, 6)
    assert q.max_wait_seconds == {"public": 60, "local": 600}
    t = cfg.tiers
    assert [t[n].priority for n in ("vip3", "vip2", "vip1", "normal", "guest")] == [1, 2, 3, 4, 5]
    assert [t[n].max_concurrent for n in ("vip3", "vip2", "vip1", "normal", "guest")] == [3, 2, 2, 1, 1]
    assert (t["vip3"].bucket, t["vip3"].weekly) == (0, 0)  # not limited
    assert (t["vip1"].bucket, t["vip1"].weekly, t["vip1"].on_bucket_empty) == (240000, 3000000, "as_normal")
    assert (t["normal"].bucket, t["normal"].weekly, t["normal"].on_bucket_empty) == (80000, 1000000, "low")
    assert (t["guest"].bucket, t["guest"].budget, t["guest"].reset) == (20000, 50000, False)


def test_new_example_loads_and_matches_the_defaults():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.accounts["admin"].tier == "vip3"
    assert cfg.accounts["admin"].limits == default_tiers()["vip3"]
    assert cfg.tiers == default_tiers()
    assert cfg.queue == parse_config({}).queue
    assert cfg.usage == parse_config({}).usage
    assert cfg.listeners == parse_config({}).listeners
    assert cfg.local_account == "admin"


# ------------------------------------------------------------------ roles / model
def test_roles_load():
    assert cfg_with(roles=["gateway", "worker"]).roles == ["gateway", "worker"]
    assert cfg_with(roles="worker").roles == ["worker"]


@pytest.mark.parametrize("bad", [[], ["gateway", "gateway"], ["router"], {"a": 1}])
def test_roles_errors(bad):
    with pytest.raises(ConfigError, match="roles"):
        cfg_with(roles=bad)


def test_model_loads_and_errors():
    assert cfg_with(model=" qwen3.5:9b ").model == "qwen3.5:9b"
    with pytest.raises(ConfigError, match="model"):
        cfg_with(model=["a", "b"])


# ------------------------------------------------------------------ usage
def test_usage_loads():
    cfg = cfg_with(usage={"window_seconds": 3600, "recover_at": 0.5, "debt_floor": 0,
                          "low_mode": "block",
                          "week_reset": {"weekday": "mon", "time": "4:30", "timezone": "UTC"}})
    u = cfg.usage
    assert (u.window_seconds, u.recover_at, u.debt_floor, u.low_mode) == (3600, 0.5, 0.0, "block")
    assert (u.week_reset.weekday, u.week_reset.time, u.week_reset.timezone) == ("mon", "04:30", "UTC")
    # a partial week_reset keeps the other defaults
    assert cfg_with(usage={"week_reset": {"weekday": "sat"}}).usage.week_reset.time == "04:00"


@pytest.mark.parametrize("bad", [
    {"window_seconds": 0},
    {"window_seconds": "soon"},
    {"recover_at": 1.5},
    {"debt_floor": -0.1},
    {"low_mode": "drop"},
    {"week_reset": {"weekday": "sunday"}},
    {"week_reset": {"time": "25:00"}},
    {"week_reset": {"time": "4pm"}},
    {"week_reset": {"timezone": "Mars/Base"}},
    {"week_reset": {"day": "sun"}},
    {"bucket": 1},
])
def test_usage_errors(bad):
    with pytest.raises(ConfigError, match="usage"):
        cfg_with(usage=bad)


# ------------------------------------------------------------------ tiers
def test_tiers_partial_override_keeps_defaults():
    cfg = cfg_with(tiers={"normal": {"bucket": 100000}, "guest": {"reset": True}})
    assert cfg.tiers["normal"].bucket == 100000
    assert cfg.tiers["normal"].weekly == 1000000 and cfg.tiers["normal"].priority == 4
    assert cfg.tiers["guest"].reset is True
    assert cfg.tiers["vip1"] == default_tiers()["vip1"]


@pytest.mark.parametrize("bad", [
    {"low": {"priority": 6}},
    {"normal": {"bucket": -1}},
    {"normal": {"priority": 0}},
    {"normal": {"max_concurrent": "two"}},
    {"guest": {"reset": "yes"}},
    {"vip1": {"on_bucket_empty": "block"}},
    {"normal": {"speed": 1}},
    {"normal": [1, 2]},
])
def test_tiers_errors(bad):
    with pytest.raises(ConfigError, match="tiers"):
        cfg_with(tiers=bad)


# ------------------------------------------------------------------ accounts
def test_account_tier_and_overrides():
    cfg = cfg_with(
        tiers={"vip1": {"weekly": 4000000}},
        accounts={"alice": acc(), "bob": acc(tier="vip1", bucket=300000),
                  "carol": acc(tier="VIP2", max_api_keys=3)},
    )
    alice, bob, carol = cfg.accounts["alice"], cfg.accounts["bob"], cfg.accounts["carol"]
    assert alice.tier == "normal" and alice.limits == cfg.tiers["normal"] and alice.max_api_keys == 1
    assert bob.tier == "vip1" and bob.limits.bucket == 300000
    assert bob.limits.weekly == 4000000  # from the tier, not overridden
    assert cfg.tiers["vip1"].bucket == 240000  # the override is per account only
    assert carol.tier == "vip2" and carol.max_api_keys == 3


@pytest.mark.parametrize("entry, match", [
    (acc(tier="low"), "tier"),
    (acc(tier="guest"), "tier"),
    (acc(bucket=-5), "bucket"),
    (acc(max_api_keys=-1), "max_api_keys"),
    (acc(max_api_keys="many"), "max_api_keys"),
    (acc(colour="red"), "unknown keys"),
])
def test_account_errors(entry, match):
    with pytest.raises(ConfigError, match=match):
        cfg_with(accounts={"alice": entry})


# ------------------------------------------------------------------ queue
def test_queue_loads():
    q = cfg_with(queue={"slots": 2, "max_length": 10, "max_per_account": 1,
                        "max_wait_seconds": {"public": 30}, "fair_share_every": 4}).queue
    assert (q.slots, q.max_length, q.max_per_account, q.fair_share_every) == (2, 10, 1, 4)
    assert q.max_wait_seconds == {"public": 30, "local": 600}


@pytest.mark.parametrize("bad", [
    {"slots": 0},
    {"max_length": "x"},
    {"max_per_account": 1.5},
    {"max_wait_seconds": {"lan": 10}},
    {"max_wait_seconds": {"public": 0}},
    {"max_wait_seconds": 60},
    {"priority": 1},
])
def test_queue_errors(bad):
    with pytest.raises(ConfigError, match="queue"):
        cfg_with(queue=bad)


# ------------------------------------------------------------------ listeners
def test_listeners_load():
    lst = cfg_with(listeners={"local": 9001, "public-ui": 9002, "public_api": 9003}).listeners
    assert (lst.local, lst.public_ui, lst.public_api) == (9001, 9002, 9003)
    assert cfg_with(listeners={"local": 9001}).listeners.public_api == 8782


@pytest.mark.parametrize("bad", [
    {"local": 0},
    {"local": 70000},
    {"local": "http"},
    {"local": 8780},  # same port as public_ui
    {"lan": 8783},
    [8781],
])
def test_listeners_errors(bad):
    with pytest.raises(ConfigError, match="listeners"):
        cfg_with(listeners=bad)


# ------------------------------------------------------------------ tunnels
def test_tunnels_load(monkeypatch):
    monkeypatch.setenv("LLMOP_TEST_TUNNEL_TOKEN", "tok")
    cfg = cfg_with(tunnels={"ui": {"enabled": True, "mode": "named", "token": "env:LLMOP_TEST_TUNNEL_TOKEN"},
                            "api": {"enabled": True}})
    assert cfg.tunnels.ui.mode == "named" and cfg.tunnels.ui.token == "tok"
    assert cfg.tunnels.api.enabled is True and cfg.tunnels.api.mode == "quick"
    assert cfg.tunnel == TunnelSettings()  # the v0.1 single tunnel is separate
    assert cfg_with(tunnels={"api": {"mode": "off"}}).tunnels.ui == TunnelSettings()


@pytest.mark.parametrize("bad", [
    {"ui": {"mode": "fast"}},
    {"api": {"port": 1}},
    {"lan": {}},
    {"ui": "quick"},
])
def test_tunnels_errors(bad):
    with pytest.raises(ConfigError, match="tunnels"):
        cfg_with(tunnels=bad)


# ------------------------------------------------------------------ local_account
def test_local_account_explicit_and_default():
    accounts = {"alice": acc(), "root": acc(role="admin"), "boss": acc(role="admin")}
    assert cfg_with(accounts=accounts).local_account == "root"  # first admin
    assert cfg_with(accounts=accounts, local_account="alice").local_account == "alice"
    assert cfg_with(accounts={"alice": acc()}).local_account == ""  # no admin: not set


def test_local_account_must_exist():
    with pytest.raises(ConfigError, match="local_account"):
        cfg_with(accounts={"alice": acc()}, local_account="nobody")


# ------------------------------------------------------------------ configedit (copied from cli.py)
def test_configedit_keeps_comments_and_validates(tmp_path):
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    entry = {"role": "user", "tier": "vip1", "password_hash": HASH}
    new, kept = configedit.edit_config_text(
        text,
        lambda t: configedit._text_add_account(t, "dave", entry),
        lambda raw: configedit._raw_add_account(raw, "dave", entry),
    )
    assert kept and "# Tiers." in new
    path = tmp_path / "config.yaml"
    cfg = configedit._validate_text(new, path)
    assert cfg.accounts["dave"].tier == "vip1"
    configedit._save_edit(path, new, kept)
    assert load_config(path).accounts["dave"].limits.bucket == 240000


def test_configedit_errors_are_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="invalid config"):
        configedit._validate_text("queue: {slots: 0}\n", tmp_path / "config.yaml")
    with pytest.raises(ConfigError, match="top level"):
        configedit.edit_config_text("- a\n", lambda t: None, lambda raw: raw)


def test_configedit_set_llm_backend_and_model_keep_comments(tmp_path):
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    new, kept = configedit.set_llm_backend(text, "lmstudio", "http://localhost:1235", "qwen3.5-9b@q6_k")
    assert kept and "# Tiers." in new and "model: qwen3.5-9b@q6_k" in new
    cfg = configedit._validate_text(new, tmp_path / "config.yaml")
    b = cfg.backends[0]
    assert (b.type, b.name, b.base_url, cfg.model) == ("lmstudio", "lmstudio", "http://localhost:1235",
                                                       "qwen3.5-9b@q6_k")
    # model=None leaves the model alone; a model with a slash is quoted
    again, kept = configedit.set_llm_backend(new, "vllm", "http://127.0.0.1:8000")
    assert kept and "model: qwen3.5-9b@q6_k" in again
    again, kept = configedit.set_llm_backend(again, "vllm", "http://127.0.0.1:8000", "qwen/qwen3.8-27b")
    assert configedit._validate_text(again, tmp_path / "c.yaml").model == "qwen/qwen3.8-27b"


def test_configedit_set_model_appends_when_missing():
    new, kept = configedit.edit_config_text("backends: []\n", lambda t: configedit._text_set_model(t, "m1"),
                                            lambda r: configedit._raw_set_model(r, "m1"))
    assert kept and new == "backends: []\nmodel: m1\n"
