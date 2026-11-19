"""Configuration: one YAML file (default ``config.yaml``) holds the server settings,
the LLM backends and the accounts with their quotas.

The file is re-read automatically when its mtime changes (see :class:`ConfigHolder`),
so editing an account's quota or adding an account needs no restart.

Secrets may be written as ``env:NAME`` to read them from an environment variable.

v0.2 sections (all optional, every value has a default; see ``config.example.yaml``):
``roles``, ``model``, ``usage``, ``tiers``, ``queue``, ``listeners``, ``tunnels``,
``local_account`` and ``accounts.*.tier`` (plus per-account tier overrides).
The rules behind the numbers are in ``docs/plan/TIERS_AND_USAGE_RULES.md``.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field, replace
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
NODE_ROLES = ("gateway", "worker", "client")
TIER_NAMES = ("vip3", "vip2", "vip1", "normal", "guest")
ACCOUNT_TIERS = ("vip3", "vip2", "vip1", "normal")  # GUEST is a key without an account
LOW_MODES = ("wait", "block")
ON_BUCKET_EMPTY = ("as_normal", "low")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LISTENER_NAMES = ("local", "public_ui", "public_api")


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
            try:
                merged[key] = int(val or 0)
            except (TypeError, ValueError):
                raise ConfigError(f"quota {key} must be a number, got {val!r}") from None
        return cls(**merged)


@dataclass
class Account:
    username: str
    password_hash: str
    role: str = "user"
    display_name: str = ""
    disabled: bool = False
    models: list[str] = field(default_factory=lambda: ["*"])
    max_api_keys: int = 1
    quota: Quota = field(default_factory=Quota)
    tier: str = "normal"
    # The account's effective tier limits: ``tiers[tier]`` with any per-account
    # override (e.g. ``bucket: 300000``) applied. Filled in by parse_config.
    limits: "TierSettings | None" = None

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
    # Let the public API link stream (``stream: true``). Off by default: quick
    # tunnels do not support SSE well (D21); the local /v1 always streams.
    public_stream: bool = False


@dataclass
class TunnelSettings:
    enabled: bool = False
    mode: str = "quick"  # quick | named | off
    cloudflared: str = "cloudflared"
    # For mode=named: the tunnel token from the Cloudflare dashboard (env:NAME recommended)
    token: str = ""
    extra_args: list[str] = field(default_factory=list)
    # mode=named in "tunnels.ui" / "tunnels.api": this tunnel's own https URL (its
    # hostname is accepted by the Host check and shown as the link).
    public_url: str = ""


@dataclass
class WeekReset:
    weekday: str = "sun"
    time: str = "04:00"  # HH:MM, 24h
    timezone: str = "local"  # "local" (the host's zone) or an IANA name such as Asia/Taipei


@dataclass
class UsageSettings:
    window_seconds: int = 5400  # 1.5-hour bucket: time from empty to full
    recover_at: float = 0.25  # LOW -> NORMAL when the bucket is back to this share
    debt_floor: float = 0.25  # the bucket may go down to -debt_floor x capacity
    low_mode: str = "wait"  # wait | block
    week_reset: WeekReset = field(default_factory=WeekReset)


@dataclass
class TierSettings:
    """One tier's limits. ``0`` for bucket / weekly / budget means not limited."""

    priority: int
    max_concurrent: int
    bucket: int = 0  # 1.5-hour bucket capacity (tokens)
    weekly: int = 0  # weekly limit (tokens)
    budget: int = 0  # GUEST: total per key
    reset: bool = False  # GUEST: reset the budget weekly
    on_bucket_empty: str = "low"  # as_normal (keep NORMAL's queue place) | low


