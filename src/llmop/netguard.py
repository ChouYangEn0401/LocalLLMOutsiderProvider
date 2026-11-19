"""網路守門：判斷「本機／外網」的唯一實作，以及秘密路徑（gate）與 log 清理。

全專案只有這裡判斷一個請求是不是「坐在這台電腦前面」發出的。要點：

* cloudflared 也是從 127.0.0.1 連進來的，所以**不能只看對端 IP**。它一定會加上
  轉送標頭（``cf-connecting-ip`` 等），外面的人拿不掉；有任何一個就是外網。
* ``Host`` 必須是 loopback 名稱加上這個入口的 port（擋 DNS rebinding）。
* 有 ``Origin`` 時必須是同一個本機來源；``Sec-Fetch-Site: cross-site`` 一律拒絕。
* 對端 IP 取 ASGI 的 ``scope["client"]``；本機入口的 uvicorn 必須關閉
  ``proxy_headers``，否則對端會被轉送標頭改寫。

已知限制：同一台電腦上的其他程式或其他 Windows 帳號也算「本機」。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

__all__ = [
    "FORWARD_HEADERS",
    "LOOPBACK_NAMES",
    "GateMiddleware",
    "GateState",
    "check_public_host",
    "clean_log",
    "gate_hash",
    "is_local",
    "redact_path",
]

#: 出現任何一個就代表請求經過代理（cloudflared、反向代理），一律視為外網。
FORWARD_HEADERS: tuple[str, ...] = (
    "cf-connecting-ip",
    "cf-ray",
    "cf-visitor",
    "cdn-loop",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "forwarded",
    "via",
)

#: ``Host`` 白名單裡允許的主機名（再加上入口的 port）。
LOOPBACK_NAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_FORWARD_BYTES = frozenset(h.encode("latin-1") for h in FORWARD_HEADERS)
_DEFAULT_PORTS = {"http": 80, "https": 443}


# --------------------------------------------------------------------------- 小工具


def _scope(request: Any) -> dict:
    scope = getattr(request, "scope", request)
    if not isinstance(scope, dict):
        raise TypeError("需要 Starlette 的 Request／WebSocket 或 ASGI scope")
    return scope


def _header_values(scope: dict, name: bytes) -> list[str]:
    """回傳某個標頭的所有值（同名標頭可能出現多次）。"""
    return [v.decode("latin-1") for k, v in scope.get("headers") or () if k.lower() == name]


def _single_header(scope: dict, name: bytes) -> str | None:
    """標頭不存在 → None；出現多次 → 丟 ValueError（不猜要信哪一個）。"""
    values = _header_values(scope, name)
    if not values:
        return None
    if len(values) > 1:
        raise ValueError("duplicate header")
    return values[0]


def _split_host(value: str) -> tuple[str, int | None] | None:
    """把 ``Host`` 標頭拆成（小寫、去尾點的主機名, port）。格式不對回 None。"""
    value = value.strip()
    if not value or any(c in value for c in "/\\@?#, \t") or any(ord(c) < 0x21 or ord(c) > 0x7E for c in value):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host, rest = value[1:end], value[end + 1:]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
    else:
        if value.count(":") > 1:
            return None  # 沒有中括號的 IPv6 不是合法的 Host
        host, sep, port_text = value.partition(":")
        rest = sep + port_text
    port: int | None = None
    if rest:
        if not rest.startswith(":") or not rest[1:].isdigit() or not rest[1:].isascii():
            return None
        port = int(rest[1:])
        if not 0 < port < 65536:
            return None
    host = host.lower().rstrip(".")
    if not host:
        return None
    return host, port


def _hostname_of(entry: str) -> str | None:
    """設定值可以是網址（``https://x.trycloudflare.com``）或主機名（可帶 port）。"""
    entry = (entry or "").strip()
    if not entry:
        return None
    if "://" in entry:
        try:
            netloc = urlsplit(entry).netloc
        except ValueError:
            return None
        entry = netloc.rpartition("@")[2]
    hp = _split_host(entry)
    return hp[0] if hp else None


def _is_loopback_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


# --------------------------------------------------------------------------- 本機判斷


