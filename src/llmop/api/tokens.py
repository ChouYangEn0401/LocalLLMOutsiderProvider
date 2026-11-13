"""Usage extraction and rough token estimates (when a backend reports no usage).

The estimate is ~4 characters per token, the usual rule of thumb for English;
it only has to be good enough for soft quotas.
"""

from __future__ import annotations

import json
from typing import Any

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_CHARS = 16  # role + chat-template markers, ~4 tokens per message


def chars_to_tokens(chars: int) -> int:
    return (max(0, chars) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def text_chars(value: Any) -> int:
    """Characters in a prompt-like value: a string, content parts, a list of strings,
    or token ids (each id counts as one token)."""
    if isinstance(value, str):
        return len(value)
    if _is_int(value):
        return CHARS_PER_TOKEN
    if isinstance(value, list):
        return sum(text_chars(v) for v in value)
    if isinstance(value, dict):  # content part: {"type": "text", "text": ...}
        text = value.get("text")
        return len(text) if isinstance(text, str) else 0
    return 0


def _json_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def estimate_prompt_tokens(endpoint: str, body: dict) -> int:
    if endpoint == "chat.completions":
        chars = 0
        for msg in body.get("messages") or []:
            if isinstance(msg, dict):
                chars += MESSAGE_OVERHEAD_CHARS + text_chars(msg.get("content"))
                if msg.get("tool_calls"):
                    chars += _json_chars(msg["tool_calls"])
        if body.get("tools"):
            chars += _json_chars(body["tools"])
        return chars_to_tokens(chars)
    if endpoint == "completions":
        return chars_to_tokens(text_chars(body.get("prompt")))
    if endpoint == "embeddings":
        return chars_to_tokens(text_chars(body.get("input")))
    return 0


def choice_chars(endpoint: str, choices: Any) -> int:
    """Generated characters in ``choices`` of a response or a stream chunk."""
    if not isinstance(choices, list):
        return 0
    total = 0
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        if endpoint == "completions":
            text = ch.get("text")
            total += len(text) if isinstance(text, str) else 0
            continue
        msg = ch.get("delta") if "delta" in ch else ch.get("message")
        if not isinstance(msg, dict):
            continue
        for key in ("content", "reasoning_content", "reasoning"):
            v = msg.get(key)
            if isinstance(v, str):
                total += len(v)
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                for key in ("name", "arguments"):
                    v = fn.get(key)
                    if isinstance(v, str):
                        total += len(v)
    return total


def usage_from(value: Any) -> tuple[int, int] | None:
    """``(prompt_tokens, completion_tokens)`` from an OpenAI ``usage`` object."""
    if not isinstance(value, dict):
        return None
    prompt, completion, total = (
        value.get("prompt_tokens"), value.get("completion_tokens"), value.get("total_tokens")
    )
    if not _is_int(completion):
        completion = None
    if not _is_int(prompt):
        if not _is_int(total):
            return None
        prompt = total - (completion or 0)
    if completion is None:
        completion = total - prompt if _is_int(total) and total > prompt else 0
    return max(0, prompt), max(0, completion)
