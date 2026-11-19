"""llmop command line: init / hash-password / add-user / set-password / serve pre-flight /
check / usage / keys. ``serve`` is tested with mocks (no real uvicorn, no real tunnel)."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from llmop import __version__, cli
from llmop.config import load_config, parse_config
from llmop.security import verify_password
from llmop.store import Store, UsageEvent

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PW = "admin-pass-123"


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("LLMOP_CONFIG", "LLMOP_DATA_DIR", "LLMOP_ADMIN_PASSWORD", "LLMOP_IN_CONTAINER"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_prompt(monkeypatch):
    """Fail loudly if a test would wait for a hidden password prompt."""

    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", boom)


@pytest.fixture
def config_file(tmp_path, capsys, no_prompt) -> Path:
    path = tmp_path / "config.yaml"
    code, _, err = run(capsys, "--config", path, "init", "--admin-password", ADMIN_PW)
    assert code == 0, err
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ the packaged example
def test_example_config_is_packaged_and_identical_to_the_root_copy():
    root = (ROOT / "config.example.yaml").read_bytes().replace(b"\r\n", b"\n")
    packaged = (ROOT / "src" / "llmop" / "examples" / "config.example.yaml").read_bytes().replace(b"\r\n", b"\n")
    assert root == packaged, "copy config.example.yaml to src/llmop/examples/ after editing it"


def test_example_config_is_valid_and_has_the_placeholder():
    cfg = parse_config(yaml.safe_load(cli._example_text()))
    assert cli.placeholder_accounts(cfg) == ["admin"]


# ------------------------------------------------------------------ init
def test_init_creates_a_working_config(config_file):
    cfg = load_config(config_file)
    assert verify_password(ADMIN_PW, cfg.accounts["admin"].password_hash)
    assert cli.placeholder_accounts(cfg) == []
    text = config_file.read_text(encoding="utf-8")
    assert "# Cloudflare tunnel" in text  # comments survive
    assert "REPLACE_ME" not in text


def test_init_refuses_to_overwrite_without_force(config_file, capsys):
    before = config_file.read_text(encoding="utf-8")
    code, _, err = run(capsys, "--config", config_file, "init", "--admin-password", "other-pass-1")
    assert code == 1
    assert "already exists" in err and "--force" in err
    assert config_file.read_text(encoding="utf-8") == before

    code, _, _ = run(capsys, "--config", config_file, "init", "--force", "--admin-password", "other-pass-1")
    assert code == 0
    assert verify_password("other-pass-1", load_config(config_file).accounts["admin"].password_hash)
    assert config_file.with_name("config.yaml.bak").read_text(encoding="utf-8") == before


def test_init_takes_the_password_from_the_environment(tmp_path, capsys, monkeypatch, no_prompt):
    monkeypatch.setenv("LLMOP_ADMIN_PASSWORD", "from-env-pass")
    path = tmp_path / "c.yaml"
    assert run(capsys, "init", "--config", path)[0] == 0
    assert verify_password("from-env-pass", load_config(path).accounts["admin"].password_hash)


def test_init_asks_twice_and_rejects_short_passwords(tmp_path, capsys, monkeypatch):
    answers = iter(["short", "long-enough-1", "different-12", "long-enough-1", "long-enough-1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    path = tmp_path / "config.yaml"
    code, _, err = run(capsys, "--config", path, "init")
    assert code == 0
    assert "too short" in err and "different" in err
    assert verify_password("long-enough-1", load_config(path).accounts["admin"].password_hash)


def test_init_without_a_terminal_fails_cleanly(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    code, _, err = run(capsys, "--config", path, "init")
    assert code == 2
    assert "--password" in err
    assert not path.exists()


def test_init_rejects_a_short_password_option(tmp_path, capsys):
    code, _, err = run(capsys, "--config", tmp_path / "c.yaml", "init", "--admin-password", "123")
    assert code == 2 and "at least 8" in err


def test_init_backend_options(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    code, _, err = run(capsys, "--config", path, "init", "--admin-password", ADMIN_PW,
                       "--backend-type", "vllm", "--backend-url", "http://192.168.1.20:8000/v1")
    assert code == 0, err
    b = load_config(path).backends[0]
    assert (b.name, b.type, b.base_url) == ("vllm", "vllm", "http://192.168.1.20:8000/v1")
    assert "# Optional friendly names" in path.read_text(encoding="utf-8")
    assert "comments were removed" not in err

    path2 = tmp_path / "c2.yaml"
    assert run(capsys, "--config", path2, "init", "--admin-password", ADMIN_PW,
               "--backend-type", "lmstudio")[0] == 0
    assert load_config(path2).backends[0].base_url == "http://127.0.0.1:1234"


# ------------------------------------------------------------------ hash-password
def test_hash_password_prints_a_verifiable_hash(capsys, no_prompt):
    code, out, _ = run(capsys, "hash-password", "--password", "my-secret-pw")
    assert code == 0
    line = out.strip().splitlines()[-1]
    assert line.startswith("scrypt$")
    assert verify_password("my-secret-pw", line)


def test_hash_password_prompts(capsys, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "typed-pass-1")
    code, out, _ = run(capsys, "hash-password")
    assert code == 0 and verify_password("typed-pass-1", out.strip())


# ------------------------------------------------------------------ add-user / set-password
def test_add_user_keeps_comments_and_other_accounts(config_file, capsys, no_prompt):
    before = load_config(config_file)
    code, out, err = run(capsys, "--config", config_file, "add-user", "bob", "--password", "bob-pass-123",
                         "--tokens-per-day", "1000", "--display-name", "Bob B", "--models", "qwen3.5:9b, llama3*")
    assert code == 0, err
    assert "comments were removed" not in err
    text = config_file.read_text(encoding="utf-8")
    for comment in ("# Cloudflare tunnel", "# friend1:", "# unlimited for the owner", "# Accounts."):
        assert comment in text
    cfg = load_config(config_file)
    bob = cfg.accounts["bob"]
    assert verify_password("bob-pass-123", bob.password_hash)
    assert bob.role == "user" and bob.display_name == "Bob B"
    assert bob.models == ["qwen3.5:9b", "llama3*"]
    assert bob.quota.tokens_per_day == 1000
    assert bob.quota.tokens_per_month == before.default_quota.tokens_per_month  # default kept
    assert cfg.accounts["admin"].password_hash == before.accounts["admin"].password_hash
    assert "no restart" in out


def test_add_user_generates_a_password(config_file, capsys, no_prompt):
    code, out, _ = run(capsys, "--config", config_file, "add-user", "carol", "--role", "admin", "--generate")
    assert code == 0
    pw = next(line.split("password:")[1].strip() for line in out.splitlines() if "password:" in line)
    acc = load_config(config_file).accounts["carol"]
    assert acc.role == "admin" and verify_password(pw, acc.password_hash)


def test_add_user_refuses_duplicates_and_bad_names(config_file, capsys, no_prompt):
    before = config_file.read_text(encoding="utf-8")
    code, _, err = run(capsys, "--config", config_file, "add-user", "admin", "--password", "xxxxxxxx1")
    assert code == 1 and "already exists" in err
    code, _, err = run(capsys, "--config", config_file, "add-user", "bad name", "--password", "xxxxxxxx1")
    assert code == 2
    code, _, err = run(capsys, "--config", config_file, "add-user", "a:b", "--password", "xxxxxxxx1")
    assert code == 2
    assert config_file.read_text(encoding="utf-8") == before


def test_add_user_to_an_empty_accounts_section(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    path.write_text("# my config\nserver:\n  port: 9000\naccounts: {}  # nobody yet\n\n# the end\n",
                    encoding="utf-8")
    assert run(capsys, "--config", path, "add-user", "yes", "--password", "pass-word-1")[0] == 0
    text = path.read_text(encoding="utf-8")
    assert "# my config" in text and "# the end" in text
    cfg = load_config(path)
    assert list(cfg.accounts) == ["yes"]  # "yes" stays a string, not a boolean
    assert cfg.server.port == 9000


def test_add_user_without_accounts_section(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    path.write_text("server:\n  port: 9000\n", encoding="utf-8")
    assert run(capsys, "--config", path, "add-user", "dave", "--password", "pass-word-1")[0] == 0
    assert list(load_config(path).accounts) == ["dave"]


def test_add_user_keeps_crlf_and_bom(config_file, capsys, no_prompt):
    raw = config_file.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    config_file.write_bytes(b"\xef\xbb\xbf" + raw)  # Notepad style
    assert run(capsys, "--config", config_file, "add-user", "erin", "--password", "pass-word-1")[0] == 0
    data = config_file.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert "erin" in load_config(config_file).accounts


def test_add_user_flow_style_falls_back_to_a_rewrite(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    path.write_text('accounts: {admin: {role: admin, password_hash: "scrypt$x"}}\n', encoding="utf-8")
    code, _, err = run(capsys, "--config", path, "add-user", "frank", "--password", "pass-word-1")
    assert code == 0
    assert "comments were removed" in err
    cfg = load_config(path)
    assert set(cfg.accounts) == {"admin", "frank"}
    assert cfg.accounts["admin"].password_hash == "scrypt$x"


def test_add_user_refuses_to_write_an_invalid_config(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    original = "server:\n  bogus_key: 1\naccounts:\n  admin:\n    password_hash: x\n"
    path.write_text(original, encoding="utf-8")
    code, _, err = run(capsys, "--config", path, "add-user", "gina", "--password", "pass-word-1")
    assert code == 2 and "nothing was changed" in err
    assert path.read_text(encoding="utf-8") == original


def test_set_password_changes_one_hash_and_logs_out_sessions(config_file, capsys, no_prompt):
    assert run(capsys, "--config", config_file, "add-user", "bob", "--password", "bob-pass-123")[0] == 0
    before = load_config(config_file)
    store = Store(before.data_path / "llmop.sqlite3")
    sid, _ = store.create_session("bob", 3600)
    store.close()
    code, out, _ = run(capsys, "--config", config_file, "set-password", "bob", "--password", "new-pass-456")
    assert code == 0
    after = load_config(config_file)
    assert verify_password("new-pass-456", after.accounts["bob"].password_hash)
    assert after.accounts["admin"].password_hash == before.accounts["admin"].password_hash
    assert "logged out 1" in out
    store = Store(before.data_path / "llmop.sqlite3")
    assert store.get_session(sid) is None
    store.close()
    code, _, err = run(capsys, "--config", config_file, "set-password", "nobody", "--password", "xxxxxxxx1")
    assert code == 1


def test_set_password_replaces_the_placeholder(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    path.write_text(cli._example_text(), encoding="utf-8")
    assert run(capsys, "--config", path, "set-password", "admin", "--password", "pass-word-1")[0] == 0
    assert cli.placeholder_accounts(load_config(path)) == []
    assert "# friend1:" in path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ parsing
def test_config_option_before_or_after_the_command(monkeypatch):
    p = cli.build_parser()
    assert p.parse_args(["serve"]).config == "config.yaml"
    assert p.parse_args(["--config", "a.yaml", "serve"]).config == "a.yaml"
    assert p.parse_args(["serve", "--config", "b.yaml"]).config == "b.yaml"
    assert p.parse_args(["-c", "a.yaml", "check", "-c", "b.yaml"]).config == "b.yaml"
    monkeypatch.setenv("LLMOP_CONFIG", "/etc/llmop.yaml")
    assert cli.build_parser().parse_args(["check"]).config == "/etc/llmop.yaml"


def test_serve_arguments():
    p = cli.build_parser()
    a = p.parse_args(["serve"])
    assert (a.tunnel, a.host, a.port, a.open, a.log_level) == (None, None, None, False, "info")
    assert p.parse_args(["serve", "--tunnel"]).tunnel is True
    assert p.parse_args(["serve", "--no-tunnel"]).tunnel is False
    a = p.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000", "--open"])
    assert (a.host, a.port, a.open) == ("0.0.0.0", 9000, True)


def test_help_version_and_usage_errors_return_codes(capsys):
    assert cli.main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out
    assert cli.main(["--help"]) == 0
    assert cli.main([]) == 1
    assert cli.main(["no-such-command"]) == 2
    assert cli.main(["serve", "--port", "abc"]) == 2


def test_missing_config_points_to_init(tmp_path, capsys):
    code, _, err = run(capsys, "--config", tmp_path / "nope.yaml", "check")
    assert code == 2 and "llmop init" in err


def test_broken_yaml_is_reported(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("server: [unclosed\n", encoding="utf-8")
    code, _, err = run(capsys, "--config", path, "serve")
    assert code == 2 and "YAML" in err


def test_python_dash_m_llmop():
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    r = subprocess.run([sys.executable, "-m", "llmop", "--version"], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0 and __version__ in r.stdout


# ------------------------------------------------------------------ serve (mocked)
@dataclass
class FakeState:
    store: object
    config: object
    tunnel: object = None


class FakeTunnel:
    instances: list["FakeTunnel"] = []

    def __init__(self, settings, local_url, store=None, data_dir=None, **kw):
        self.settings, self.local_url, self.store, self.data_dir, self.kw = (
            settings, local_url, store, data_dir, kw)
        self.status, self.mode, self.url = "off", settings.mode, None
        self.started = self.stopped = False
        FakeTunnel.instances.append(self)

    def start(self):
        self.started, self.status = True, "starting"
        return True

    def stop(self):
        self.stopped, self.status = True, "stopped"


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Replaces state building, the app and uvicorn; records what serve did."""
    rec: dict = {}
    FakeTunnel.instances = []

    def build_state(cfg):
        rec["cfg"] = cfg
        rec["state"] = FakeState(store=Store(":memory:"), config=cfg)
        return rec["state"]

    def create_app(state):
        rec["app_state"] = state
        return "the-app"

    def run_uvicorn(app, **kw):
        rec["app"] = app
        rec["uvicorn"] = kw
        rec["tunnel_during_run"] = rec["state"].tunnel
        if rec.get("raise"):
            raise rec["raise"]

    monkeypatch.setattr(cli, "_build_state", build_state)
    monkeypatch.setattr(cli, "_create_app", create_app)
    monkeypatch.setattr(cli, "_run_uvicorn", run_uvicorn)
    monkeypatch.setattr(cli, "TunnelManager", FakeTunnel)
    return rec