def is_local(request: Any, *, port: int | None = None) -> bool:
    """這個請求是不是從這台電腦、直接（不經 tunnel／代理）連到本機入口。

    以下全部成立才回 True：

    1. TCP 對端（``scope["client"]``）是 loopback 位址。
    2. 沒有任何 :data:`FORWARD_HEADERS`。
    3. 只有一個 ``Host``，主機名是 ``127.0.0.1``／``localhost``／``[::1]``，
       且 port 等於這個入口的 port（``port`` 參數，沒給就用 ``scope["server"]`` 的 port）。
    4. 有 ``Origin`` 時，必須是 ``http://`` 加上與 ``Host`` 相同的主機名與 port。
    5. ``Sec-Fetch-Site`` 不是 ``cross-site``。

    ``request`` 可以是 Starlette 的 ``Request``、``WebSocket`` 或 ASGI scope。
    """
    scope = _scope(request)

    client = scope.get("client")
    if not client or not _is_loopback_ip(str(client[0])):
        return False

    for key, _ in scope.get("headers") or ():
        if key.lower() in _FORWARD_BYTES:
            return False

    expected_port = port
    if expected_port is None:
        server = scope.get("server")
        expected_port = server[1] if server and len(server) > 1 else None
    if not expected_port:
        return False

    try:
        host_value = _single_header(scope, b"host")
        origin = _single_header(scope, b"origin")
        fetch_site = _single_header(scope, b"sec-fetch-site")
    except ValueError:
        return False
    if host_value is None:
        return False
    hp = _split_host(host_value)
    if hp is None or hp[0] not in LOOPBACK_NAMES or hp[1] != expected_port:
        return False

    if fetch_site is not None and fetch_site.strip().lower() == "cross-site":
        return False

    if origin is not None:
        try:
            o = urlsplit(origin.strip())
            o_host, o_port = o.hostname, o.port
        except ValueError:
            return False
        if (o.scheme or "").lower() != "http" or not o_host:
            return False  # 包括 "null"
        if o.username or o.password or o.path not in ("", "/") or o.query or o.fragment:
            return False
        if o_host.rstrip(".") != hp[0] or (o_port or _DEFAULT_PORTS["http"]) != hp[1]:
            return False

    return True


def check_public_host(request: Any, allowed: Iterable[str]) -> bool:
    """公開入口的 ``Host`` 檢查（不符時呼叫端回 421）。

    ``allowed`` 是 ``public_url``、目前 tunnel 網址或主機名（可混用；空字串略過）。
    loopback 名稱一律允許（cloudflared 自己的健康檢查、本機測試）。
    只比主機名，不比 port；``Host`` 有多個、格式錯 → False。若有
    ``X-Forwarded-Host``，它也必須在允許清單內。
    """
    scope = _scope(request)
    names = set(LOOPBACK_NAMES)
    for entry in allowed:
        name = _hostname_of(entry)
        if name:
            names.add(name)
    try:
        host_value = _single_header(scope, b"host")
    except ValueError:
        return False
    if host_value is None:
        return False
    hp = _split_host(host_value)
    if hp is None or hp[0] not in names:
        return False
    for xfh in _header_values(scope, b"x-forwarded-host"):
        for part in xfh.split(","):
            xp = _split_host(part)
            if xp is None or xp[0] not in names:
                return False
    return True


# --------------------------------------------------------------------------- 秘密路徑（gate）


