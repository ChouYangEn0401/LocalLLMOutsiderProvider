"""SDK 的本機設定檔：每個設定檔一個資料夾 ``$LLMOP_HOME/client/<名稱>/``。

    client/<名稱>/config.json       API 連結、API 金鑰、傳輸方式、釘選的 worker 公鑰、帳號網站網址與帳號名
    client/<名稱>/signing_key.json  Ed25519 簽章私鑰（只有加密設定檔才有；永遠不離開這台電腦）

兩個檔案與資料夾都收緊成只有本人（見 perms.py）。

舊版（v0.2）的單一設定檔 ``client/config.json`` 當成名稱 ``default`` 的設定檔照讀（不搬檔）；
對它的修改（set-link、pin…）寫回同一個檔、只用舊格式的欄位，舊的 ``llmop client proxy`` 照樣讀得懂。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import SdkError
from .perms import restrict

CONFIG_NAME = "config.json"
SIGNING_KEY_NAME = "signing_key.json"
LEGACY_NAME = "default"
SCHEMA_VERSION = 1
TRANSPORTS = ("auto", "e2e", "plain")
API_KEY_PREFIXES = ("sk-llop-", "llmop_sk_")
DEFAULT_MAX_TOKENS = 4096
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def llmop_home(home: str | os.PathLike | None = None) -> Path:
    """$LLMOP_HOME（預設 ~/.llmop）。不建立資料夾。"""
    if home is not None:
        return Path(home).expanduser()
    return Path(os.environ.get("LLMOP_HOME") or "~/.llmop").expanduser()


def client_root(home: str | os.PathLike | None = None) -> Path:
    return llmop_home(home) / "client"


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise SdkError("bad_request", "設定檔名稱只能用英數字、底線、連字號（1 到 64 字，英數字開頭）",
                       kind_hint="bad_request")
    return name


def _is_loopback(host: str | None) -> bool:
    import ipaddress

    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_url(url: str, what: str = "網址") -> str:
    """https://，或連到本機 loopback 的 http://；不准帶 query／fragment。回傳去掉結尾 / 的網址。"""
    if not isinstance(url, str) or not url.strip():
        raise SdkError("bad_request", f"{what}不能是空的")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        raise SdkError("bad_request", f"{what}格式不正確") from None
    if not parts.hostname or parts.query or parts.fragment or parts.username or parts.password:
        raise SdkError("bad_request", f"{what}必須是 https://主機[/路徑] 形式")
    if parts.scheme == "https" or (parts.scheme == "http" and _is_loopback(parts.hostname)):
        return url.rstrip("/")
    raise SdkError("bad_request", f"{what}必須是 https://（只有連到本機 127.0.0.1 時才可以用 http://）")


def check_api_link(url: str, what: str = "API 連結") -> str:
    """API 連結：check_url ＋ 去掉結尾的 /v1。OpenAI 相容工具習慣把連結寫成 `…/<秘密路徑>/v1`，
    SDK 自己會接 /v1/…；不去掉的話會變成 /v1/v1/models（真機實測踩到）。"""
    link = check_url(url, what)
    while link.lower().endswith("/v1"):
        link = link[:-3].rstrip("/")
    if not urlsplit(link).hostname:
        raise SdkError("bad_request", f"{what}格式不正確")
    return link


def check_api_key(key: str) -> str:
    key = (key or "").strip()
    if not key.startswith(API_KEY_PREFIXES) or len(key) > 512 or any(c.isspace() for c in key):
        raise SdkError("bad_request", f"API key 必須以 {' 或 '.join(API_KEY_PREFIXES)} 開頭", kind_hint="bad_request")
    return key


def mask_link(url: str) -> str:
    """顯示用：只留主機，秘密路徑換成 …（秘密路徑跟 API key 一樣不印出來）。"""
    if not url:
        return ""
    parts = urlsplit(url)
    path = "/…" if parts.path.strip("/") else ""
    return f"{parts.scheme}://{parts.netloc}{path}"


def mask_key(key: str) -> str:
    return f"{key[:8]}…{key[-4:]}" if key and len(key) > 16 else ("…" if key else "")


@dataclass(frozen=True)
class PinnedWorker:
    key_id: str  # 22 字元指紋
    public_key: str  # X25519 公鑰 b64u


@dataclass(frozen=True)
class Profile:
    """一個設定檔的內容。秘密欄位（api_key）不出現在 repr。"""

    name: str
    api_link: str = ""
    api_key: str = field(default="", repr=False)
    transport: str = "auto"
    encryption: str = ""  # required | optional | ""（不知道，例如 add-plain）
    account_url: str = ""
    username: str = ""
    key_id: str = ""  # 伺服器上這把 API key 的紀錄 id（撤銷用）
    pinned_workers: tuple[PinnedWorker, ...] = ()
    default_max_tokens: int = DEFAULT_MAX_TOKENS
    status: str = "ready"  # ready | incomplete | revoked
    created_at: float = 0.0
    legacy: bool = False
    directory: Path | None = field(default=None, repr=False, compare=False)

    @property
    def config_path(self) -> Path:
        assert self.directory is not None
        return self.directory / CONFIG_NAME

    @property
    def signing_key_path(self) -> Path:
        assert self.directory is not None
        return self.directory / SIGNING_KEY_NAME

    @property
    def has_signing_key(self) -> bool:
        return self.directory is not None and self.signing_key_path.exists()

    def resolved_transport(self) -> str:
        """auto：有簽章金鑰而且有釘選的 worker 就走 e2e，否則 plain。"""
        if self.transport != "auto":
            return self.transport
        return "e2e" if self.has_signing_key and self.pinned_workers else "plain"

    def to_json(self) -> dict[str, Any]:
        if self.legacy:
            # 舊格式：只寫 v0.2 ClientConfig 認得的欄位。
            return {
                "gateway_url": self.api_link,
                "api_key": self.api_key,
                "pinned_workers": [p.public_key for p in self.pinned_workers],
                "default_max_tokens": min(self.default_max_tokens, 1_000_000),
            }
        return {
            "version": SCHEMA_VERSION,
            "api_link": self.api_link,
            "api_key": self.api_key,
            "transport": self.transport,
            "encryption": self.encryption,
            "account_url": self.account_url,
            "username": self.username,
            "key_id": self.key_id,
            "pinned_workers": [{"key_id": p.key_id, "public_key": p.public_key} for p in self.pinned_workers],
            "default_max_tokens": self.default_max_tokens,
            "status": self.status,
            "created_at": self.created_at,
        }