def default_tiers() -> dict[str, TierSettings]:
    return {
        "vip3": TierSettings(priority=1, max_concurrent=3),
        "vip2": TierSettings(priority=2, max_concurrent=2),
        "vip1": TierSettings(priority=3, max_concurrent=2, bucket=240000, weekly=3000000,
                             on_bucket_empty="as_normal"),
        "normal": TierSettings(priority=4, max_concurrent=1, bucket=80000, weekly=1000000),
        "guest": TierSettings(priority=5, max_concurrent=1, bucket=20000, budget=50000, reset=False),
    }


@dataclass
class QueueSettings:
    slots: int = 6  # must match the backend's parallel slots and context
    max_length: int = 30
    max_per_account: int = 2
    max_wait_seconds: dict[str, float] = field(
        default_factory=lambda: {"public": 60.0, "local": 600.0})
    fair_share_every: int = 6


@dataclass
class Listeners:
    """Ports of the three entry points (all bound to 127.0.0.1)."""

    local: int = 8781  # local console + key-less /v1
    public_ui: int = 8780  # account website (tunnel #1)
    public_api: int = 8782  # /<gate>/v1, /<gate>/e2e/v1, /<gate>/worker/ws (tunnel #2)


@dataclass
class Tunnels:
    """Two independent tunnels: ``ui`` (account website) and ``api`` (API link)."""

    ui: TunnelSettings = field(default_factory=TunnelSettings)
    api: TunnelSettings = field(default_factory=TunnelSettings)


@dataclass
class Config:
    server: ServerSettings
    backends: list[Backend]
    accounts: dict[str, Account]
    default_quota: Quota
    tunnel: TunnelSettings
    model_aliases: dict[str, str]
    path: Path | None = None
    roles: list[str] = field(default_factory=lambda: ["gateway"])
    model: str = ""  # the single model every request is sent to ("" = not set)
    usage: UsageSettings = field(default_factory=UsageSettings)
    tiers: dict[str, TierSettings] = field(default_factory=default_tiers)
    queue: QueueSettings = field(default_factory=QueueSettings)
    listeners: Listeners = field(default_factory=Listeners)
    tunnels: Tunnels = field(default_factory=Tunnels)
    # Account used by the key-less local /v1. Not set: the first admin account.
    local_account: str = ""

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


