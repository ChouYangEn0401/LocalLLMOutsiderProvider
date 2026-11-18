"""閘道設定：$LLMOP_HOME/gateway/config.json，所有欄位都可省略。"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, fields
from pathlib import Path

from llmop import paths
from llmop.protocol import MAX_INFLIGHT, REQUEST_TIMEOUT_SECONDS, SYNC_WAIT_SECONDS

CONFIG_NAME = "config.json"


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    admin_local_only: bool = True
    sync_wait_seconds: float = SYNC_WAIT_SECONDS
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    max_inflight: int = MAX_INFLIGHT

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not is_loopback(self.host):
            raise ValueError(f"host 必須是本機回送位址（例如 127.0.0.1），目前是 {self.host!r}")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port 必須是 1 到 65535 的整數")
        if type(self.admin_local_only) is not bool:
            raise ValueError("admin_local_only 必須是 true 或 false")
        for name in ("sync_wait_seconds", "request_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not value > 0:
                raise ValueError(f"{name} 必須是大於 0 的數字")
        if type(self.max_inflight) is not int or self.max_inflight < 1:
            raise ValueError("max_inflight 必須是至少為 1 的整數")

    @classmethod
    def from_dict(cls, data: dict) -> GatewayConfig:
        known = {f.name for f in fields(cls)}
        unknown = data.keys() - known
        if unknown:
            raise ValueError(f"設定檔有不認得的欄位：{', '.join(sorted(unknown))}")
        return cls(**data)


def is_loopback(host: str) -> bool:
    """host 是否為回送位址（127.0.0.0/8、::1 或 localhost）。"""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def config_path() -> Path:
    return paths.home("gateway") / CONFIG_NAME


def load_config() -> GatewayConfig:
    """讀取設定檔；檔案不存在就全部用預設值。格式錯誤丟 ValueError。"""
    path = config_path()
    if not path.exists():
        return GatewayConfig()
    return GatewayConfig.from_dict(paths.read_json(path))