def _worker_key_id(public_key: str) -> str:
    from llmop import crypto
    from llmop.protocol import b64u_decode

    return crypto.worker_key_id(b64u_decode(public_key))


def _from_json(name: str, data: dict, directory: Path, *, legacy: bool) -> Profile:
    if legacy:
        pins = []
        for pub in data.get("pinned_workers") or []:
            try:
                pins.append(PinnedWorker(_worker_key_id(pub), pub))
            except Exception:
                continue
        return Profile(
            name=name, api_link=str(data.get("gateway_url", "")).rstrip("/"), api_key=str(data.get("api_key", "")),
            transport="auto", encryption="", pinned_workers=tuple(pins),
            default_max_tokens=int(data.get("default_max_tokens") or 1024), status="ready", legacy=True,
            directory=directory,
        )
    pins = tuple(
        PinnedWorker(str(p["key_id"]), str(p["public_key"]))
        for p in data.get("pinned_workers") or []
        if isinstance(p, dict) and "key_id" in p and "public_key" in p
    )
    transport = data.get("transport", "auto")
    return Profile(
        name=name,
        api_link=str(data.get("api_link", "")),
        api_key=str(data.get("api_key", "")),
        transport=transport if transport in TRANSPORTS else "auto",
        encryption=str(data.get("encryption", "")),
        account_url=str(data.get("account_url", "")),
        username=str(data.get("username", "")),
        key_id=str(data.get("key_id", "")),
        pinned_workers=pins,
        default_max_tokens=int(data.get("default_max_tokens") or DEFAULT_MAX_TOKENS),
        status=str(data.get("status", "ready")),
        created_at=float(data.get("created_at") or 0.0),
        legacy=False,
        directory=directory,
    )


def _legacy_dir(home: str | os.PathLike | None) -> Path | None:
    root = client_root(home)
    if (root / CONFIG_NAME).exists() and not (root / LEGACY_NAME).is_dir():
        return root
    return None


def list_profiles(*, home: str | os.PathLike | None = None) -> list[str]:
    """已存在的設定檔名稱（排序）。舊版的 client/config.json 算成 default。"""
    root = client_root(home)
    names = set()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and _NAME.match(child.name) and (child / CONFIG_NAME).exists():
                names.add(child.name)
    if _legacy_dir(home) is not None:
        names.add(LEGACY_NAME)
    return sorted(names)


def profile_dir(name: str, *, home: str | os.PathLike | None = None) -> Path:
    check_name(name)
    if name == LEGACY_NAME and (legacy := _legacy_dir(home)) is not None:
        return legacy
    return client_root(home) / name


def exists(name: str, *, home: str | os.PathLike | None = None) -> bool:
    return name in list_profiles(home=home)


def load(name: str = "default", *, home: str | os.PathLike | None = None) -> Profile:
    from llmop import paths

    check_name(name)
    legacy_dir = _legacy_dir(home) if name == LEGACY_NAME else None
    directory = legacy_dir or client_root(home) / name
    path = directory / CONFIG_NAME
    if not path.exists():
        raise SdkError("profile_not_found",
                       f"找不到設定檔「{name}」。先執行 llmop client enroll 或 llmop client add-plain")
    try:
        data = paths.read_json(path)
        return _from_json(name, data, directory, legacy=legacy_dir is not None)
    except (ValueError, TypeError, KeyError) as exc:
        raise SdkError("bad_request", f"設定檔「{name}」的內容不正確（{type(exc).__name__}）") from None


def ensure_dir(directory: Path) -> None:
    """建立設定檔資料夾（與上層），並收緊成只有本人。"""
    root = directory.parent
    for d in (root.parent, root, directory):
        if not d.exists():
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            restrict(d)


def save(profile: Profile) -> Profile:
    """原子寫入 config.json 並收緊權限。回傳同一個 profile。"""
    from llmop import paths

    assert profile.directory is not None
    ensure_dir(profile.directory)
    paths.write_json(profile.config_path, profile.to_json(), secret=True)
    restrict(profile.config_path)
    return profile


def new_profile(name: str, *, home: str | os.PathLike | None = None, **fields: Any) -> Profile:
    check_name(name)
    directory = client_root(home) / name
    return Profile(name=name, directory=directory, created_at=time.time(), **fields)


def update(profile: Profile, **changes: Any) -> Profile:
    return save(replace(profile, **changes))


def delete(profile: Profile) -> list[str]:
    """刪掉設定檔（config.json、signing_key.json；新格式連資料夾一起刪）。回傳刪掉的檔名。"""
    assert profile.directory is not None
    removed = []
    if profile.legacy:
        for p in (profile.config_path, profile.signing_key_path):
            if p.exists():
                p.unlink()
                removed.append(p.name)
        return removed
    if profile.directory.is_dir():
        removed = sorted(p.name for p in profile.directory.iterdir())
        shutil.rmtree(profile.directory)
    return removed
