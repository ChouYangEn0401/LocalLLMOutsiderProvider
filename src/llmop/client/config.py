"""用戶端設定（client/config.json）與簽章金鑰（client/signing_key.json）。

兩個檔案都在 $LLMOP_HOME/client/ 底下，權限 0600：config.json 有 API 金鑰，signing_key.json 有簽章私鑰。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from llmop import crypto, paths
from llmop.protocol import MAX_TOKENS_RANGE, ProtocolError, b64u_decode, b64u_encode

log = logging.getLogger("llmop.client")

ROLE = "client"
CONFIG_NAME = "config.json"
SIGNING_KEY_NAME = "signing_key.json"

DEFAULT_PROXY_PORT = 8788
DEFAULT_MAX_TOKENS = 1024
API_KEY_PREFIX = "llmop_sk_"

CONFIG_EXAMPLE = """{
  "gateway_url": "https://<名稱>.trycloudflare.com",
  "api_key": "llmop_sk_...",
  "pinned_workers": ["<worker 主控台印出的 X25519 公鑰（b64u）>"],
  "proxy_port": 8788,
  "default_max_tokens": 1024
}"""


class ConfigError(Exception):
    """設定檔或金鑰檔缺少、格式錯誤或內容不合法。訊息是給人看的繁體中文。"""


def client_home() -> Path:
    return paths.home(ROLE)


def config_path() -> Path:
    return client_home() / CONFIG_NAME


def signing_key_path() -> Path:
    return client_home() / SIGNING_KEY_NAME


def is_loopback_host(host: str | None) -> bool:
    """host 是否為 loopback：localhost、127.0.0.0/8 或 ::1。"""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _warn_if_readable_by_others(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s 的權限是 %o，建議改成 0600", path, stat.S_IMODE(mode))


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientConfig:
    gateway_url: str
    api_key: str
    pinned_workers: tuple[str, ...]
    proxy_port: int = DEFAULT_PROXY_PORT
    default_max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        _check_gateway_url(self.gateway_url)
        if not isinstance(self.api_key, str) or not self.api_key.startswith(API_KEY_PREFIX):
            raise ConfigError(f"api_key 必須是以 {API_KEY_PREFIX} 開頭的字串")
        for key in self.pinned_workers:
            try:
                ok = isinstance(key, str) and len(b64u_decode(key)) == 32
            except ProtocolError:
                ok = False
            if not ok:
                raise ConfigError("pinned_workers 的每一項都必須是 32 位元組 X25519 公鑰的 b64u（43 個字元）")
        if type(self.proxy_port) is not int or not 1 <= self.proxy_port <= 65535:
            raise ConfigError("proxy_port 必須是 1 到 65535 的整數")
        low, high = MAX_TOKENS_RANGE
        if type(self.default_max_tokens) is not int or not low <= self.default_max_tokens <= high:
            raise ConfigError(f"default_max_tokens 必須是 {low} 到 {high} 的整數")

    @property
    def base_url(self) -> str:
        return self.gateway_url.rstrip("/")

    @classmethod
    def from_dict(cls, data: dict) -> ClientConfig:
        known = {"gateway_url", "api_key", "pinned_workers", "proxy_port", "default_max_tokens"}
        unknown = data.keys() - known
        if unknown:
            raise ConfigError(f"設定檔有不認得的欄位：{', '.join(sorted(unknown))}")
        missing = {"gateway_url", "api_key", "pinned_workers"} - data.keys()
        if missing:
            raise ConfigError(f"設定檔缺少欄位：{', '.join(sorted(missing))}")
        pinned = data["pinned_workers"]
        if not isinstance(pinned, list):
            raise ConfigError("pinned_workers 必須是陣列")
        return cls(
            gateway_url=data["gateway_url"],
            api_key=data["api_key"],
            pinned_workers=tuple(pinned),
            proxy_port=data.get("proxy_port", DEFAULT_PROXY_PORT),
            default_max_tokens=data.get("default_max_tokens", DEFAULT_MAX_TOKENS),
        )


def _check_gateway_url(url: object) -> None:
    if not isinstance(url, str):
        raise ConfigError("gateway_url 必須是字串")
    parts = urlsplit(url)
    if not parts.hostname or parts.query or parts.fragment:
        raise ConfigError("gateway_url 必須是 https://主機 形式的網址")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and is_loopback_host(parts.hostname):
        return
    raise ConfigError("gateway_url 必須是 https://，只有連到本機 loopback 時才可以用 http://")


def load_config(path: str | os.PathLike | None = None) -> ClientConfig:
    """讀取並檢查 config.json。檔案不存在或內容不合法時丟 ConfigError。"""
    path = Path(path) if path is not None else config_path()
    if not path.exists():
        raise ConfigError(f"找不到設定檔 {path}。請以權限 0600 建立，內容如下：\n{CONFIG_EXAMPLE}")
    _warn_if_readable_by_others(path)
    try:
        data = paths.read_json(path)
    except ValueError as exc:
        raise ConfigError(f"{path} 不是合法的 JSON 物件") from exc
    return ClientConfig.from_dict(data)


# ---------------------------------------------------------------------------
# signing_key.json
# ---------------------------------------------------------------------------


def signing_key_info(key: Ed25519PrivateKey) -> tuple[str, str]:
    """回傳 (公鑰 b64u, sig_key_id)，這兩個值要貼到閘道的管理介面。"""
    raw = crypto.public_raw(key)
    return b64u_encode(raw), crypto.signing_key_id(raw)


def load_signing_key(path: str | os.PathLike | None = None) -> Ed25519PrivateKey:
    """讀取簽章私鑰，並確認檔案裡記錄的公鑰與私鑰相符。"""
    path = Path(path) if path is not None else signing_key_path()
    if not path.exists():
        raise ConfigError(f"找不到簽章金鑰 {path}。請先執行 llmop client keygen")
    _warn_if_readable_by_others(path)
    try:
        data = paths.read_json(path)
        key = crypto.load_signing_private(b64u_decode(data["private_key"]))
    except (ValueError, KeyError, TypeError, ProtocolError) as exc:
        raise ConfigError(f"{path} 的內容不是合法的簽章金鑰") from exc
    if data.get("public_key") != signing_key_info(key)[0]:
        raise ConfigError(f"{path} 裡的公鑰與私鑰不相符")
    return key


def ensure_signing_key(path: str | os.PathLike | None = None) -> tuple[Ed25519PrivateKey, bool]:
    """沒有簽章金鑰就建立一把。回傳 (金鑰, 是否為新建立)。已存在的金鑰絕不覆寫。"""
    path = Path(path) if path is not None else signing_key_path()
    if path.exists():
        return load_signing_key(path), False
    key = crypto.generate_signing_key()
    public_key, sig_key_id = signing_key_info(key)
    paths.write_json(
        path,
        {
            "private_key": b64u_encode(crypto.private_raw(key)),
            "public_key": public_key,
            "sig_key_id": sig_key_id,
        },
        secret=True,
    )
    return key, True