def _mapping(value: Any, what: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{what} must be a mapping")
    return dict(value)


def _check_keys(data: dict, allowed, what: str) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        raise ConfigError(f"unknown {what} keys: {sorted(map(str, unknown))}")


def _int(value: Any, what: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{what} must be a whole number, got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be a whole number, got {value!r}") from None
    if number != value and not (isinstance(value, str) and str(number) == value.strip()):
        raise ConfigError(f"{what} must be a whole number, got {value!r}")
    if number < minimum:
        raise ConfigError(f"{what} must be at least {minimum}, got {number}")
    return number


def _float(value: Any, what: str, low: float, high: float | None = None) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{what} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be a number, got {value!r}") from None
    if number != number or number < low or (high is not None and number > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ConfigError(f"{what} must be {bound}, got {value!r}")
    return number


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{what} must be true or false, got {value!r}")
    return value


def _choice(value: Any, choices: tuple, what: str) -> str:
    text = str(value).lower() if value is not None else ""
    if text not in choices:
        raise ConfigError(f"{what} must be one of {' | '.join(choices)}, got {value!r}")
    return text


def _check_timezone(name: str, what: str) -> str:
    if name == "local":
        return name
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(
            f"{what} must be 'local' or a time zone name such as Asia/Taipei, got {name!r}"
            " (on Windows, IANA names need the 'tzdata' package)"
        ) from None
    return name


def _parse_roles(value: Any) -> list[str]:
    if value is None:
        return ["gateway"]
    roles = [_choice(r, NODE_ROLES, "roles") for r in _as_list(value, "roles")]
    if not roles:
        raise ConfigError(f"roles must list at least one of {NODE_ROLES}")
    if len(roles) != len(set(roles)):
        raise ConfigError("roles must not repeat")
    return roles


def _parse_usage(value: Any) -> UsageSettings:
    u = _mapping(value, "usage")
    _check_keys(u, UsageSettings.__dataclass_fields__, "usage")
    w = _mapping(u.pop("week_reset", None), "usage.week_reset")
    _check_keys(w, WeekReset.__dataclass_fields__, "usage.week_reset")
    week = WeekReset()
    if "weekday" in w:
        week.weekday = _choice(w["weekday"], WEEKDAYS, "usage.week_reset.weekday")
    if "time" in w:
        t = str(w["time"]).strip()
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ConfigError(f"usage.week_reset.time must be HH:MM (24h), got {w['time']!r}")
        week.time = f"{int(m.group(1)):02d}:{m.group(2)}"
    if "timezone" in w:
        week.timezone = _check_timezone(str(w["timezone"]), "usage.week_reset.timezone")
    out = UsageSettings(week_reset=week)
    if "window_seconds" in u:
        out.window_seconds = _int(u["window_seconds"], "usage.window_seconds", 1)
    if "recover_at" in u:
        out.recover_at = _float(u["recover_at"], "usage.recover_at", 0.0, 1.0)
    if "debt_floor" in u:
        out.debt_floor = _float(u["debt_floor"], "usage.debt_floor", 0.0, 1.0)
    if "low_mode" in u:
        out.low_mode = _choice(u["low_mode"], LOW_MODES, "usage.low_mode")
    return out


def _apply_tier_values(base: TierSettings, data: dict, what: str) -> TierSettings:
    _check_keys(data, TierSettings.__dataclass_fields__, what)
    values: dict[str, Any] = {}
    for key, val in data.items():
        name = f"{what}.{key}"
        if key in ("priority", "max_concurrent"):
            values[key] = _int(val, name, 1)
        elif key in ("bucket", "weekly", "budget"):
            values[key] = 0 if val is None else _int(val, name, 0)
        elif key == "reset":
            values[key] = _bool(val, name)
        elif key == "on_bucket_empty":
            values[key] = _choice(val, ON_BUCKET_EMPTY, name)
    return replace(base, **values)


def _parse_tiers(value: Any) -> dict[str, TierSettings]:
    t = _mapping(value, "tiers")
    _check_keys(t, TIER_NAMES, "tiers")
    tiers = default_tiers()
    for name, data in t.items():
        tiers[name] = _apply_tier_values(tiers[name], _mapping(data, f"tiers.{name}"), f"tiers.{name}")
    return tiers


def _parse_queue(value: Any) -> QueueSettings:
    q = _mapping(value, "queue")
    _check_keys(q, QueueSettings.__dataclass_fields__, "queue")
    out = QueueSettings()
    for key in ("slots", "max_length", "max_per_account", "fair_share_every"):
        if key in q:
            setattr(out, key, _int(q[key], f"queue.{key}", 1))
    if "max_wait_seconds" in q:
        mw = _mapping(q["max_wait_seconds"], "queue.max_wait_seconds")
        _check_keys(mw, ("public", "local"), "queue.max_wait_seconds")
        for key, val in mw.items():
            out.max_wait_seconds[key] = _float(val, f"queue.max_wait_seconds.{key}", 1.0)
    return out


def _parse_listeners(value: Any) -> Listeners:
    raw = _mapping(value, "listeners")
    data = {str(k).replace("-", "_"): v for k, v in raw.items()}
    _check_keys(data, LISTENER_NAMES, "listeners")
    out = Listeners()
    for key, val in data.items():
        port = _int(val, f"listeners.{key}", 1)
        if port > 65535:
            raise ConfigError(f"listeners.{key} must be a port (1-65535), got {port}")
        setattr(out, key, port)
    ports = [getattr(out, name) for name in LISTENER_NAMES]
    if len(ports) != len(set(ports)):
        raise ConfigError(f"listeners must use three different ports, got {ports}")
    return out


def _parse_tunnel(value: Any, what: str) -> TunnelSettings:
    tun_raw = _mapping(value, what)
    _check_keys(tun_raw, TunnelSettings.__dataclass_fields__, what)
    tunnel = TunnelSettings(**tun_raw)
    tunnel.token = resolve_secret(tunnel.token)
    if tunnel.mode not in ("quick", "named", "off"):
        raise ConfigError(f"{what}.mode must be quick | named | off")
    return tunnel


def _parse_tunnels(value: Any) -> Tunnels:
    t = _mapping(value, "tunnels")
    _check_keys(t, ("ui", "api"), "tunnels")
    return Tunnels(ui=_parse_tunnel(t.get("ui"), "tunnels.ui"),
                   api=_parse_tunnel(t.get("api"), "tunnels.api"))


def parse_config(raw: dict | None, path: Path | None = None) -> Config:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    srv_raw = dict(raw.get("server") or {})
    unknown = set(srv_raw) - set(ServerSettings.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown server keys: {sorted(unknown)}")
    server = ServerSettings(**srv_raw)
    try:
        server.port = int(server.port)
        server.session_minutes = int(server.session_minutes)
        server.sse_keepalive = float(server.sse_keepalive)
    except (TypeError, ValueError):
        raise ConfigError("server.port / session_minutes / sse_keepalive must be numbers") from None
    # Docker / scripts may override where data lives without editing the file.
    if os.environ.get("LLMOP_DATA_DIR"):
        server.data_dir = os.environ["LLMOP_DATA_DIR"]
    if server.cookie_secure not in ("auto", "always", "never"):
        raise ConfigError("server.cookie_secure must be auto | always | never")
    server.public_stream = _bool(server.public_stream, "server.public_stream")

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

    tiers = _parse_tiers(raw.get("tiers"))
    tier_keys = set(TierSettings.__dataclass_fields__)

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
        tier = _choice(a.pop("tier", "normal"), ACCOUNT_TIERS, f"account {username!r}: tier")
        overrides = {k: a.pop(k) for k in list(a) if k in tier_keys}
        limits = _apply_tier_values(tiers[tier], overrides, f"accounts.{username}")
        unknown = set(a) - (set(Account.__dataclass_fields__) - {"tier", "limits"})
        if unknown:
            raise ConfigError(f"account {username!r} unknown keys: {sorted(unknown)}")
        if "max_api_keys" in a:
            a["max_api_keys"] = _int(a["max_api_keys"], f"account {username!r}: max_api_keys", 0)
        accounts[username] = Account(username=username, quota=quota, tier=tier, limits=limits, **a)

    tunnel = _parse_tunnel(raw.get("tunnel"), "tunnel")  # v0.1 single tunnel (kept)

    aliases = {str(k): str(v) for k, v in (raw.get("model_aliases") or {}).items()}

    model = raw.get("model")
    if model is None:
        model = ""
    elif not isinstance(model, str):
        raise ConfigError(f"model must be a model name (text), got {model!r}")

    local_account = raw.get("local_account")
    if local_account is None or local_account == "":
        local_account = next((n for n, acc in accounts.items() if acc.is_admin), "")
    else:
        local_account = str(local_account)
        if local_account not in accounts:
            raise ConfigError(f"local_account {local_account!r} is not an account in this file")

    return Config(
        server=server,
        backends=backends,
        accounts=accounts,
        default_quota=default_quota,
        tunnel=tunnel,
        model_aliases=aliases,
        path=path,
        roles=_parse_roles(raw.get("roles")),
        model=model.strip(),
        usage=_parse_usage(raw.get("usage")),
        tiers=tiers,
        queue=_parse_queue(raw.get("queue")),
        listeners=_parse_listeners(raw.get("listeners")),
        tunnels=_parse_tunnels(raw.get("tunnels")),
        local_account=local_account,
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
                    except (ConfigError, yaml.YAMLError, OSError, TypeError, ValueError) as exc:
                        self.last_error = str(exc)
        return self._config

    def set(self, config: Config) -> None:
        with self._lock:
            self._config = config
            self._mtime = self._stat()