def test_serve_refuses_the_placeholder_password(tmp_path, capsys, served):
    path = tmp_path / "config.yaml"
    path.write_text(cli._example_text(), encoding="utf-8")
    code, _, err = run(capsys, "--config", path, "serve")
    assert code == 2
    assert "placeholder" in err and "llmop set-password admin" in err and "hash-password" in err
    assert "uvicorn" not in served and "cfg" not in served


def test_serve_placeholder_message_in_a_container(tmp_path, capsys, served, monkeypatch):
    monkeypatch.setenv("LLMOP_IN_CONTAINER", "1")
    path = tmp_path / "config.yaml"
    path.write_text(cli._example_text(), encoding="utf-8")
    code, _, err = run(capsys, "--config", path, "serve")
    assert code == 2 and "docker compose run --rm --no-deps llmop hash-password" in err


def test_serve_with_tunnel_wires_everything(config_file, capsys, served):
    port = free_port()
    code, out, err = run(capsys, "--config", config_file, "serve", "--tunnel", "--port", port)
    assert code == 0, err
    kw = served["uvicorn"]
    assert served["app"] == "the-app"
    assert kw["host"] == "127.0.0.1" and kw["port"] == port
    assert kw["proxy_headers"] is True
    assert set(kw["forwarded_allow_ips"].split(",")) >= {"127.0.0.1", "::1"}
    assert kw["log_level"] == "info"
    [tunnel] = FakeTunnel.instances
    assert served["tunnel_during_run"] is tunnel  # state.tunnel is set before uvicorn runs
    assert tunnel.started and tunnel.stopped  # ... and stopped afterwards
    assert tunnel.local_url == f"http://127.0.0.1:{port}"
    assert tunnel.store is served["state"].store
    assert tunnel.data_dir == served["cfg"].data_path
    assert f"http://127.0.0.1:{port}/" in out
    assert "WARNING" not in err


