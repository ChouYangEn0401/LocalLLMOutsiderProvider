"""worker 設定（$LLMOP_HOME/worker/config.json）的讀取與啟動檢查。

規則：
- gateway_url 必須是 wss://；只有連到本機迴圈位址時才允許 ws://。
- 每個後端的 base_url 都必須是 http:// 加上迴圈位址（127.0.0.0/8、::1、localhost），沒有例外開關。
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from llmop.protocol import MAX_MODEL_LEN, REQUEST_TIMEOUT_SECONDS, ProtocolError, b64u_decode

BACKEND_KINDS = ("ollama", "openai")

_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_address("::1")

_TOP_LEVEL_KEYS = {"gateway_url", "backends", "expose_models", "max_concurrency", "request_timeout", "pinned_clients"}


class ConfigError(Exception):
    """設定檔有問題；訊息是給操作者看的繁體中文說明。"""


@dataclass(frozen=True)
class BackendConfig:
    kind: str  # "ollama" 或 "openai"（LM Studio、vLLM）
    base_url: str  # 不含結尾斜線，例如 http://127.0.0.1:11434
    api_key: str = ""  # 只有 openai 類型使用；空字串表示不送 Authorization


@dataclass(frozen=True)
class WorkerConfig:
    gateway_url: str
    backends: tuple[BackendConfig, ...]
    expose_models: frozenset[str] = frozenset()  # 空＝全部對外提供
    max_concurrency: int = 2
    request_timeout: float = REQUEST_TIMEOUT_SECONDS
    pinned_clients: frozenset[bytes] = frozenset()  # 原始 32 位元組 Ed25519 公鑰；空＝信任閘道轉來的公鑰


def default_config() -> dict:
    """`llmop worker init` 寫出的設定範本：連到同一台機器上的閘道、使用本機 Ollama。"""
    return {
        "gateway_url": "ws://127.0.0.1:8787/worker/ws",
        "backends": [{"kind": "ollama", "base_url": "http://127.0.0.1:11434"}],
        "expose_models": [],
        "max_concurrency": 2,
        "request_timeout": REQUEST_TIMEOUT_SECONDS,
        "pinned_clients": [],
    }


def is_loopback_host(host: str | None) -> bool:
    """host 是否為 localhost、127.0.0.0/8 或 ::1。"""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 4:
        return address in _LOOPBACK_V4
    return address == _LOOPBACK_V6


def _split_url(url: object, name: str):
    if not isinstance(url, str) or not url:
        raise ConfigError(f"{name} 必須是非空字串")
    try:
        parts = urlsplit(url)
        parts.port  # 埠號不合法時會丟 ValueError
    except ValueError as exc:
        raise ConfigError(f"{name} 不是合法的網址：{url}") from exc
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{name} 不可以包含帳號密碼：{url}")
    return parts


def check_gateway_url(url: object) -> str:
    parts = _split_url(url, "gateway_url")
    if parts.scheme == "wss" and parts.hostname:
        return url
    if parts.scheme == "ws":
        if is_loopback_host(parts.hostname):
            return url
        raise ConfigError(f"gateway_url 用 ws:// 時只能連到本機迴圈位址；連到遠端閘道請用 wss://（目前是 {url}）")
    raise ConfigError(f"gateway_url 必須是 wss://…（或連到本機的 ws://…），目前是 {url}")


def check_backend_url(url: object) -> str:
    parts = _split_url(url, "後端 base_url")
    if parts.scheme != "http" or not is_loopback_host(parts.hostname):
        raise ConfigError(
            f"後端 base_url 必須是 http:// 加上本機迴圈位址（127.0.0.0/8、::1、localhost），目前是 {url}"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError(f"後端 base_url 只填到埠號，例如 http://127.0.0.1:11434（目前是 {url}）")
    return url.rstrip("/")


def _parse_backend(obj: object, index: int) -> BackendConfig:
    where = f"backends[{index}]"
    if not isinstance(obj, dict):
        raise ConfigError(f"{where} 必須是 JSON 物件")
    kind = obj.get("kind")
    if kind not in BACKEND_KINDS:
        raise ConfigError(f"{where}.kind 必須是 {' 或 '.join(BACKEND_KINDS)}")
    allowed = {"kind", "base_url", "api_key"} if kind == "openai" else {"kind", "base_url"}
    extra = obj.keys() - allowed
    if extra:
        raise ConfigError(f"{where} 有不認得的欄位：{', '.join(sorted(extra))}")
    if "base_url" not in obj:
        raise ConfigError(f"{where} 缺少 base_url")
    api_key = obj.get("api_key", "")
    if not isinstance(api_key, str):
        raise ConfigError(f"{where}.api_key 必須是字串")
    return BackendConfig(kind=kind, base_url=check_backend_url(obj["base_url"]), api_key=api_key)


def _parse_pinned(value: object) -> frozenset[bytes]:
    if not isinstance(value, list):
        raise ConfigError("pinned_clients 必須是陣列")
    keys = set()
    for item in value:
        try:
            raw = b64u_decode(item)
        except ProtocolError:
            raw = b""
        if len(raw) != 32:
            raise ConfigError(f"pinned_clients 的項目必須是 32 位元組 Ed25519 公鑰的 b64u：{item!r}")
        keys.add(raw)
    return frozenset(keys)


def parse_config(obj: object) -> WorkerConfig:
    """檢查並轉換設定物件。任何問題都丟 ConfigError。"""
    if not isinstance(obj, dict):
        raise ConfigError("設定檔必須是 JSON 物件")
    extra = obj.keys() - _TOP_LEVEL_KEYS
    if extra:
        raise ConfigError(f"設定檔有不認得的欄位：{', '.join(sorted(extra))}")
    if "gateway_url" not in obj:
        raise ConfigError("設定檔缺少 gateway_url")
    gateway_url = check_gateway_url(obj["gateway_url"])

    backends = obj.get("backends")
    if not isinstance(backends, list) or not backends:
        raise ConfigError("backends 必須是非空陣列，至少要設定一個後端")
    parsed_backends = tuple(_parse_backend(item, i) for i, item in enumerate(backends))

    expose = obj.get("expose_models", [])
    if not isinstance(expose, list) or not all(isinstance(m, str) and 0 < len(m) <= MAX_MODEL_LEN for m in expose):
        raise ConfigError(f"expose_models 必須是模型名稱（1 到 {MAX_MODEL_LEN} 字元）的陣列")

    concurrency = obj.get("max_concurrency", 2)
    if type(concurrency) is not int or concurrency < 1:
        raise ConfigError("max_concurrency 必須是至少為 1 的整數")

    timeout = obj.get("request_timeout", REQUEST_TIMEOUT_SECONDS)
    if type(timeout) not in (int, float) or not timeout > 0:
        raise ConfigError("request_timeout 必須是大於 0 的秒數")

    return WorkerConfig(
        gateway_url=gateway_url,
        backends=parsed_backends,
        expose_models=frozenset(expose),
        max_concurrency=concurrency,
        request_timeout=float(timeout),
        pinned_clients=_parse_pinned(obj.get("pinned_clients", [])),
    )


def load_config(path: Path) -> WorkerConfig:
    if not path.exists():
        raise ConfigError(f"找不到設定檔 {path}；請先執行 llmop worker init")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"無法讀取設定檔 {path}：{exc}") from exc
    return parse_config(obj)
