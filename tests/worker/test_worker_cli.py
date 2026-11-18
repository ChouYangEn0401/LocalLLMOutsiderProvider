"""`llmop worker` 命令列與金鑰庫：init、show-key、rotate-key、prune-keys、run 的前置檢查。"""

import json
import os
import stat

import pytest

from llmop import paths
from llmop.__main__ import main as llmop_main
from llmop.crypto import public_raw, worker_key_id
from llmop.protocol import b64u_decode
from llmop.worker import cli
from llmop.worker.keystore import KeyStoreError, load_keys


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_init_on_fresh_home(home, capsys):
    assert llmop_main(["worker", "init"]) == 0
    out = capsys.readouterr().out
    store = load_keys(home / "keys.json")
    key = store.current_key()
    assert key.key_id in out and key.public_key in out
    assert worker_key_id(b64u_decode(key.public_key)) == key.key_id
    assert mode(home / "keys.json") == 0o600
    assert mode(home / "config.json") == 0o600
    assert json.loads((home / "config.json").read_text())["gateway_url"].endswith("/worker/ws")

    # 第二次 init 不會換掉金鑰。
    assert cli.main(["init"]) == 0
    again = load_keys(home / "keys.json")
    assert [k.key_id for k in again.keys] == [key.key_id]
    assert key.key_id in capsys.readouterr().out


def test_rotate_show_and_prune(home, capsys):
    cli.main(["init"])
    first = load_keys(home / "keys.json").current
    assert cli.main(["rotate-key"]) == 0
    store = load_keys(home / "keys.json")
    assert store.current != first
    assert [k.key_id for k in store.ordered()] == [store.current, first]
    assert mode(home / "keys.json") == 0o600

    capsys.readouterr()
    assert cli.main(["show-key"]) == 0
    out = capsys.readouterr().out
    assert out.index(store.current) < out.index(first)

    assert cli.main(["prune-keys", "--yes"]) == 0
    pruned = load_keys(home / "keys.json")
    assert [k.key_id for k in pruned.keys] == [store.current]
    assert mode(home / "keys.json") == 0o600
    assert cli.main(["prune-keys", "--yes"]) == 0  # 沒有舊金鑰時什麼都不做


def test_prune_asks_for_confirmation(home, monkeypatch):
    cli.main(["init"])
    cli.main(["rotate-key"])
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert cli.main(["prune-keys"]) == 1
    assert len(load_keys(home / "keys.json").keys) == 2
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert cli.main(["prune-keys"]) == 0
    assert len(load_keys(home / "keys.json").keys) == 1


def test_commands_without_keys_fail_cleanly(home, capsys):
    for command in (["show-key"], ["rotate-key"], ["prune-keys", "--yes"]):
        assert cli.main(command) == 1
        assert "llmop worker init" in capsys.readouterr().err


def test_run_prints_key_and_requires_reg_token_on_first_run(home, capsys):
    cli.main(["init"])
    capsys.readouterr()
    assert cli.main(["run"]) == 1
    captured = capsys.readouterr()
    key = load_keys(home / "keys.json").current_key()
    assert key.key_id in captured.out and key.public_key in captured.out
    assert "--reg-token" in captured.err


def test_corrupted_keys_file_is_reported(home, capsys):
    cli.main(["init"])
    data = paths.read_json(home / "keys.json")
    data["keys"][0]["key_id"] = "A" * 22
    paths.write_json(home / "keys.json", data, secret=True)
    with pytest.raises(KeyStoreError):
        load_keys(home / "keys.json")
    assert cli.main(["show-key"]) == 1
    assert "key_id" in capsys.readouterr().err


def test_key_id_matches_public_key(home):
    cli.main(["init"])
    key = load_keys(home / "keys.json").current_key()
    assert key.key_id == worker_key_id(public_raw(key.private_key))