def test_serve_tunnel_follows_the_config_unless_overridden(config_file, capsys, served):
    port = free_port()
    assert run(capsys, "--config", config_file, "serve", "--port", port)[0] == 0
    assert FakeTunnel.instances == []  # tunnel.enabled is false in the example

    text = config_file.read_text(encoding="utf-8").replace("  enabled: false", "  enabled: true")
    config_file.write_text(text, encoding="utf-8")
    assert run(capsys, "--config", config_file, "serve", "--port", port)[0] == 0
    assert len(FakeTunnel.instances) == 1
    assert run(capsys, "--config", config_file, "serve", "--no-tunnel", "--port", port)[0] == 0
    assert len(FakeTunnel.instances) == 1


def test_serve_trusted_proxies_go_to_uvicorn(config_file, capsys, served):
    text = config_file.read_text(encoding="utf-8").replace(
        "  port: 8780", '  port: 8780\n  trusted_proxies: ["127.0.0.1", "172.16.0.0/12"]')
    config_file.write_text(text, encoding="utf-8")
    assert run(capsys, "--config", config_file, "serve", "--port", free_port())[0] == 0
    assert served["uvicorn"]["forwarded_allow_ips"] == "127.0.0.1,::1,172.16.0.0/12"


def test_serve_stops_the_tunnel_when_uvicorn_fails(config_file, capsys, served):
    served["raise"] = SystemExit(3)  # what uvicorn does when it cannot start
    with pytest.raises(SystemExit):
        cli.main(["--config", str(config_file), "serve", "--tunnel", "--port", str(free_port())])
    assert FakeTunnel.instances[0].stopped


