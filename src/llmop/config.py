"""Configuration: one YAML file (default ``config.yaml``) holds the server settings,
the LLM backends and the accounts with their quotas.

The file is re-read automatically when its mtime changes (see :class:`ConfigHolder`),
so editing an account's quota or adding an account needs no restart.

Secrets may be written as ``env:NAME`` to read them from an environment variable.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BACKEND_TYPES = ("ollama", "vllm", "lmstudio", "llamacpp", "openai")
DEFAULT_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "vllm": "http://127.0.0.1:8000",
    "lmstudio": "http://127.0.0.1:1234",
    "llamacpp": "http://127.0.0.1:8080",
    "openai": "http://127.0.0.1:8000",
}
ROLES = ("user", "admin")


class ConfigError(ValueError):
    """Raised for an invalid configuration file; the message is shown to the operator."""


def resolve_secret(value: str | None) -> str:
    if not value:
        return ""
    value = str(value)
    if value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


@dataclass
class Quota:
    """Per-account limits. ``0`` (or ``None``) means unlimited."""

    tokens_per_day: int = 0
    tokens_per_month: int = 0
    requests_per_minute: int = 0
    requests_per_day: int = 0
    max_concurrent: int = 2
    max_tokens_per_request: int = 0

    @classmethod
    def from_dict(cls, data: dict | None, base: "Quota | None" = None) -> "Quota":
        merged = dict(vars(base)) if base else {}
        for key, val in (data or {}).items():
            if key not in cls.__dataclass_fields__:
                raise ConfigError(f"unknown quota key: {key!r}")
            merged[key] = int(val or 0)
        return cls(**merged)


@dataclass
class Account:
    username: str
    password_hash: str
    role: str = "user"
    display_name: str = ""
    disabled: bool = False
    models: list[str] = field(default_factory=lambda: ["*"])
    max_api_keys: int = 5
    quota: Quota = field(default_factory=Quota)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def may_use_model(self, model_id: str) -> bool:
        return model_allowed(model_id, self.models)


def model_allowed(model_id: str, patterns: list[str]) -> bool:
    """``*`` matches everything; ``prefix*`` matches by prefix; otherwise exact
    (``:latest`` is ignored on both sides, Ollama style)."""
    norm = model_id.removesuffix(":latest")
    for pat in patterns:
        if pat == "*":
            return True
        if pat.endswith("*") and model_id.startswith(pat[:-1]):
            return True
        if pat.removesuffix(":latest") == norm:
            return True
    return False


@dataclass
class Backend:
    name: str
    type: str
    base_url: str
    api_key: str = ""
    models: list[str] = field(default_factory=lambda: ["*"])
    timeout: float = 600.0  # idle (no progress) timeout in seconds
    connect_timeout: float = 10.0
    max_concurrency: int = 4  # simultaneous requests sent to this backend
    # Ollama only: send options.num_ctx (0 = leave server default).
    context_size: int = 0
    # Extra JSON merged into every upstream request body (e.g. {"keep_alive": "30m"}).
    extra_body: dict = field(default_factory=dict)

    @property
    def root_url(self) -> str:
        """Base URL without a trailing ``/`` or ``/v1`` (users often paste ``.../v1``)."""
        url = self.base_url.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        return url


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8780
    data_dir: str = "./data"
    # Fixed public URL (e.g. a named tunnel). Empty = use the quick-tunnel URL if any.
    public_url: str = ""
    session_minutes: int = 720
    # Trust X-Forwarded-For / CF-Connecting-IP only when the TCP peer is loopback
    # (cloudflared or a reverse proxy on the same machine / compose network).
    trusted_proxies: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    # Allow API keys / login on plain http (True for local testing; behind a tunnel
    # the public side is https anyway).
    cookie_secure: str = "auto"  # auto | always | never
    title: str = "Local LLM Outsider Provider"
    # Seconds between ": keepalive" SSE comments while waiting for the first token.
    sse_keepalive: float = 15.0
    # Log every request (without bodies) to the console.
    access_log: bool = True


@dataclass
class TunnelSettings:
    enabled: bool = False
    mode: str = "quick"  # quick | named | off
    cloudflared: str = "cloudflared"
    # For mode=named: the tunnel token from the Cloudflare dashboard (env:NAME recommended)
    token: str = ""
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    server: ServerSettings
    backends: list[Backend]
    accounts: dict[str, Account]
    default_quota: Quota
    tunnel: TunnelSettings
    model_aliases: dict[str, str]
    path: Path | None = None

    @property
    def data_path(self) -> Path:
        p = Path(self.server.data_dir)
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent / p
        return p

    def backend(self, name: str) -> Backend | None:
        return next((b for b in self.backends if b.name == name), None)

    def account(self, username: str) -> Account | None:
        return self.accounts.get(username)


def _as_list(value: Any, what: str) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ConfigError(f"{what} must be a list")
    return value


def parse_config(raw: dict | None, path: Path | None = None) -> Config:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    srv_raw = dict(raw.get("server") or {})
    unknown = set(srv_raw) - set(ServerSettings.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown server keys: {sorted(unknown)}")
    server = ServerSettings(**srv_raw)
    if server.cookie_secure not in ("auto", "always", "never"):
        raise ConfigError("server.cookie_secure must be auto | always | never")

    backends: list[Backend] = []
    for i, b in enumerate(_as_list(raw.get("backends"), "backends")):
        if not isinstance(b, dict):
            raise ConfigError(f"backends[{i}] must be a mapping")
        b = dict(b)
        btype = str(b.get("type", "ollama")).lower()
        if btype not in BACKEND_TYPES:
            raise ConfigError(f"backends[{i}].type must be one of {BACKEND_TYPES}")
        b["type"] = btype
        b.setdefault("name", btype)
        b.setdefault("base_url", DEFAULT_URLS[btype])
        b["api_key"] = resolve_secret(b.get("api_key"))
        b["models"] = [str(m) for m in _as_list(b.get("models", ["*"]), "backend models")]
        unknown = set(b) - set(Backend.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"backends[{i}] unknown keys: {sorted(unknown)}")
        backends.append(Backend(**b))
    names = [b.name for b in backends]
    if len(names) != len(set(names)):
        raise ConfigError("backend names must be unique")

    default_quota = Quota.from_dict(raw.get("default_quota"))

    accounts: dict[str, Account] = {}
    acc_raw = raw.get("accounts") or {}
    if not isinstance(acc_raw, dict):
        raise ConfigError("accounts must be a mapping of username -> settings")
    for username, a in acc_raw.items():
        username = str(username)
        if not username or any(c in username for c in " /\\:"):
            raise ConfigError(f"invalid username: {username!r}")
        a = dict(a or {})
        if not a.get("password_hash"):
            raise ConfigError(
                f"account {username!r} has no password_hash (run: llmop hash-password)"
            )
        role = a.get("role", "user")
        if role not in ROLES:
            raise ConfigError(f"account {username!r}: role must be one of {ROLES}")
        quota = Quota.from_dict(a.pop("quota", None), base=default_quota)
        a["models"] = [str(m) for m in _as_list(a.get("models", ["*"]), "account models")]
        unknown = set(a) - set(Account.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"account {username!r} unknown keys: {sorted(unknown)}")
        accounts[username] = Account(username=username, quota=quota, **a)

    tun_raw = dict(raw.get("tunnel") or {})
    unknown = set(tun_raw) - set(TunnelSettings.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown tunnel keys: {sorted(unknown)}")
    tunnel = TunnelSettings(**tun_raw)
    tunnel.token = resolve_secret(tunnel.token)
    if tunnel.mode not in ("quick", "named", "off"):
        raise ConfigError("tunnel.mode must be quick | named | off")

    aliases = {str(k): str(v) for k, v in (raw.get("model_aliases") or {}).items()}

    return Config(
        server=server,
        backends=backends,
        accounts=accounts,
        default_quota=default_quota,
        tunnel=tunnel,
        model_aliases=aliases,
        path=path,
    )


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p} (copy config.example.yaml to start)")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse_config(raw, path=p.resolve())


class ConfigHolder:
    """Holds the current Config and reloads it when the file changes on disk.

    A broken edit keeps the last good config (and remembers the error so the admin
    page can show it) instead of taking the service down.
    """

    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()
        self._mtime = self._stat()
        self.last_error: str | None = None

    def _stat(self) -> float | None:
        if self._config.path is None:
            return None
        try:
            return self._config.path.stat().st_mtime
        except OSError:
            return None

    def get(self) -> Config:
        if self._config.path is None:
            return self._config
        mtime = self._stat()
        if mtime is not None and mtime != self._mtime:
            with self._lock:
                if mtime != self._mtime:
                    self._mtime = mtime
                    try:
                        self._config = load_config(self._config.path)
                        self.last_error = None
                    except (ConfigError, yaml.YAMLError, OSError, TypeError) as exc:
                        self.last_error = str(exc)
        return self._config

    def set(self, config: Config) -> None:
        with self._lock:
            self._config = config
            self._mtime = self._stat()
