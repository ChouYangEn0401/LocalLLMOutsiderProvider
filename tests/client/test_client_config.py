from __future__ import annotations

import os
import stat

import pytest

from llmop import crypto, paths
from llmop.client.config import (
    ClientConfig,
    ConfigError,
    config_path,
    ensure_signing_key,
    load_config,
    load_signing_key,
    signing_key_info,
    signing_key_path,
)
from llmop.protocol import b64u_encode

from .fakes import API_KEY

PINNED = b64u_encode(crypto.public_raw(crypto.generate_worker_key()))


def make(**overrides) -> dict:
    data = {"gateway_url": "https://demo.trycloudflare.com", "api_key": API_KEY, "pinned_workers": [PINNED]}
    data.update(overrides)
    return data


def test_defaults_and_base_url():
    config = ClientConfig.from_dict(make(gateway_url="https://demo.trycloudflare.com/"))
    assert config.proxy_port == 8788
    assert config.default_max_tokens == 1024
    assert config.pinned_workers == (PINNED,)
    assert config.base_url == "https://demo.trycloudflare.com"


@pytest.mark.parametrize(
    "url",
    ["https://demo.trycloudflare.com", "http://127.0.0.1:8787", "http://localhost:8787", "http://[::1]:8787"],
)
def test_gateway_url_accepted(url):
    ClientConfig.from_dict(make(gateway_url=url))


@pytest.mark.parametrize(
    "url",
    ["http://demo.trycloudflare.com", "http://192.168.1.10:8787", "ftp://127.0.0.1", "https://", "demo", 42],
)
def test_gateway_url_rejected(url):
    with pytest.raises(ConfigError):
        ClientConfig.from_dict(make(gateway_url=url))


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "sk-openai"},
        {"pinned_workers": ["not-a-key"]},
        {"pinned_workers": [PINNED + "="]},
        {"pinned_workers": PINNED},
        {"proxy_port": 0},
        {"proxy_port": "8788"},
        {"default_max_tokens": 0},
        {"default_max_tokens": 1_000_001},
        {"default_max_tokens": True},
        {"pinned_worker": [PINNED]},
    ],
)
def test_invalid_values_rejected(overrides):
    with pytest.raises(ConfigError):
        ClientConfig.from_dict(make(**overrides))


def test_missing_field_rejected():
    data = make()
    del data["api_key"]
    with pytest.raises(ConfigError, match="api_key"):
        ClientConfig.from_dict(data)


def test_load_config_missing_file_explains_format():
    with pytest.raises(ConfigError, match="pinned_workers"):
        load_config()


def test_load_config_from_home():
    paths.write_json(config_path(), make(proxy_port=9999), secret=True)
    assert load_config().proxy_port == 9999


def test_signing_key_created_once_with_mode_0600():
    key, created = ensure_signing_key()
    assert created
    assert stat.S_IMODE(os.stat(signing_key_path()).st_mode) == 0o600
    again, created_again = ensure_signing_key()
    assert not created_again
    assert signing_key_info(again) == signing_key_info(key)
    assert signing_key_info(load_signing_key()) == signing_key_info(key)
    public_key, sig_key_id = signing_key_info(key)
    assert sig_key_id == crypto.signing_key_id(crypto.public_raw(key))
    assert paths.read_json(signing_key_path())["sig_key_id"] == sig_key_id


def test_signing_key_mismatch_rejected():
    ensure_signing_key()
    data = paths.read_json(signing_key_path())
    data["public_key"] = b64u_encode(crypto.public_raw(crypto.generate_signing_key()))
    paths.write_json(signing_key_path(), data, secret=True)
    with pytest.raises(ConfigError):
        load_signing_key()


def test_missing_signing_key_points_to_keygen():
    with pytest.raises(ConfigError, match="keygen"):
        load_signing_key()


@pytest.mark.parametrize("api_key", ["sk-llop-" + "A" * 43, "llmop_sk_" + "A" * 43])
def test_both_key_prefixes_accepted(api_key):
    assert ClientConfig.from_dict(make(api_key=api_key)).api_key == api_key


@pytest.mark.parametrize("api_key", ["sk-llop", "sk_llop_abc", "llmop-sk-abc", "SK-LLOP-abc", ""])
def test_other_key_prefixes_rejected(api_key):
    with pytest.raises(ConfigError):
        ClientConfig.from_dict(make(api_key=api_key))


@pytest.mark.parametrize(
    ("url", "base"),
    [
        ("https://demo.trycloudflare.com/g4te-S3cret", "https://demo.trycloudflare.com/g4te-S3cret"),
        ("https://demo.trycloudflare.com/g4te-S3cret/", "https://demo.trycloudflare.com/g4te-S3cret"),
        ("http://127.0.0.1:8782/g4te", "http://127.0.0.1:8782/g4te"),
    ],
)
def test_gateway_url_with_gate_path(url, base):
    assert ClientConfig.from_dict(make(gateway_url=url)).base_url == base
