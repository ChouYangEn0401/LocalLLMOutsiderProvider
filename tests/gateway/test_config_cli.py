"""閘道設定與命令列。"""

from __future__ import annotations

import pytest

from llmop import paths
from llmop.gateway import cli
from llmop.gateway.config import GatewayConfig, load_config
from llmop.gateway.store import Store


def test_defaults(llmop_home):
    config = load_config()
    assert config == GatewayConfig()
    assert (config.host, config.port, config.admin_local_only) == ("127.0.0.1", 8787, True)
    assert (config.sync_wait_seconds, config.request_timeout_seconds, config.max_inflight) == (80, 1800, 150)


def test_config_file(llmop_home):
    paths.write_json(paths.home("gateway") / "config.json", {"port": 9000, "max_inflight": 10, "host": "::1"})
    config = load_config()
    assert (config.port, config.max_inflight, config.host) == (9000, 10, "::1")


@pytest.mark.parametrize(
    "data",
    [
        {"host": "0.0.0.0"},
        {"host": "192.168.1.5"},
        {"host": "example.com"},
        {"port": "8787"},
        {"admin_local_only": 1},
        {"sync_wait_seconds": 0},
        {"max_inflight": 0},
        {"unknown": 1},
    ],
)
def test_bad_config(data):
    with pytest.raises(ValueError):
        GatewayConfig.from_dict(data)


def test_init_writes_password_hash(llmop_home, monkeypatch, capsys):
    answers = iter(["s3cret-password", "s3cret-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["init"]) == 0
    store = Store.open_default()
    try:
        salt, digest = store.admin_password_record()
        assert len(salt) == 16 and len(digest) == 32
        assert b"s3cret-password" not in store.path.read_bytes()
    finally:
        store.close()


def test_init_rejects_mismatch_and_short(llmop_home, monkeypatch):
    answers = iter(["s3cret-password", "different-one"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["init"]) == 2
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "short")
    assert cli.main(["init"]) == 2


def test_run_requires_init(llmop_home, capsys):
    assert cli.main(["run"]) == 2
    assert "llmop gateway init" in capsys.readouterr().err


def test_run_refuses_public_host(llmop_home, capsys):
    paths.write_json(paths.home("gateway") / "config.json", {"host": "0.0.0.0"})
    assert cli.main(["run"]) == 2
    assert "host" in capsys.readouterr().err


def test_run_starts_server(llmop_home, monkeypatch, capsys):
    store = Store.open_default()
    store.set_admin_password("s3cret-password")
    store.close()
    started = {}

    def fake_run_app(app, host, port, print):
        started.update(app=app, host=host, port=port)

    monkeypatch.setattr("aiohttp.web.run_app", fake_run_app)
    assert cli.main(["run"]) == 0
    assert (started["host"], started["port"]) == ("127.0.0.1", 8787)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8787" in out
    assert "cloudflared tunnel --url http://127.0.0.1:8787" in out


def test_no_command_prints_usage(capsys):
    assert cli.main([]) == 2