def test_serve_warns_when_listening_on_all_interfaces(config_file, capsys, served, monkeypatch):
    code, _, err = run(capsys, "--config", config_file, "serve", "--host", "0.0.0.0", "--port", free_port())
    assert code == 0
    assert "WARNING: listening on 0.0.0.0" in err
    assert served["uvicorn"]["host"] == "0.0.0.0"

    monkeypatch.setenv("LLMOP_IN_CONTAINER", "1")
    code, _, err = run(capsys, "--config", config_file, "serve", "--host", "0.0.0.0", "--port", free_port())
    assert code == 0 and "WARNING" not in err


def test_serve_refuses_a_busy_port(config_file, capsys, served):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code, _, err = run(capsys, "--config", config_file, "serve", "--port", port)
    assert code == 2
    assert "cannot listen" in err and "another window" in err
    assert "uvicorn" not in served


def test_serve_forgets_a_url_left_by_a_killed_tunnel(config_file, capsys, served, monkeypatch):
    original = cli._build_state

    def build_state(cfg):
        state = original(cfg)
        state.store.kv_set("public_url", "https://dead.trycloudflare.com")
        return state

    monkeypatch.setattr(cli, "_build_state", build_state)
    assert run(capsys, "--config", config_file, "serve", "--port", free_port())[0] == 0
    assert served["state"].store.kv_get("public_url")[0] == ""


