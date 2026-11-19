"""OpenAI 相容後端（LM Studio、vLLM）：/v1/chat/completions 與 /v1/models。

請求本文原樣轉送，只強制 model、max_tokens 與 stream: false；回應的 JSON 原樣交回。
"""

from __future__ import annotations

import aiohttp

from llmop.worker.backends import MODEL_LIST_TIMEOUT, Backend, BackendError, request_json
from llmop.worker.config import BackendConfig

# 用戶端可能用這個欄位指定上限；標頭裡的 max_tokens 才算數，所以拿掉。
_IGNORED_FIELDS = ("max_completion_tokens",)


def make(cfg: BackendConfig, session: aiohttp.ClientSession, request_timeout: float) -> Backend:
    base = cfg.base_url
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else None

    async def list_models() -> list[str]:
        data = await request_json(session, "GET", f"{base}/v1/models", timeout=MODEL_LIST_TIMEOUT, headers=headers)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise BackendError("backend_error", "/v1/models 的回應格式不符")
        return [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]

    async def chat(body: dict, model: str, max_tokens: int) -> dict:
        payload = {k: v for k, v in body.items() if k not in _IGNORED_FIELDS}
        payload.update(model=model, max_tokens=max_tokens, stream=False)
        data = await request_json(
            session, "POST", f"{base}/v1/chat/completions", timeout=request_timeout, payload=payload, headers=headers
        )
        if not isinstance(data, dict):
            raise BackendError("backend_error", "/v1/chat/completions 的回應不是 JSON 物件")
        return data

    return Backend(name=f"openai {base}", list_models=list_models, chat=chat)