def gate_hash(gate: str) -> str:
    """gate 的 sha256 十六進位（資料庫只存這個）。"""
    return hashlib.sha256(gate.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GateState:
    """目前可用的 gate：現行一個，加上寬限期內的上一個。全部只存雜湊。

    ``grace_until`` 是 epoch 秒；``now < grace_until`` 時上一個 gate 仍可用。
    ``current_hash`` 為空字串時一律不通。
    """

    current_hash: str
    previous_hash: str | None = None
    grace_until: float = 0.0

    def allows(self, gate: str, now: float) -> bool:
        if not gate:
            return False
        digest = gate_hash(gate)
        ok = bool(self.current_hash) and hmac.compare_digest(digest, self.current_hash)
        if self.previous_hash and now < self.grace_until:
            ok = hmac.compare_digest(digest, self.previous_hash) or ok
        return ok


class GateMiddleware:
    """ASGI middleware：路徑第一段必須是有效的 gate，否則 404（在任何驗證之前）。

    通過時把 ``/<gate>`` 從 ``scope["path"]`` 與 ``scope["raw_path"]`` 去掉後交給下游
    （``/<gate>`` → ``/``）。``gates`` 是每個請求都會呼叫一次的函式，所以更換 gate
    不必重建 app。``lifespan`` 直接放行。
    """

    def __init__(self, app: Callable, gates: Callable[[], GateState | None], *,
                 clock: Callable[[], float] = time.time) -> None:
        self.app = app
        self.gates = gates
        self.clock = clock

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        gate, rest = _split_gate(path)
        state = self.gates()
        if gate is None or state is None or not state.allows(gate, self.clock()):
            await _not_found(scope, receive, send)
            return
        new_scope = dict(scope)
        new_scope["path"] = rest
        raw = scope.get("raw_path")
        if isinstance(raw, (bytes, bytearray)):
            raw_gate, raw_rest = _split_raw(bytes(raw))
            new_scope["raw_path"] = raw_rest if raw_gate is not None else rest.encode("utf-8")
        await self.app(new_scope, receive, send)


def _split_gate(path: str) -> tuple[str | None, str]:
    if not path.startswith("/"):
        return None, path
    seg, sep, rest = path[1:].partition("/")
    if not seg:
        return None, path
    return seg, "/" + rest if sep else "/"


def _split_raw(raw: bytes) -> tuple[bytes | None, bytes]:
    raw = raw.split(b"?", 1)[0]
    if not raw.startswith(b"/"):
        return None, raw
    seg, sep, rest = raw[1:].partition(b"/")
    if not seg:
        return None, raw
    return seg, b"/" + rest if sep else b"/"


async def _not_found(scope: dict, receive: Callable, send: Callable) -> None:
    body = b"Not Found"
    headers = [(b"content-type", b"text/plain; charset=utf-8"),
               (b"content-length", str(len(body)).encode())]
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 404, "headers": headers})
        await send({"type": "http.response.body", "body": body})
        return
    # websocket：伺服器支援的話回 HTTP 404，否則在握手前關閉（uvicorn 會回 403）。
    if "websocket.http.response" in (scope.get("extensions") or {}):
        await send({"type": "websocket.http.response.start", "status": 404, "headers": headers})
        await send({"type": "websocket.http.response.body", "body": body})
    else:
        await send({"type": "websocket.close", "code": 1008})


# --------------------------------------------------------------------------- log 清理

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9][A-Za-z0-9_-]{7,})|((?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,})")


def _escape_char(c: str) -> str:
    if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn") and c != " ":
        o = ord(c)
        return f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}" if o < 0x10000 else f"\\U{o:08x}"
    return c


def clean_log(value: Any, max_len: int = 200) -> str:
    """外來字串進 log 前的清理：跳脫控制字元（含換行、bidi 字元）、遮蔽看起來
    像 key／Bearer 權杖的字串、截斷到 ``max_len`` 個字元（超過時以 ``…`` 結尾）。"""
    text = value if isinstance(value, str) else str(value)
    text = _SECRET_RE.sub(lambda m: "sk-***" if m.group(1) else "Bearer ***", text)
    text = "".join(_escape_char(c) for c in text)
    if max_len > 0 and len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def redact_path(path: str) -> str:
    """給公開 API 入口的存取紀錄用：第一段（gate）換成 ``***``，查詢字串整段遮蔽，
    結果再過 :func:`clean_log`。例：``/abc123/v1/chat?x=1`` → ``/***/v1/chat?***``。"""
    path = path if isinstance(path, str) else str(path)
    base, q, _ = path.partition("?")
    if base.startswith("/"):
        seg, sep, rest = base[1:].partition("/")
        if seg:
            base = "/***" + (sep + rest)
    return clean_log(base + ("?***" if q else ""), max_len=300)