def test_data_dir_environment_override(config_file, capsys, served, monkeypatch, tmp_path):
    monkeypatch.setenv("LLMOP_DATA_DIR", str(tmp_path / "elsewhere"))
    assert run(capsys, "--config", config_file, "serve", "--port", free_port())[0] == 0
    assert served["cfg"].data_path == tmp_path / "elsewhere"


def test_local_url_helpers():
    assert cli.local_base_url("0.0.0.0", 8780) == "http://127.0.0.1:8780"
    assert cli.local_base_url("::1", 8780) == "http://[::1]:8780"
    assert cli.local_base_url("192.168.1.5", 80) == "http://192.168.1.5:80"
    assert cli.is_loopback_host("127.0.0.1") and cli.is_loopback_host("localhost")
    assert cli.is_loopback_host("::1") and cli.is_loopback_host("[::1]")
    assert not cli.is_loopback_host("0.0.0.0") and not cli.is_loopback_host("192.168.1.5")


# ------------------------------------------------------------------ tunnel command (mocked)
def test_tunnel_command_runs_and_stops_the_manager(config_file, capsys, monkeypatch):
    class OneShot(FakeTunnel):
        def wait(self):
            self.waited = True

    FakeTunnel.instances = []
    monkeypatch.setattr(cli, "TunnelManager", OneShot)
    code, out, err = run(capsys, "--config", config_file, "tunnel", "--url", "http://127.0.0.1:9999/")
    assert code == 0, err
    [mgr] = FakeTunnel.instances
    assert mgr.local_url == "http://127.0.0.1:9999"
    assert mgr.started and mgr.waited and mgr.stopped
    assert mgr.settings.mode == "quick"


# ------------------------------------------------------------------ check
def test_check_lists_accounts_without_hashes(config_file, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_backend_health", lambda cfg: [
        {"name": "ollama", "type": "ollama", "base_url": "http://127.0.0.1:11434", "reachable": True,
         "model_count": 7, "models": ["m1", "m2", "m3", "m4", "m5", "m6", "m7"], "error": None,
         "latency_ms": 12}])
    monkeypatch.setattr(cli, "conflicting_config_files", lambda *a: [])
    code, out, err = run(capsys, "--config", config_file, "check")
    assert code == 0, out + err
    assert "admin" in out and "unlimited" in out
    assert "scrypt$" not in out + err
    assert "[OK] ollama" in out and "models: 7" in out and "m1, m2" in out and "(+2)" in out
    assert "All good." in out


