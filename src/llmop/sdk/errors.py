"""SDK 的錯誤：一個類別 SdkError，帶 kind_hint 讓呼叫端（例如 waterfall）分類。

kind_hint 只有這幾種（字串，契約）：

| kind_hint      | 意思 |
|---|---|
| quota          | 額度用完（明文 429 insufficient_quota、401 key_exhausted、加密 429 quota_exceeded） |
| auth           | 金鑰無效、過期、撤銷（401）；帳號停用 |
| rate           | 太頻繁或排隊太多（其他 429，例如 queue_wait） |
| link_changed   | API 連結的秘密路徑回 404：連結已更換，要向服務提供者拿新連結 |
| offline        | 連不上（網路、通道沒在跑、Cloudflare 530、worker 斷線） |
| timeout        | 逾時（含 Cloudflare 524、閘道 504、整體期限） |
| trust          | 加密的信任問題：指紋不符、簽章不對、解不開回覆 |
| bad_request    | 請求或設定有問題（400、模型不存在、這把 key 只能加密使用…） |
| server         | 伺服器那邊的錯（5xx、佇列滿） |
| unknown        | 其他 |
"""

from __future__ import annotations

from typing import Literal

KindHint = Literal[
    "quota", "auth", "rate", "link_changed", "offline", "timeout", "trust", "bad_request", "server", "unknown"
]
KIND_HINTS: tuple[str, ...] = (
    "quota", "auth", "rate", "link_changed", "offline", "timeout", "trust", "bad_request", "server", "unknown",
)

LINK_CHANGED_MESSAGE = "API 連結已更換，請向服務提供者拿新連結（然後執行 llmop client set-link）"

# 伺服器的錯誤碼 → kind_hint。明文 /v1 的碼仿 OpenAI；加密 /e2e/v1 的碼見 docs/secure/PROTOCOL.md 3.5。
_CODE_KIND: dict[str, str] = {
    # 額度
    "insufficient_quota": "quota",
    "key_exhausted": "quota",
    "quota_exceeded": "quota",
    # 金鑰／帳號
    "invalid_api_key": "auth",
    "key_expired": "auth",
    "key_revoked": "auth",
    "unauthorized": "auth",
    "account_disabled": "auth",
    "invalid_credentials": "auth",
    "not_logged_in": "auth",
    # 頻率
    "rate_limit_exceeded": "rate",
    "queue_wait": "rate",
    "too_many_attempts": "rate",
    # 信任
    "bad_signature": "trust",
    "untrusted_client": "trust",
    "decrypt_failed": "trust",
    "unknown_key_id": "trust",
    "fingerprint_mismatch": "trust",
    "not_pinned": "trust",
    # 連線／逾時
    "worker_disconnected": "offline",
    "network_error": "offline",
    "timeout": "timeout",
    "backend_timeout": "timeout",
    # 伺服器
    "gateway_busy": "server",
    "worker_busy": "server",
    "queue_full": "server",
    "internal_error": "server",
    "backend_error": "server",
    "bad_backend_response": "server",
    "bad_response": "server",
    # 請求／設定
    "encryption_required": "bad_request",
    "model_not_found": "bad_request",
    "model_not_allowed": "bad_request",
    "model_unavailable": "bad_request",
    "bad_request": "bad_request",
    "bad_envelope": "bad_request",
    "invalid_request": "bad_request",
    "invalid_json": "bad_request",
    "request_too_large": "bad_request",
    "payload_too_large": "bad_request",
    "stale_timestamp": "bad_request",
    "max_api_keys": "bad_request",
    "api_keys_disabled": "bad_request",
    "local_only": "bad_request",
    "origin_mismatch": "bad_request",
    "csrf_failed": "bad_request",
    "profile_not_found": "bad_request",
    "profile_exists": "bad_request",
    "profile_incomplete": "bad_request",
    # 連結
    "link_changed": "link_changed",
}


def kind_for(code: str, status: int | None) -> str:
    """錯誤碼優先；沒有對照時看 HTTP 狀態。"""
    kind = _CODE_KIND.get(code or "")
    if kind:
        return kind
    if status is None:
        return "unknown"
    if status == 401:
        return "auth"
    if status == 429:
        return "rate"
    if status in (524, 504):
        return "timeout"
    if status in (530, 502):
        return "offline"
    if status >= 500:
        return "server"
    if 400 <= status < 500:
        return "bad_request"
    return "unknown"


class SdkError(Exception):
    """SDK 的唯一錯誤類別。

    code：伺服器或 SDK 的錯誤碼（例：insufficient_quota、link_changed、fingerprint_mismatch）。
    message：給人看的說明（繁體中文為主）。
    status：HTTP 狀態；不是 HTTP 造成的錯誤為 None。
    retry_after：伺服器的 Retry-After（秒）；沒有為 None。
    kind_hint：見模組說明的表。
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int | None = None,
        retry_after: float | None = None,
        kind_hint: str | None = None,
    ) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after
        hint = kind_hint or kind_for(code, status)
        self.kind_hint: str = hint if hint in KIND_HINTS else "unknown"

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code

    def __repr__(self) -> str:
        return (f"SdkError(code={self.code!r}, status={self.status!r}, kind_hint={self.kind_hint!r}, "
                f"retry_after={self.retry_after!r})")
