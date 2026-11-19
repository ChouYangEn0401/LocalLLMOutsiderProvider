"""路徑與 JSON 設定檔讀寫測試。"""

import os
import stat

import pytest

from llmop import paths


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_home_honours_llmop_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMOP_HOME", str(tmp_path / "state"))
    path = paths.home("worker")
    assert path == tmp_path / "state" / "worker"
    assert path.is_dir()
    assert mode(path) == 0o700
    assert mode(tmp_path / "state") == 0o700
    assert paths.home("worker") == path  # 第二次呼叫不出錯


def test_home_defaults_to_dot_llmop(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMOP_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.home("client") == tmp_path / ".llmop" / "client"


def test_write_and_read_round_trip(tmp_path):
    target = tmp_path / "config.json"
    data = {"name": "測試", "n": 1, "list": [1, 2]}
    paths.write_json(target, data)
    assert paths.read_json(target) == data
    assert mode(target) & 0o077 != 0o077  # 一般檔案不強制 0600


def test_secret_write_is_0600(tmp_path):
    target = tmp_path / "keys.json"
    paths.write_json(target, {"k": "v"}, secret=True)
    assert mode(target) == 0o600


def test_secret_write_replaces_open_file_with_0600(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("{}")
    os.chmod(target, 0o644)
    paths.write_json(target, {"token": "x"}, secret=True)
    assert mode(target) == 0o600


def test_secret_mode_is_set_before_content(tmp_path, monkeypatch):
    seen = []
    real_fdopen = os.fdopen

    def spying_fdopen(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        real_write = fh.write

        def write(text):
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return real_write(text)

        fh.write = write
        return fh

    monkeypatch.setattr(paths.os, "fdopen", spying_fdopen)
    paths.write_json(tmp_path / "keys.json", {"k": "v"}, secret=True)
    assert seen and all(m == 0o600 for m in seen)


def test_failed_serialisation_leaves_old_file(tmp_path):
    target = tmp_path / "config.json"
    paths.write_json(target, {"old": True})
    with pytest.raises(TypeError):
        paths.write_json(target, {"bad": object()})
    assert paths.read_json(target) == {"old": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]


def test_failed_replace_leaves_no_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    paths.write_json(target, {"old": True})

    def broken_replace(src, dst):
        raise OSError("磁碟壞了")

    monkeypatch.setattr(paths.os, "replace", broken_replace)
    with pytest.raises(OSError):
        paths.write_json(target, {"new": True}, secret=True)
    assert paths.read_json(target) == {"old": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]


@pytest.mark.parametrize("secret", [True, False])
def test_write_json_without_fchmod(tmp_path, monkeypatch, secret):
    """Windows（Python 3.12）沒有 os.fchmod：照樣寫得成功、不留暫存檔。"""
    monkeypatch.delattr(paths.os, "fchmod", raising=False)
    assert not hasattr(os, "fchmod")
    target = tmp_path / "keys.json"
    target.write_text("{}")
    paths.write_json(target, {"k": "v"}, secret=secret)
    assert paths.read_json(target) == {"k": "v"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keys.json"]


def test_read_json_requires_object(tmp_path):
    target = tmp_path / "list.json"
    target.write_text("[1, 2]")
    with pytest.raises(ValueError):
        paths.read_json(target)
