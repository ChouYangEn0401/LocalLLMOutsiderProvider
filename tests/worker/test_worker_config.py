"""worker 設定的啟動檢查：迴圈位址後端、gateway_url 的協定與主機。"""

import pytest

from llmop import paths
from llmop.crypto import generate_signing_key, public_raw
from llmop.protocol import b64u_encode
from llmop.worker import cli
from llmop.worker.config import ConfigError, default_config, parse_config


def config(**overrides):
    obj = default_config()
    obj.update(overrides)
    return obj


def test_default_config_is_valid():
    parsed = parse_config(default_config())
    # R-11: the v0.2 node's local listener (B's old 8787 is gone)
    assert parsed.gateway_url == "ws://127.0.0.1:8781/worker/ws"
    assert parsed.backends[0].kind == "ollama"
    assert parsed.max_concurrency == 2
    assert parsed.request_timeout == 1800
    assert parsed.expose_models == frozenset()
    assert parsed.pinned_clients == frozenset()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://127.3.4.5:11434/",
        "http://[::1]:1234",
        "http://localhost:1234",
        "http://LOCALHOST:1234",
    ],
)
def test_loopback_backend_urls_are_accepted(url):
    parsed = parse_config(config(backends=[{"kind": "openai", "base_url": url, "api_key": ""}]))
    assert parsed.backends[0].base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.5:11434",
        "http://10.0.0.1:11434",
        "http://0.0.0.0:11434",
        "http://example.com:11434",
        "http://localhost.example.com:11434",
        "http://[::2]:11434",
        "http://[::ffff:127.0.0.1]:11434",
        "https://127.0.0.1:11434",
        "ftp://127.0.0.1",
        "http://user:pw@127.0.0.1:11434",
        "http://127.0.0.1:11434/v1",
        "127.0.0.1:11434",
        "",
    ],
)
def test_non_loopback_backend_urls_are_refused(url):
    with pytest.raises(ConfigError):
        parse_config(config(backends=[{"kind": "ollama", "base_url": url}]))


@pytest.mark.parametrize(
    "url",
    ["wss://abc.trycloudflare.com/worker/ws", "ws://127.0.0.1:8787/worker/ws", "ws://localhost/worker/ws",
     "ws://[::1]:8787/worker/ws"],
)
def test_gateway_url_accepted(url):
    assert parse_config(config(gateway_url=url)).gateway_url == url


@pytest.mark.parametrize(
    "url",
    ["ws://abc.trycloudflare.com/worker/ws", "ws://192.168.0.2:8787/worker/ws", "http://127.0.0.1:8787/worker/ws",
     "https://abc.trycloudflare.com/worker/ws", "wss:///worker/ws", ""],
)
def test_gateway_url_refused(url):
    with pytest.raises(ConfigError):
        parse_config(config(gateway_url=url))


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown": 1},
        {"backends": []},
        {"backends": [{"kind": "vllm", "base_url": "http://127.0.0.1:8000"}]},
        {"backends": [{"kind": "ollama", "base_url": "http://127.0.0.1:11434", "api_key": "x"}]},
        {"max_concurrency": 0},
        {"max_concurrency": True},
        {"request_timeout": 0},
        {"request_timeout": "60"},
        {"expose_models": "qwen3:8b"},
        {"pinned_clients": ["not-a-key"]},
        {"pinned_clients": "x"},
    ],
)
def test_bad_values_are_refused(overrides):
    with pytest.raises(ConfigError):
        parse_config(config(**overrides))


def test_pinned_clients_decoded():
    pub = public_raw(generate_signing_key())
    assert parse_config(config(pinned_clients=[b64u_encode(pub)])).pinned_clients == {pub}


def test_missing_gateway_url_refused():
    obj = default_config()
    del obj["gateway_url"]
    with pytest.raises(ConfigError, match="gateway_url"):
        parse_config(obj)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"backends": [{"kind": "ollama", "base_url": "http://192.168.1.5:11434"}]}, "迴圈位址"),
        ({"gateway_url": "ws://gateway.example.com/worker/ws"}, "wss://"),
    ],
)
def test_run_refuses_to_start_with_bad_config(home, keystore, capsys, overrides, message):
    paths.write_json(home / "config.json", config(**overrides))
    assert cli.main(["run", "--reg-token", "llmop_reg_x"]) != 0
    assert message in capsys.readouterr().err


def test_run_without_config_fails_cleanly(home, capsys):
    assert cli.main(["run"]) == 1
    assert "llmop worker init" in capsys.readouterr().err
