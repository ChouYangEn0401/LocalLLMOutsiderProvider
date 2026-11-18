"""Ollama 原生 API 後端：/api/chat 與 /api/tags。

用戶端送來的是 OpenAI chat-completions 形狀的本文，這裡負責雙向翻譯：
- 請求：訊息（文字、data URI 圖片、工具呼叫、工具結果）、tools、取樣參數、response_format。
- 回覆：轉回 OpenAI chat.completion 物件（含 tool_calls、reasoning_content、finish_reason、usage）。
工具呼叫只做格式轉換，worker 從不執行任何工具。
"""

from __future__ import annotations

import json
import secrets
import time

import aiohttp

from llmop.worker.backends import MODEL_LIST_TIMEOUT, Backend, BackendError, request_json
from llmop.worker.config import BackendConfig

# 有提供就原樣放進 Ollama options 的 OpenAI 取樣參數。
_COPIED_OPTIONS = ("temperature", "top_p", "seed", "stop", "presence_penalty", "frequency_penalty")


def make(cfg: BackendConfig, session: aiohttp.ClientSession, request_timeout: float) -> Backend:
    base = cfg.base_url

    async def list_models() -> list[str]:
        data = await request_json(session, "GET", f"{base}/api/tags", timeout=MODEL_LIST_TIMEOUT)
        items = data.get("models") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise BackendError("backend_error", "/api/tags 的回應格式不符")
        return [m["name"] for m in items if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]]

    async def chat(body: dict, model: str, max_tokens: int) -> dict:
        payload = to_ollama(body, model, max_tokens)
        data = await request_json(session, "POST", f"{base}/api/chat", timeout=request_timeout, payload=payload)
        return from_ollama(data, model)

    return Backend(name=f"ollama {base}", list_models=list_models, chat=chat)


# ---------------------------------------------------------------------------
# 請求：OpenAI → Ollama
# ---------------------------------------------------------------------------


def _bad_request(message: str) -> BackendError:
    return BackendError("bad_request", message)


def to_ollama(body: dict, model: str, max_tokens: int) -> dict:
    """把 OpenAI 形狀的本文翻成 Ollama /api/chat 的本文。model 與 max_tokens 由呼叫端強制指定。"""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _bad_request("messages 必須是非空陣列")
    call_names: dict[str, str] = {}  # tool_call_id → 函式名稱，讓工具結果帶上 tool_name
    options: dict = {"num_predict": max_tokens}
    for key in _COPIED_OPTIONS:
        if body.get(key) is not None:
            options[key] = body[key]
    if isinstance(options.get("stop"), str):
        options["stop"] = [options["stop"]]  # OpenAI 允許單一字串，Ollama 只收陣列
    payload = {
        "model": model,
        "messages": [_message(m, call_names) for m in messages],
        "stream": False,
        "options": options,
    }
    if body.get("tools") is not None:
        payload["tools"] = body["tools"]
    fmt = _format(body.get("response_format"))
    if fmt is not None:
        payload["format"] = fmt
    return payload


def _message(message: object, call_names: dict[str, str]) -> dict:
    if not isinstance(message, dict) or not isinstance(message.get("role"), str):
        raise _bad_request("每則訊息都必須是帶有 role 的物件")
    role = message["role"]
    text, images = _content(message.get("content"))
    item: dict = {"role": role, "content": text}
    if images:
        item["images"] = images
    if role == "assistant" and message.get("tool_calls"):
        item["tool_calls"] = [_tool_call(call, call_names) for call in _as_list(message["tool_calls"], "tool_calls")]
    if role == "tool":
        name = call_names.get(message.get("tool_call_id"))
        if name:
            item["tool_name"] = name
    return item


def _as_list(value: object, name: str) -> list:
    if not isinstance(value, list):
        raise _bad_request(f"{name} 必須是陣列")
    return value


def _content(content: object) -> tuple[str, list[str]]:
    """回傳 (文字, 圖片的純 base64 清單)。content-part 陣列的文字以換行串接。"""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[str] = []
    for part in _as_list(content, "content"):
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            images.append(_image_base64(url))
        else:
            raise _bad_request(f"不支援的內容類型：{kind!r}")
    return "\n".join(texts), images


def _image_base64(url: object) -> str:
    """從 data URI 取出 base64 本體（去掉 data:…;base64, 前綴）。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        raise _bad_request("Ollama 只接受 data: URI 形式的圖片；worker 不會替你下載網址")
    prefix, comma, data = url.partition(",")
    if not comma or not prefix.endswith(";base64") or not data:
        raise _bad_request("圖片的 data URI 必須是 base64 編碼")
    return data


def _tool_call(call: object, call_names: dict[str, str]) -> dict:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise _bad_request("tool_calls 的每一項都必須有 function.name")
    if isinstance(call.get("id"), str):
        call_names[call["id"]] = function["name"]
    return {"function": {"name": function["name"], "arguments": _arguments(function.get("arguments"))}}


def _arguments(raw: object) -> dict:
    """OpenAI 的 arguments 是 JSON 字串，Ollama 要物件。"""
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if isinstance(value, dict):
            return value
    raise _bad_request("tool_calls 的 arguments 必須是 JSON 物件字串")


def _format(response_format: object) -> object:
    """response_format → Ollama 的 format：json_object 變 "json"，json_schema 變 schema 本身。"""
    if response_format is None:
        return None
    kind = response_format.get("type") if isinstance(response_format, dict) else None
    if kind == "text":
        return None
    if kind == "json_object":
        return "json"
    if kind == "json_schema":
        spec = response_format.get("json_schema")
        schema = spec.get("schema") if isinstance(spec, dict) else None
        if isinstance(schema, dict):
            return schema
        raise _bad_request("response_format.json_schema 必須有 schema 物件")
    raise _bad_request(f"不支援的 response_format：{kind!r}")


# ---------------------------------------------------------------------------
# 回覆：Ollama → OpenAI chat.completion
# ---------------------------------------------------------------------------


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def from_ollama(data: object, model: str) -> dict:
    """把 Ollama /api/chat 的非串流回應轉成 OpenAI chat.completion 物件。"""
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        raise BackendError("backend_error", f"Ollama 回報錯誤：{data['error'][:300]}")
    reply = data.get("message") if isinstance(data, dict) else None
    if not isinstance(reply, dict):
        raise BackendError("backend_error", "Ollama 的回應缺少 message")
    content = reply.get("content")
    message: dict = {"role": reply.get("role") or "assistant", "content": content if isinstance(content, str) else ""}
    thinking = reply.get("thinking")
    if isinstance(thinking, str) and thinking:
        message["reasoning_content"] = thinking
    calls = reply.get("tool_calls") or []
    if not isinstance(calls, list):
        raise BackendError("backend_error", "Ollama 回應的 tool_calls 格式不符")
    if calls:
        message["tool_calls"] = [_reply_tool_call(i, call) for i, call in enumerate(calls)]
        finish_reason = "tool_calls"
    elif data.get("done_reason") == "length":
        finish_reason = "length"
    else:
        finish_reason = "stop"
    prompt_tokens = _count(data.get("prompt_eval_count"))
    completion_tokens = _count(data.get("eval_count"))
    return {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _reply_tool_call(index: int, call: object) -> dict:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise BackendError("backend_error", "Ollama 回應的 tool_calls 格式不符")
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return {"id": f"call_{index}", "type": "function", "function": {"name": function["name"], "arguments": arguments}}
