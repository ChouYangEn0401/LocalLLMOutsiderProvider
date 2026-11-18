"""Ollama 翻譯：OpenAI 本文 ↔ /api/chat。"""

import json

import pytest

from llmop.worker.backends import BackendError
from llmop.worker.backends.ollama import from_ollama, to_ollama

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天氣",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


def ollama_reply(**message):
    return {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": "", **message},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 30,
        "eval_count": 4,
    }


def test_plain_request_and_forced_fields():
    body = {
        "model": "ignored",
        "max_tokens": 5,
        "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "top_p": 0.9,
        "seed": 7,
        "stop": "END",
        "presence_penalty": 0.1,
        "frequency_penalty": 0.3,
        "tool_choice": "auto",
    }
    payload = to_ollama(body, "qwen3:8b", 128)
    assert payload == {
        "model": "qwen3:8b",
        "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        "stream": False,
        "options": {
            "num_predict": 128,
            "temperature": 0.2,
            "top_p": 0.9,
            "seed": 7,
            "stop": ["END"],
            "presence_penalty": 0.1,
            "frequency_penalty": 0.3,
        },
    }


def test_tools_pass_through_unchanged():
    payload = to_ollama({"messages": [{"role": "user", "content": "x"}], "tools": TOOLS}, "m", 1)
    assert payload["tools"] == TOOLS


def test_tool_call_round_trip():
    # Ollama 回傳物件形式的 arguments → OpenAI 的 JSON 字串。
    reply = from_ollama(
        ollama_reply(tool_calls=[{"function": {"name": "get_weather", "arguments": {"city": "台北"}}}]), "m"
    )
    choice = reply["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["id"] == "call_0" and call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"city": "台北"}

    # 用戶端把助理的工具呼叫與工具結果送回來 → Ollama 的物件形式 arguments 與 tool_name。
    body = {
        "messages": [
            {"role": "user", "content": "天氣？"},
            {"role": "assistant", "content": None, "tool_calls": choice["message"]["tool_calls"]},
            {"role": "tool", "tool_call_id": "call_0", "content": "晴，25 度"},
            {"role": "tool", "tool_call_id": "call_unknown", "content": "?"},
        ],
        "tools": TOOLS,
    }
    messages = to_ollama(body, "m", 1)["messages"]
    assert messages[1] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "台北"}}}],
    }
    assert messages[2] == {"role": "tool", "content": "晴，25 度", "tool_name": "get_weather"}
    assert messages[3] == {"role": "tool", "content": "?"}


def test_tool_call_arguments_must_be_a_json_object():
    body = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "[1, 2]"}}]}
        ]
    }
    with pytest.raises(BackendError) as info:
        to_ollama(body, "m", 1)
    assert info.value.code == "bad_request"


def test_images_from_data_uri():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "這是什麼？"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                    {"type": "text", "text": "請簡短回答"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}},
                ],
            }
        ]
    }
    message = to_ollama(body, "m", 1)["messages"][0]
    assert message == {
        "role": "user",
        "content": "這是什麼？\n請簡短回答",
        "images": ["iVBORw0KGgo=", "/9j/4AAQ"],
    }


@pytest.mark.parametrize(
    "url",
    ["https://example.com/cat.png", "data:image/png,rawbytes", "data:image/png;base64,", None],
)
def test_non_data_image_url_is_bad_request(url):
    body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    with pytest.raises(BackendError) as info:
        to_ollama(body, "m", 1)
    assert info.value.code == "bad_request"


@pytest.mark.parametrize(
    "response_format, expected",
    [
        ({"type": "json_object"}, "json"),
        (
            {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object", "properties": {}}}},
            {"type": "object", "properties": {}},
        ),
    ],
)
def test_response_format(response_format, expected):
    payload = to_ollama({"messages": [{"role": "user", "content": "x"}], "response_format": response_format}, "m", 1)
    assert payload["format"] == expected


def test_response_format_text_adds_nothing_and_unknown_is_rejected():
    base = {"messages": [{"role": "user", "content": "x"}]}
    assert "format" not in to_ollama({**base, "response_format": {"type": "text"}}, "m", 1)
    with pytest.raises(BackendError):
        to_ollama({**base, "response_format": {"type": "xml"}}, "m", 1)
    with pytest.raises(BackendError):
        to_ollama({**base, "response_format": {"type": "json_schema", "json_schema": {}}}, "m", 1)


@pytest.mark.parametrize("messages", [None, [], ["hi"], [{"content": "no role"}]])
def test_bad_messages_are_bad_request(messages):
    with pytest.raises(BackendError) as info:
        to_ollama({"messages": messages}, "m", 1)
    assert info.value.code == "bad_request"


@pytest.mark.parametrize(
    "done_reason, tool_calls, expected",
    [
        ("stop", None, "stop"),
        ("length", None, "length"),
        (None, None, "stop"),
        ("stop", [{"function": {"name": "f", "arguments": {}}}], "tool_calls"),
        ("length", [{"function": {"name": "f", "arguments": {}}}], "tool_calls"),
    ],
)
def test_finish_reason(done_reason, tool_calls, expected):
    data = ollama_reply(content="hi")
    data["done_reason"] = done_reason
    if tool_calls:
        data["message"]["tool_calls"] = tool_calls
    assert from_ollama(data, "m")["choices"][0]["finish_reason"] == expected


def test_reply_shape_usage_and_thinking():
    reply = from_ollama(ollama_reply(content="答案是 4", thinking="2+2=4"), "qwen3:8b")
    assert reply["object"] == "chat.completion"
    assert reply["id"].startswith("chatcmpl-")
    assert reply["model"] == "qwen3:8b"
    assert isinstance(reply["created"], int)
    assert reply["choices"][0]["message"] == {
        "role": "assistant",
        "content": "答案是 4",
        "reasoning_content": "2+2=4",
    }
    assert reply["usage"] == {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34}


def test_no_reasoning_content_without_thinking():
    message = from_ollama(ollama_reply(content="x", thinking=""), "m")["choices"][0]["message"]
    assert "reasoning_content" not in message


@pytest.mark.parametrize("data", [{"error": "model not found"}, {"done": True}, [], "x"])
def test_bad_ollama_reply_is_backend_error(data):
    with pytest.raises(BackendError) as info:
        from_ollama(data, "m")
    assert info.value.code == "backend_error"
