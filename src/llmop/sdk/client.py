"""同步的 Client 與 enroll／add_plain，以及薄包成 async 的 AsyncClient。

HTTP 用 httpx；加密沿用 llmop.crypto（HPKE＋Ed25519）與 llmop.protocol，SDK 不自創任何演算法。
信任規則跟 v0.2 的用戶端相同：只加密給設定檔裡釘選的 worker 公鑰；閘道公布的公鑰只拿來比對，
而且一律用公鑰重新算一次 key_id（指紋）再比。

不做：e2e 串流、前向保密、自動探索新的 API 連結（連結換了 → SdkError(kind_hint="link_changed")，
使用者向服務提供者拿新連結後 set_link()）。

紀錄只寫狀態碼與耗時，不寫提示、回覆、API key、秘密路徑或任何金鑰。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from llmop import crypto
from llmop.protocol import (
    MAX_MODEL_LEN,
    MAX_TOKENS_RANGE,
    REQUEST_TIMEOUT_SECONDS,
    SYNC_WAIT_SECONDS,
    ProtocolError,
    b64u_decode,
)

from . import keys as keyfile
from . import profiles as store
from .errors import LINK_CHANGED_MESSAGE, SdkError
from .perms import check_private, restrict
from .profiles import PinnedWorker, Profile

log = logging.getLogger("llmop.sdk")

DEADLINE_SECONDS = float(REQUEST_TIMEOUT_SECONDS)
HTTP_TIMEOUT_SECONDS = SYNC_WAIT_SECONDS + 30  # 閘道長輪詢最多 80 秒，單一 HTTP 呼叫要比它長
ACCOUNT_TIMEOUT_SECONDS = 60.0
POLL_BACKOFF = (0.5, 1.0, 2.0, 4.0, 5.0)
POLL_MIN_INTERVAL = 0.5
E2E_PREFIX = "/e2e/v1"
PLAIN_PREFIX = "/v1"
_ANY_MODEL = ("", "any", "auto", "default")


# ---------------------------------------------------------------------------
# 回傳型別
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelEntry:
    id: str
    owned_by: str = ""
    key_id: str = ""  # 加密：提供這個模型的 worker 指紋；明文為 ""
    context_length: int | None = None


@dataclass(frozen=True)
class PinResult:
    key_id: str
    newly_pinned: bool
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnrollResult:
    profile: str
    transport: str  # 這個設定檔實際會走的：e2e | plain
    status: str  # ready | incomplete
    key_record_id: str  # 伺服器上這把 API key 的紀錄 id（撤銷用）
    key_display: str  # 遮蔽過的 key（只給人看）
    sig_key_id: str = ""  # 簽章公鑰的 id（加密才有）
    signing_key_created: bool = False
    pinned: tuple[str, ...] = ()  # 已釘選的 worker 指紋
    api_link_display: str = ""  # 只有主機，秘密路徑遮掉
    test_reply: str | None = None


# ---------------------------------------------------------------------------
# HTTP 與錯誤
# ---------------------------------------------------------------------------


def default_http() -> httpx.Client:
    """SDK 預設的 httpx.Client：照環境變數走 proxy（公司網路可能需要），但連到本機 loopback 一律直連。"""
    direct = httpx.HTTPTransport()
    return httpx.Client(
        trust_env=True, follow_redirects=False,
        mounts={"all://127.0.0.1": direct, "all://localhost": direct, "all://[::1]": direct},
    )


def _retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class _Transient(Exception):
    """網路層失敗或邊緣節點回的非 JSON 5xx（輪詢時可以重試）。"""

    def __init__(self, error: SdkError) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass
class _Reply:
    status: int
    data: dict | None
    headers: httpx.Headers


def _send(http: httpx.Client, method: str, url: str, *, api: bool, json_body: Any = None,
          headers: dict | None = None, timeout: float = HTTP_TIMEOUT_SECONDS) -> _Reply:
    """送一個 HTTP 請求。網路失敗、非 JSON 的 5xx、非 JSON 的 404（api=True 時＝連結已更換）都在這裡處理。"""
    try:
        resp = http.request(method, url, json=json_body, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        raise _Transient(SdkError("timeout", "連線逾時", kind_hint="timeout")) from None
    except (httpx.TransportError, OSError) as exc:
        raise _Transient(SdkError("network_error", f"連不上伺服器（{type(exc).__name__}）",
                                  kind_hint="offline")) from None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        status = resp.status_code
        if status == 524:
            raise _Transient(SdkError("timeout", "Cloudflare 524：伺服器太久沒回應（長回答請改用加密模式或縮短 max_tokens）",
                                      status=524, kind_hint="timeout"))
        if status >= 500:
            raise _Transient(SdkError("network_error", f"通道或邊緣節點回了 HTTP {status}（伺服器的通道可能沒在跑）",
                                      status=status, kind_hint="offline"))
        if status == 404 and api:
            raise SdkError("link_changed", LINK_CHANGED_MESSAGE, status=404, kind_hint="link_changed")
        if status < 400 and resp.content == b"":
            return _Reply(status, {}, resp.headers)
        raise SdkError("bad_response", f"伺服器回了無法解析的回應（HTTP {status}）", status=status,
                       kind_hint="server" if status >= 500 else "bad_request")
    return _Reply(resp.status_code, data, resp.headers)


def _error(reply: _Reply, *, default_code: str = "") -> SdkError:
    data = reply.data or {}
    err = data.get("error")
    code, message = default_code, ""
    if isinstance(err, dict):
        code = err.get("code") if isinstance(err.get("code"), str) else default_code
        message = err.get("message") if isinstance(err.get("message"), str) else ""
    elif isinstance(err, str):
        message = err
    if not code:
        code = {401: "invalid_api_key", 429: "rate_limit_exceeded"}.get(reply.status, f"http_{reply.status}")
    return SdkError(code, message or f"HTTP {reply.status}", status=reply.status,
                    retry_after=_retry_after(reply.headers))


def _call(http: httpx.Client, method: str, url: str, **kw: Any) -> _Reply:
    """一次呼叫；_Transient 轉成它帶的 SdkError（不重試）。"""
    try:
        return _send(http, method, url, **kw)
    except _Transient as exc:
        raise exc.error from None


def _queue_info(data: dict) -> dict[str, int] | None:
    queue = data.get("queue")
    if not isinstance(queue, dict):
        return None
    info: dict[str, int] = {}
    for name in ("position", "eta"):
        value = queue.get(name)
        if isinstance(value, float) and math.isfinite(value) and 0 <= value < 1e9:
            value = round(value)
        if type(value) is int and 0 <= value < 10**9:
            info[name] = value
    return info or None


# ---------------------------------------------------------------------------
# 帳號網站（只在 enroll／revoke／換簽章金鑰時用；密碼用完就丟）
# ---------------------------------------------------------------------------


class _Account:
    def __init__(self, http: httpx.Client, account_url: str) -> None:
        self.http = http
        self.base = store.check_url(account_url, "帳號網站網址")
        self.csrf = ""

    def _req(self, method: str, path: str, body: dict | None = None) -> _Reply:
        headers = {"X-CSRF-Token": self.csrf} if method != "GET" and self.csrf else None
        reply = _call(self.http, method, self.base + path, api=False, json_body=body, headers=headers,
                      timeout=ACCOUNT_TIMEOUT_SECONDS)
        if reply.status >= 400:
            raise _error(reply)
        return reply

    def login(self, username: str, password: str) -> None:
        if not username or not password:
            raise SdkError("missing_credentials", "需要帳號與密碼", kind_hint="auth")
        reply = self._req("POST", "/ui/api/login", {"username": username, "password": password})
        self.csrf = str((reply.data or {}).get("csrf", ""))

    def logout(self) -> None:
        if not self.csrf:
            return
        try:
            self._req("POST", "/ui/api/logout", {})
        except SdkError:
            pass
        finally:
            self.csrf = ""

    def portal(self) -> dict:
        return self._req("GET", "/ui/api/portal").data or {}

    def create_key(self, *, name: str, encryption: str, sig_public_key: str | None,
                   expires_days: int | None) -> dict:
        body: dict[str, Any] = {"name": name, "encryption": encryption}
        if sig_public_key:
            body["sig_public_key"] = sig_public_key
        if expires_days:
            body["expires_days"] = expires_days
        return self._req("POST", "/ui/api/keys", body).data or {}

    def update_key(self, key_id: str, changes: dict) -> dict:
        return self._req("PATCH", f"/ui/api/keys/{key_id}", changes).data or {}

    def delete_key(self, key_id: str) -> None:
        self._req("DELETE", f"/ui/api/keys/{key_id}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client:
    """一個設定檔的用戶端。用 ``Client.from_profile("名稱")`` 建立。

    執行緒：同一個 Client 不要在多條執行緒同時用（httpx.Client 本身可以，但設定檔的寫入沒有鎖）。
    """

    def __init__(self, profile: Profile, *, http: httpx.Client | None = None,
                 home: str | Path | None = None) -> None:
        self._profile = profile
        self._home = home
        self._own_http = http is None
        self._http = http or default_http()

    @classmethod
    def from_profile(cls, name: str = "default", *, home: str | Path | None = None,
                     http: httpx.Client | None = None) -> Client:
        return cls(store.load(name, home=home), http=http, home=home)

    # -- 基本 --------------------------------------------------------------
    @property
    def profile(self) -> Profile:
        return self._profile

    @property
    def name(self) -> str:
        return self._profile.name

    @property
    def transport(self) -> str:
        """這個設定檔實際會走的：e2e 或 plain（auto 在這裡解開）。"""
        return self._profile.resolved_transport()

    def close(self) -> None:
        if self._own_http:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _reload(self) -> None:
        try:
            self._profile = store.load(self._profile.name, home=self._home)
        except SdkError:
            pass

    def _usable(self) -> Profile:
        p = self._profile
        if p.status == "revoked" or not p.api_key:
            raise SdkError("key_revoked", f"設定檔「{p.name}」的 API key 已撤銷；請重新 enroll", kind_hint="auth")
        if not p.api_link:
            raise SdkError("profile_incomplete", "設定檔沒有 API 連結；用 llmop client set-link 設定")
        return p

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._usable().api_key}"}

    def _url(self, prefix: str, path: str) -> str:
        return self._usable().api_link + prefix + path

    def _api(self, method: str, prefix: str, path: str, body: Any = None,
             timeout: float = HTTP_TIMEOUT_SECONDS) -> _Reply:
        reply = _call(self._http, method, self._url(prefix, path), api=True, json_body=body,
                      headers=self._auth(), timeout=timeout)
        if reply.status >= 400:
            raise _error(reply)
        return reply

    def _pick_transport(self, transport: str | None) -> str:
        t = transport or "auto"
        if t not in store.TRANSPORTS:
            raise SdkError("bad_request", "transport 只能是 auto、e2e、plain")
        if t == "auto":
            return self.transport
        if t == "e2e" and not (self._profile.has_signing_key and self._profile.pinned_workers):
            raise SdkError("not_pinned", "這個設定檔不能走加密：沒有簽章金鑰或沒有釘選的 worker", kind_hint="trust")
        return t

    # -- 模型 --------------------------------------------------------------
    def _announced(self) -> list[tuple[str, str, str, str]]:
        """GET /e2e/v1/models → [(model, key_id, public_key, owned_by)]，格式不對的略過。"""
        data = self._api("GET", E2E_PREFIX, "/models").data or {}
        out = []
        for entry in data.get("data") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("llmop"), dict):
                continue
            model, meta = entry.get("id"), entry["llmop"]
            kid, pub = meta.get("key_id"), meta.get("public_key")
            if not (isinstance(model, str) and 0 < len(model) <= MAX_MODEL_LEN):
                continue
            if isinstance(kid, str) and isinstance(pub, str):
                out.append((model, kid, pub, str(entry.get("owned_by", ""))))
        return out

    def _pinned_models(self) -> list[tuple[str, PinnedWorker]]:
        """已釘選、而且閘道給的 key_id 由公鑰重算後相符的 (model, worker)，依釘選順序。"""
        by_pub = {p.public_key: p for p in self._profile.pinned_workers}
        found: list[tuple[str, PinnedWorker]] = []
        for model, kid, pub, _owner in self._announced():
            pin = by_pub.get(pub)
            if pin is None:
                continue
            try:
                ok = crypto.worker_key_id(b64u_decode(pub)) == kid == pin.key_id
            except (ProtocolError, ValueError):
                ok = False
            if ok:
                found.append((model, pin))
            else:
                log.warning("閘道給的 key_id 與公鑰不符，已略過 model=%s", model)
        order = {p.public_key: i for i, p in enumerate(self._profile.pinned_workers)}
        found.sort(key=lambda mp: order[mp[1].public_key])
        return found

    def models(self, *, transport: str | None = None) -> list[ModelEntry]:
        """可用的模型。加密：只列已釘選的 worker 提供的；明文：/v1/models。"""
        if self._pick_transport(transport) == "e2e":
            return [ModelEntry(id=m, key_id=w.key_id) for m, w in self._pinned_models()]
        data = self._api("GET", PLAIN_PREFIX, "/models").data or {}
        out = []
        for entry in data.get("data") or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ctx = entry.get("context_length")
                out.append(ModelEntry(id=entry["id"], owned_by=str(entry.get("owned_by", "")),
                                      context_length=ctx if type(ctx) is int else None))
        return out

    # -- 聊天 --------------------------------------------------------------
    def chat(self, body: dict, *, model: str | None = None, max_tokens: int | None = None,
             transport: str | None = None, deadline: float = DEADLINE_SECONDS,
             on_queue: Callable[[dict], None] | None = None) -> dict:
        """送一筆 chat completions，回 OpenAI 的 chat.completion（dict）。

        body：OpenAI 的請求本文（messages、temperature、tools…）。model／max_tokens 參數優先於 body 裡的值。
        不支援串流（body 帶 stream: true 會被拒）。transport：None／"auto" 照設定檔，"e2e"、"plain" 強制。
        on_queue：加密模式排隊時收到 {"position": n, "eta": 秒}。
        """
        if not isinstance(body, dict):
            raise SdkError("bad_request", "body 必須是 dict")
        if body.get("stream"):
            raise SdkError("bad_request", "SDK 不支援串流（stream 必須是 false）")
        how = self._pick_transport(transport)
        low, high = MAX_TOKENS_RANGE
        tokens = max_tokens if max_tokens is not None else body.get("max_tokens", self._profile.default_max_tokens)
        if type(tokens) is not int or not low <= tokens <= high:
            raise SdkError("bad_request", f"max_tokens 必須是 {low} 到 {high} 的整數")
        wanted = model if model is not None else body.get("model")
        started = time.monotonic()
        try:
            if how == "e2e":
                return self._chat_e2e(body, wanted, tokens, started + deadline, on_queue)
            payload = dict(body, max_tokens=tokens, stream=False)
            payload["model"] = wanted if isinstance(wanted, str) and wanted else "default"
            return self._api("POST", PLAIN_PREFIX, "/chat/completions", payload,
                             timeout=min(HTTP_TIMEOUT_SECONDS, deadline)).data or {}
        finally:
            log.info("chat transport=%s duration=%.2fs", how, time.monotonic() - started)

    def _chat_e2e(self, body: dict, wanted: object, max_tokens: int, end: float,
                  on_queue: Callable[[dict], None] | None) -> dict:
        signing_key = keyfile.load(self._profile.signing_key_path)
        candidates = self._pinned_models()
        if not candidates:
            raise SdkError("model_unavailable", "沒有任何已釘選的 worker 在線上（不會改用未釘選的金鑰）",
                           status=404, kind_hint="offline")
        if not isinstance(wanted, str) or wanted.strip().lower() in _ANY_MODEL:
            model, worker = candidates[0]
        else:
            hit = next(((m, w) for m, w in candidates if m == wanted), None)
            if hit is None:
                raise SdkError("model_unavailable", f"已釘選的 worker 沒有提供模型 {wanted!r}", status=404)
            model, worker = hit
        inner = {k: v for k, v in body.items() if k not in ("model", "max_tokens", "stream")}
        try:
            envelope, opener = crypto.seal_request(inner, model=model, max_tokens=max_tokens,
                                                   worker_public=b64u_decode(worker.public_key),
                                                   signing_key=signing_key)
        except (TypeError, ValueError) as exc:
            raise SdkError("bad_request", "body 無法序列化成 JSON") from exc
        request_id = envelope.header.request_id

        def remaining() -> float:
            left = end - time.monotonic()
            if left <= 0:
                raise SdkError("timeout", "超過整體期限仍未完成", kind_hint="timeout")
            return min(HTTP_TIMEOUT_SECONDS, left)

        def poll() -> _Reply:
            attempt = 0
            while True:
                try:
                    return _send(self._http, "GET", self._url(E2E_PREFIX, f"/requests/{request_id}"), api=True,
                                 headers=self._auth(), timeout=remaining())
                except _Transient as exc:
                    delay = POLL_BACKOFF[min(attempt, len(POLL_BACKOFF) - 1)]
                    attempt += 1
                    log.warning("輪詢時網路失敗（%s），%.1f 秒後重試", exc.error.code, delay)
                    time.sleep(min(delay, max(0.0, end - time.monotonic())))
                    remaining()

        try:
            try:
                reply = _send(self._http, "POST", self._url(E2E_PREFIX, "/chat/completions"), api=True,
                              json_body=envelope.to_json(), headers=self._auth(), timeout=remaining())
            except _Transient:
                reply = poll()  # 送出時網路斷了：用同一個 request_id 問閘道有沒有收到
                err = (reply.data or {}).get("error")
                if reply.status == 404 and isinstance(err, dict) and err.get("code") == "unknown_request":
                    raise SdkError("network_error", "送出請求時網路中斷，閘道沒有收到這筆請求",
                                   kind_hint="offline") from None
            while True:
                data = reply.data or {}
                if data.get("request_id", request_id) != request_id:
                    raise SdkError("bad_response", "閘道回應的 request_id 與請求不符", kind_hint="server")
                if reply.status == 200 and data.get("status") == "done" and isinstance(data.get("sealed"), str):
                    sealed = data["sealed"]
                    break
                if reply.status == 202 and data.get("status") == "pending":
                    if on_queue is not None and (queue := _queue_info(data)) is not None:
                        on_queue(queue)
                    asked = time.monotonic()
                    reply = poll()
                    if reply.status == 202:
                        time.sleep(max(0.0, min(POLL_MIN_INTERVAL - (time.monotonic() - asked),
                                                end - time.monotonic())))
                    continue
                raise _error(reply)
            try:
                result = opener.open(sealed)
            except ProtocolError as exc:
                raise SdkError(exc.code, exc.message, kind_hint="trust") from None
        finally:
            opener = None  # 一次性回覆私鑰只在記憶體，用完放掉
        if not result["ok"]:
            err = result["error"]
            raise SdkError(err["code"], err["message"], status=502)
        return result["body"]

    # -- 帳號資訊 ----------------------------------------------------------
    def me(self) -> dict:
        """這個設定檔與帳號的資訊。``account`` 是伺服器 /v1/me 的內容；「必須加密」的 key 拿不到，
        這時 ``account`` 為 None、``account_error`` 寫原因。"""
        p = self._profile
        info: dict[str, Any] = {
            "profile": p.name,
            "transport": self.transport,
            "status": p.status,
            "encryption": p.encryption,
            "api_link": store.mask_link(p.api_link),
            "key": store.mask_key(p.api_key),
            "pinned_workers": [w.key_id for w in p.pinned_workers],
            "account": None,
            "account_error": None,
        }
        try:
            info["account"] = self._api("GET", PLAIN_PREFIX, "/me").data
        except SdkError as exc:
            if exc.code != "encryption_required":
                raise
            info["account_error"] = "encryption_required"
        return info

    # -- 連結 --------------------------------------------------------------
    def set_link(self, url: str, *, check: bool = True) -> bool | None:
        """換成新的 API 連結（向服務提供者拿的）。check=True 時先打 <連結>/health 試一次：
        回 True（通）／False（不通，仍然存下）；check=False 回 None。"""
        link = store.check_url(url, "API 連結")
        self._profile = store.update(self._profile, api_link=link)
        return self.link_ok() if check else None

    def link_ok(self) -> bool:
        try:
            reply = _call(self._http, "GET", self._profile.api_link + "/health", api=True, timeout=15.0)
        except SdkError:
            return False
        return reply.status == 200

    # -- 釘選 --------------------------------------------------------------
    def pin_worker(self, fingerprint: str) -> PinResult:
        """在閘道公布的 worker 公鑰裡找指紋（key_id）＝fingerprint 的那把，用公鑰重算確認後釘選。"""
        fp = (fingerprint or "").strip()
        if not fp:
            raise SdkError("bad_request", "需要 worker 的指紋（22 個字元的 key_id）")
        announced = [(m, kid, pub) for m, kid, pub, _o in self._announced() if kid == fp]
        if not announced:
            raise SdkError("fingerprint_mismatch", "伺服器沒有公布這個指紋的金鑰（worker 沒上線，或指紋打錯）；沒有釘選",
                           kind_hint="trust")
        pubs = {pub for _m, _k, pub in announced}
        good = []
        for pub in pubs:
            try:
                if crypto.worker_key_id(b64u_decode(pub)) == fp:
                    good.append(pub)
            except (ProtocolError, ValueError):
                continue
        if len(good) != 1:
            raise SdkError("fingerprint_mismatch", "指紋不符，可能被調包，沒有釘選", kind_hint="trust")
        pub = good[0]
        models = tuple(sorted({m for m, _k, p in announced if p == pub}))
        p = self._profile
        if any(w.public_key == pub for w in p.pinned_workers):
            return PinResult(fp, False, models)
        status = "ready" if p.status == "incomplete" else p.status
        self._profile = store.update(p, pinned_workers=p.pinned_workers + (PinnedWorker(fp, pub),), status=status)
        return PinResult(fp, True, models)

    def unpin_worker(self, key_id: str) -> None:
        p = self._profile
        kept = tuple(w for w in p.pinned_workers if w.key_id != key_id)
        if len(kept) == len(p.pinned_workers):
            raise SdkError("not_pinned", f"沒有釘選這個指紋：{key_id}", kind_hint="bad_request")
        self._profile = store.update(p, pinned_workers=kept)

    def pending_worker_keys(self) -> list[str]:
        """閘道公布了、但還沒釘選的 worker 指紋。只是提醒，絕不自動信任。"""
        pinned = {w.key_id for w in self._profile.pinned_workers}
        return sorted({kid for _m, kid, _p, _o in self._announced() if kid not in pinned})

    # -- 帳號操作（要密碼） --------------------------------------------------
    def _account(self) -> _Account:
        p = self._profile
        if not p.account_url or not p.username or not p.key_id:
            raise SdkError("bad_request", "這個設定檔不是用帳號建立的（例如 add-plain）；請向服務提供者撤銷或更換 key")
        return _Account(self._http, p.account_url)

    def rotate_signing_key(self, password: str) -> str:
        """換一把新的簽章金鑰：登入 → 更新伺服器上這把 key 的簽章公鑰 → 換本機檔。回傳新的 sig_key_id。
        （不准只憑 API key 換簽章公鑰，所以一定要密碼。）"""
        p = self._profile
        account = self._account()
        new_key = crypto.generate_signing_key()
        public_key, sig_key_id = keyfile.key_info(new_key)
        account.login(p.username, password)
        try:
            account.update_key(p.key_id, {"sig_public_key": public_key})
        finally:
            account.logout()
        keyfile.write(p.signing_key_path, new_key)
        return sig_key_id

    def revoke(self, password: str) -> None:
        """在伺服器撤銷這把 API key（要密碼），本機設定檔標成 revoked、清掉 key。"""
        p = self._profile
        account = self._account()
        account.login(p.username, password)
        try:
            try:
                account.delete_key(p.key_id)
            except SdkError as exc:
                if exc.status != 404:  # 404＝已經撤銷了
                    raise
        finally:
            account.logout()
        self._profile = store.update(p, api_key="", status="revoked")

    def forget(self) -> list[str]:
        """刪掉這台電腦上的設定檔（API key、簽章私鑰、釘選）。不會撤銷伺服器上的 key。回傳刪掉的檔名。"""
        return store.delete(self._profile)

    # -- 自我檢查 ----------------------------------------------------------
    def self_check(self, *, fix: bool = False) -> list[tuple[str, bool, str]]:
        """回 [(項目, 通過?, 說明)]。fix=True 先把檔案權限重新收緊。不送任何對話內容。"""
        p = self._profile
        results: list[tuple[str, bool, str]] = []

        def add(item: str, ok: bool, note: str = "") -> None:
            results.append((item, ok, note))

        files = [p.config_path] + ([p.signing_key_path] if p.has_signing_key else [])
        for f in files:
            if fix:
                restrict(f)
            ok, note = check_private(f)
            add(f"檔案權限：{f.name}", ok, note)
        add("設定檔完整", p.status == "ready" and bool(p.api_key and p.api_link),
            {"ready": "", "incomplete": "還沒釘選 worker（llmop client pin）", "revoked": "key 已撤銷"}.get(p.status, p.status))
        if not (p.api_key and p.api_link):
            return results
        link_ok = self.link_ok()
        add("API 連結連得到", link_ok, "" if link_ok else "連不上或連結已更換（向服務提供者拿新連結）")
        how = self.transport
        try:
            got = self.models()
            add(f"API key 有效（{how}）", True, f"{len(got)} 個模型")
        except SdkError as exc:
            add(f"API key 有效（{how}）", False, f"{exc.kind_hint}: {exc.code}")
        if p.encryption == "required":
            try:
                self._api("GET", PLAIN_PREFIX, "/models")
                add("明文被拒（必須加密的 key）", False, "伺服器接受了明文")
            except SdkError as exc:
                add("明文被拒（必須加密的 key）", exc.code == "encryption_required", exc.code)
        if how == "e2e":
            add("沒有簽章的請求被拒", *self._unsigned_refused())
            pending = []
            try:
                pending = self.pending_worker_keys()
            except SdkError:
                pass
            if pending:
                add("伺服器有還沒釘選的新金鑰", True, "向服務提供者核對指紋後 llmop client pin：" + ", ".join(pending))
        return results

    def _unsigned_refused(self) -> tuple[bool, str]:
        """用一把隨機、沒登記過的簽章金鑰送信封：閘道必須拒絕（bad_signature）。"""
        workers = self._pinned_models()
        if not workers:
            return False, "沒有線上的已釘選 worker，無法測"
        model, worker = workers[0]
        stranger = crypto.generate_signing_key()
        envelope, _opener = crypto.seal_request({"messages": []}, model=model, max_tokens=1,
                                                worker_public=b64u_decode(worker.public_key), signing_key=stranger)
        try:
            reply = _call(self._http, "POST", self._url(E2E_PREFIX, "/chat/completions"), api=True,
                          json_body=envelope.to_json(), headers=self._auth())
        except SdkError as exc:
            return False, exc.code
        if reply.status in (400, 401, 403):
            return True, str(((reply.data or {}).get("error") or {}).get("code", reply.status))
        return False, f"HTTP {reply.status}"


# ---------------------------------------------------------------------------
# 建立設定檔：enroll（帳號密碼）與 add_plain（只有一把 key）
# ---------------------------------------------------------------------------


def _require_new(name: str, home: str | Path | None, replace_existing: bool) -> None:
    if store.exists(name, home=home) and not replace_existing:
        raise SdkError("profile_exists", f"設定檔「{name}」已存在；換個名稱，或先 llmop client forget --profile {name}")


def enroll(account_url: str, username: str, password: str, *, worker_fingerprint: str | None = None,
           profile: str = "default", encryption: str = "required", key_name: str = "",
           expires_days: int | None = None, replace_key_id: str | None = None, api_link: str | None = None,
           test: bool = False, home: str | Path | None = None, http: httpx.Client | None = None) -> EnrollResult:
    """用帳號密碼在帳號網站建一把 API key，並在這台電腦建好設定檔。

    1. encryption="required"（預設）時在本機產生簽章金鑰（私鑰不離開這台電腦），只上傳公鑰。
    2. 登入帳號網站 → （可選）刪掉 replace_key_id → 建 key → 登出。密碼不存檔、不寫紀錄。
    3. API 連結：api_link 參數，沒給就用帳號網站顯示的目前連結。
    4. 有 worker_fingerprint 就到 API 連結的 /e2e/v1/models 找這個指紋、用公鑰重算確認後釘選；
       對不上 → SdkError(kind_hint="trust")，設定檔留著但標 incomplete。
    5. test=True 時送一句測試（走這個設定檔的傳輸方式）。
    """
    if encryption not in ("required", "optional"):
        raise SdkError("bad_request", "encryption 只能是 required 或 optional")
    if encryption == "required" and not worker_fingerprint:
        raise SdkError("bad_request", "必須加密的 key 需要 worker 指紋（向服務提供者拿 22 個字元的 key_id）")
    _require_new(profile, home, False)
    own_http = http is None
    client_http = http or default_http()
    try:
        prof = store.new_profile(profile, home=home, account_url=store.check_url(account_url, "帳號網站網址"),
                                 username=username, encryption=encryption, status="incomplete",
                                 transport="auto")
        if api_link:
            prof = replace(prof, api_link=store.check_url(api_link, "API 連結"))
        store.ensure_dir(prof.directory)
        sig_public = sig_key_id = ""
        created = False
        if encryption == "required" or worker_fingerprint:
            key, created = keyfile.ensure(prof.signing_key_path)
            sig_public, sig_key_id = keyfile.key_info(key)

        account = _Account(client_http, prof.account_url)
        account.login(username, password)
        try:
            if not prof.api_link:
                link = ((account.portal().get("api_link") or {}).get("url")) or ""
                if not link:
                    raise SdkError("bad_request", "帳號網站沒有提供 API 連結；請用 --api-link 指定（向服務提供者拿）")
                prof = replace(prof, api_link=store.check_url(link, "API 連結"))
            if replace_key_id:
                account.delete_key(replace_key_id)
            made = account.create_key(name=key_name or f"llmop-sdk-{profile}", encryption=encryption,
                                      sig_public_key=sig_public or None, expires_days=expires_days)
        finally:
            account.logout()
        api_key = str(made.get("key", ""))
        record = made.get("record") or {}
        prof = store.save(replace(prof, api_key=store.check_api_key(api_key), key_id=str(record.get("id", "")),
                                  status="incomplete" if worker_fingerprint else "ready"))
        client = Client(prof, http=client_http, home=home)
        pinned: tuple[str, ...] = ()
        if worker_fingerprint:
            client.pin_worker(worker_fingerprint)  # 失敗時丟 trust，設定檔維持 incomplete
            pinned = tuple(w.key_id for w in client.profile.pinned_workers)
        reply = None
        if test:
            completion = client.chat({"messages": [{"role": "user", "content": "ping"}]}, max_tokens=16)
            reply = (((completion.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
        final = client.profile
        return EnrollResult(
            profile=final.name, transport=final.resolved_transport(), status=final.status,
            key_record_id=final.key_id, key_display=store.mask_key(final.api_key), sig_key_id=sig_key_id,
            signing_key_created=created, pinned=pinned, api_link_display=store.mask_link(final.api_link),
            test_reply=reply,
        )
    finally:
        if own_http:
            client_http.close()


def add_plain(api_link: str, api_key: str, *, profile: str = "default", max_tokens: int | None = None,
              home: str | Path | None = None) -> Profile:
    """只有一把 key（例如服務提供者發的 GUEST key）：建一個走明文的設定檔。不連網。"""
    _require_new(profile, home, False)
    prof = store.new_profile(profile, home=home, api_link=store.check_url(api_link, "API 連結"),
                             api_key=store.check_api_key(api_key), transport="plain", status="ready",
                             default_max_tokens=max_tokens or store.DEFAULT_MAX_TOKENS)
    return store.save(prof)


def set_link(url: str, *, profile: str = "default", home: str | Path | None = None) -> None:
    """不建 Client、直接換某個設定檔的 API 連結（不試連）。"""
    store.update(store.load(profile, home=home), api_link=store.check_url(url, "API 連結"))


# ---------------------------------------------------------------------------
# AsyncClient：同步 Client 的薄包（每個方法丟到執行緒）
# ---------------------------------------------------------------------------


class AsyncClient:
    """跟 Client 同名的方法，都是 async（內部用 asyncio.to_thread 呼叫同步版）。"""

    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    def from_profile(cls, name: str = "default", *, home: str | Path | None = None,
                     http: httpx.Client | None = None) -> AsyncClient:
        return cls(Client.from_profile(name, home=home, http=http))

    @property
    def sync(self) -> Client:
        return self._client

    @property
    def profile(self) -> Profile:
        return self._client.profile

    @property
    def transport(self) -> str:
        return self._client.transport

    async def models(self, **kw: Any) -> list[ModelEntry]:
        return await asyncio.to_thread(self._client.models, **kw)

    async def chat(self, body: dict, **kw: Any) -> dict:
        return await asyncio.to_thread(self._client.chat, body, **kw)

    async def me(self) -> dict:
        return await asyncio.to_thread(self._client.me)

    async def set_link(self, url: str, **kw: Any) -> bool | None:
        return await asyncio.to_thread(self._client.set_link, url, **kw)

    async def pin_worker(self, fingerprint: str) -> PinResult:
        return await asyncio.to_thread(self._client.pin_worker, fingerprint)

    async def unpin_worker(self, key_id: str) -> None:
        await asyncio.to_thread(self._client.unpin_worker, key_id)

    async def pending_worker_keys(self) -> list[str]:
        return await asyncio.to_thread(self._client.pending_worker_keys)

    async def rotate_signing_key(self, password: str) -> str:
        return await asyncio.to_thread(self._client.rotate_signing_key, password)

    async def revoke(self, password: str) -> None:
        await asyncio.to_thread(self._client.revoke, password)

    async def forget(self) -> list[str]:
        return await asyncio.to_thread(self._client.forget)

    async def self_check(self, **kw: Any) -> list[tuple[str, bool, str]]:
        return await asyncio.to_thread(self._client.self_check, **kw)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._client.close)

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = [
    "AsyncClient", "Client", "EnrollResult", "ModelEntry", "PinResult", "add_plain", "enroll", "set_link",
]
