"""Ask an LLM server which models it has (used by the local console's "LLM 後端" panel).

Not tied to the configured backends: it takes a ``type`` and a URL the admin typed,
with a short timeout, and returns plain dicts. The caller decides who may call it
(admin, local surface only).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import BACKEND_TYPES

TIMEOUT = httpx.Timeout(4.0, connect=2.5)
MAX_MODELS = 500


class ProbeError(Exception):
    """A readable reason (Traditional Chinese) why the server could not be queried."""


def normalize_url(url: object) -> str:
    """Validate and normalise a backend URL: ``http(s)://host[:port]``, no ``/v1`` tail."""
    if not isinstance(url, str) or not url.strip():
        raise ProbeError("請輸入後端網址 (base_url required).")
    url = url.strip()
    if len(url) > 300 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ProbeError("後端網址格式不正確 (invalid URL).")
    if "://" not in url:
        url = "http://" + url  # "localhost:1234" is fine
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise ProbeError("後端網址格式不正確 (invalid URL).") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ProbeError("後端網址要以 http:// 或 https:// 開頭 (must start with http:// or https://).")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ProbeError("後端網址不可含帳密、查詢字串或 # (no credentials/query/fragment).")
    out = url.rstrip("/")
    if out.endswith("/v1"):
        out = out[:-3].rstrip("/")
    return out


def check_type(btype: object) -> str:
    if not isinstance(btype, str) or btype.lower() not in BACKEND_TYPES:
        raise ProbeError("後端類型必須是：" + " / ".join(BACKEND_TYPES))
    return btype.lower()


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> Any:
    resp = await client.get(url, headers=headers)
    if resp.status_code in (401, 403):
        raise ProbeError("後端拒絕存取（需要 API key？）(HTTP %d)." % resp.status_code)
    if resp.status_code >= 400:
        raise ProbeError("後端回應 HTTP %d (%s)." % (resp.status_code, urlsplit(url).path))
    return resp.json()


async def list_models(btype: str, base_url: str, api_key: str = "") -> list[dict]:
    """``[{"id", "loaded"(bool|None), "context_length"(int|None)}]``. Raises ProbeError."""
    btype = check_type(btype)
    root = normalize_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    out: dict[str, dict] = {}

    def add(mid: Any, loaded: bool | None = None, ctx: Any = None) -> None:
        if isinstance(mid, str) and mid and len(out) < MAX_MODELS:
            row = out.setdefault(mid, {"id": mid, "loaded": None, "context_length": None})
            if loaded is not None:
                row["loaded"] = loaded
            if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
                row["context_length"] = ctx

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            if btype == "ollama":
                data = await _get(client, root + "/api/tags", headers)
                for m in (data.get("models") if isinstance(data, dict) else None) or []:
                    if isinstance(m, dict):
                        add(m.get("name") or m.get("model"))
            else:
                data = await _get(client, root + "/v1/models", headers)
                items = data.get("data") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    raise ProbeError("後端的 /v1/models 回應格式不對（這真的是 %s 嗎？）" % btype)
                for m in items:
                    if isinstance(m, dict):
                        add(m.get("id"), None, m.get("max_model_len") or m.get("context_length"))
                if btype == "lmstudio":  # native API knows which models are loaded
                    try:
                        native = await _get(client, root + "/api/v0/models", headers)
                        for m in (native.get("data") if isinstance(native, dict) else None) or []:
                            if isinstance(m, dict) and m.get("type") != "embeddings":
                                add(m.get("id"), m.get("state") == "loaded",
                                    m.get("loaded_context_length") or m.get("max_context_length"))
                    except (ProbeError, httpx.HTTPError, ValueError):
                        pass  # older LM Studio: the plain list is enough
    except ProbeError:
        raise
    except httpx.ConnectError:
        raise ProbeError("連不上 %s（伺服器沒開，或網址／埠號不對）(cannot connect)." % root) from None
    except httpx.TimeoutException:
        raise ProbeError("連線逾時：%s 沒有回應 (timeout)." % root) from None
    except (httpx.HTTPError, ValueError):
        raise ProbeError("無法讀取 %s 的回應 (bad response)." % root) from None
    return sorted(out.values(), key=lambda r: (not r["loaded"], r["id"]))