def test_backend_health_uses_the_router(config_file):
    """The real BackendRouter (work line A) against a port where nothing listens."""
    cfg = load_config(config_file)
    cfg.backends[0].base_url = f"http://127.0.0.1:{free_port()}"
    cfg.backends[0].connect_timeout = 2
    health = cli._backend_health(cfg)
    assert [h["name"] for h in health] == ["ollama"]
    assert health[0]["reachable"] is False and health[0]["error"]


def test_check_reports_unreachable_backends_and_placeholders(tmp_path, capsys, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(cli._example_text(), encoding="utf-8")
    monkeypatch.setattr(cli, "_backend_health", lambda cfg: [
        {"name": "ollama", "type": "ollama", "reachable": False, "models": 0, "error": "connection refused"}])
    code, out, err = run(capsys, "--config", path, "check")
    assert code == 1
    assert "NOT reachable: connection refused" in out
    assert "placeholder" in err


def test_check_degrades_when_backend_testing_is_unavailable(config_file, capsys, monkeypatch):
    def broken(cfg):
        raise ImportError("no backends package")

    monkeypatch.setattr(cli, "_backend_health", broken)
    code, out, err = run(capsys, "--config", config_file, "check")
    assert code == 1
    assert "could not test the backends" in err
    assert "http://127.0.0.1:11434" in out


# ------------------------------------------------------------------ usage / keys / revoke-key
def test_usage_keys_and_revoke(config_file, capsys):
    cfg = load_config(config_file)
    assert run(capsys, "--config", config_file, "usage")[1].startswith("No usage")
    assert run(capsys, "--config", config_file, "keys")[1].startswith("No API keys")

    store = Store(cfg.data_path / "llmop.sqlite3")
    _, rec = store.create_api_key("admin", name="laptop")
    store.record_usage(UsageEvent(username="admin", endpoint="chat.completions", model="m",
                                  backend="ollama", prompt_tokens=1200, completion_tokens=34))
    store.record_usage(UsageEvent(username="ghost", endpoint="chat.completions", model="m",
                                  backend="ollama", prompt_tokens=5, completion_tokens=5))
    store.close()

    code, out, _ = run(capsys, "--config", config_file, "usage")
    assert code == 0
    assert "1,234" in out and "admin" in out
    assert "ghost" in out and "(removed)" in out

    code, out, _ = run(capsys, "--config", config_file, "usage", "--user", "admin", "--days", "7")
    assert code == 0 and "1,234 tokens" in out and "today: 1,234 / unlimited" in out

    code, out, _ = run(capsys, "--config", config_file, "keys")
    assert code == 0 and rec.id in out and "laptop" in out and rec.display in out and "active" in out
    assert "sk-llop-" in out and len(rec.display) < 20  # only the short display form

    assert run(capsys, "--config", config_file, "revoke-key", rec.id)[0] == 0
    assert run(capsys, "--config", config_file, "keys")[1].startswith("No API keys")
    code, out, _ = run(capsys, "--config", config_file, "keys", "--all", "--user", "admin")
    assert "revoked" in out
    code, _, err = run(capsys, "--config", config_file, "revoke-key", rec.id)
    assert code == 1 and "no active API key" in err


# ------------------------------------------------------------------ v0.2 node (T-9)
@pytest.fixture
def node_run(monkeypatch):
    rec: dict = {}

    def run_node(cfg, **kw):
        rec["cfg"], rec["kw"] = cfg, kw
        return 0

    monkeypatch.setattr(cli, "_run_node", run_node)
    monkeypatch.setattr(cli, "_run_uvicorn", lambda app, **kw: rec.setdefault("uvicorn", kw))
    return rec


def test_serve_without_port_starts_the_node(config_file, capsys, node_run):
    code, out, err = run(capsys, "--config", config_file, "serve")
    assert code == 0, err
    assert node_run["kw"] == {"tunnels": None, "worker": None, "log_level": "info"}
    assert "uvicorn" not in node_run  # not the v0.1 single listener
    assert run(capsys, "--config", config_file, "serve", "--no-tunnel", "--worker")[0] == 0
    assert node_run["kw"]["tunnels"] is False and node_run["kw"]["worker"] is True


def test_serve_node_still_refuses_the_placeholder_password(tmp_path, capsys, node_run):
    path = tmp_path / "config.yaml"
    path.write_text(cli._example_text(), encoding="utf-8")
    code, _, err = run(capsys, "--config", path, "serve")
    assert code == 2 and "placeholder" in err and "cfg" not in node_run


def test_gateway_aliases(config_file, capsys, node_run, tmp_path, no_prompt):
    from llmop.__main__ import main as llmop_main

    assert llmop_main(["gateway", "run", "--config", str(config_file), "--no-tunnel"]) == 0
    assert node_run["kw"]["tunnels"] is False
    path = tmp_path / "g.yaml"
    assert llmop_main(["gateway", "init", "--config", str(path), "--admin-password", ADMIN_PW]) == 0
    assert verify_password(ADMIN_PW, load_config(path).accounts["admin"].password_hash)
    assert llmop_main(["gateway"]) == 2
    assert llmop_main(["gateway", "nope"]) == 2
    assert "gateway {run,init}" in capsys.readouterr().err


def test_role_cli_without_the_secure_extra_prints_a_hint(monkeypatch, capsys):
    import importlib

    from llmop import __main__ as entry

    real = importlib.import_module

    def fake_import(name, *a, **kw):
        if name == "llmop.worker.cli":
            raise ModuleNotFoundError("No module named 'cryptography'", name="cryptography")
        return real(name, *a, **kw)

    monkeypatch.setattr(entry.importlib, "import_module", fake_import)
    assert entry.main(["worker", "run"]) == 2
    err = capsys.readouterr().err
    assert 'pip install "localllm-outsider-provider[secure]"' in err
    assert "Traceback" not in err


def test_init_model_option(tmp_path, capsys, no_prompt):
    path = tmp_path / "config.yaml"
    code, _, err = run(capsys, "--config", path, "init", "--admin-password", ADMIN_PW,
                       "--model", "qwen3.5:9b")
    assert code == 0, err
    cfg = load_config(path)
    assert cfg.model == "qwen3.5:9b"
    assert cfg.listeners.local == 8781 and cfg.tunnels.api.mode == "quick"
    assert "# Ports of the three entry points" in path.read_text(encoding="utf-8")


def test_tunnel_rotate_and_show_api(config_file, capsys):
    data = config_file.parent / "data"
    code, out, _ = run(capsys, "--config", config_file, "tunnel", "show-api")
    assert code == 0
    first = re.search(r"API link : (\S+)/v1", out).group(1)
    assert first.startswith("http://127.0.0.1:8782/")
    code, out, _ = run(capsys, "--config", config_file, "tunnel", "rotate-api", "--reason", "leak")
    assert code == 0 and "old link stopped working" in out
    second = re.search(r"(http://127\.0\.0\.1:8782/\S+)/v1", out).group(1)
    assert second != first
    code, out, _ = run(capsys, "--config", config_file, "tunnel", "show-api")
    assert second in out
    code, out, _ = run(capsys, "--config", config_file, "tunnel", "rotate-api", "--grace", "600")
    assert code == 0 and "600 more seconds" in out
    audit = (data / "api_link_audit.jsonl").read_text(encoding="utf-8")
    assert '"reason": "leak"' in audit
    assert first.rsplit("/", 1)[1] not in audit and second.rsplit("/", 1)[1] not in audit


def test_tunnel_rotate_api_change_host_without_a_node(config_file, capsys):
    code, out, err = run(capsys, "--config", config_file, "tunnel", "rotate-api", "--change-host",
                         "--wait", "0.2")
    assert code == 0
    assert "no running node answered" in err and "[OK] new API link" in out
    store = Store(config_file.parent / "data" / "llmop.sqlite3")
    try:
        assert store.kv_get("api_tunnel_restart")[0] == ""  # not left for a later start
    finally:
        store.close()


def test_tunnel_command_refuses_the_local_console_port(config_file, capsys, monkeypatch):
    seen = {}

    class Refusing(FakeTunnel):
        def start(self):
            seen["forbidden"] = self.kw.get("forbidden_ports")
            return False

    monkeypatch.setattr(cli, "TunnelManager", Refusing)
    code, _, _ = run(capsys, "--config", config_file, "tunnel", "--url", "http://127.0.0.1:8781")
    assert code == 1
    assert 8781 in seen["forbidden"] and 11434 in seen["forbidden"]
