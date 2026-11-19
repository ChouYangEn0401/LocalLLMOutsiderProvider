"""本機 OpenAI 相容代理：讓一般的 OpenAI 相容程式不必改動就能透過閘道使用 worker。

只綁 127.0.0.1（loopback），而且**沒有帳號驗證**：這台機器上任何能連到 loopback 的程式，都能以設定檔裡的
API 金鑰與簽章金鑰送出請求。正因如此，它拒絕綁到 loopback 以外的位址。

為了擋住瀏覽器裡的惡意網頁（跨站請求、DNS rebinding），每個請求都要通過：
- Host 必須剛好一個，而且是 127.0.0.1、localhost 或 [::1] 加上代理實際監聽的 port，否則 403。
- 帶 Origin 標頭，或 Sec-Fetch-Site 是 cross-site，一律 403（一般的 OpenAI 相容程式不會送這些）。
- 有本文的請求（POST）Content-Type 必須是 application/json，否則 415。

- GET  /v1/models：已釘選 worker 提供的模型，OpenAI 清單格式。
- POST /v1/chat/completions：一般的 OpenAI 請求。model 取自本文；max_tokens 取自 max_tokens 或
  max_completion_tokens，都沒有就用 default_max_tokens。封裝前會把 model、stream、stream_options、
  max_tokens、max_completion_tokens 從本文移除。

stream 為 true 時，閘道不做串流：代理先回 text/event-stream 標頭，等待期間每 15 秒寫一行註解
": waiting"（閘道回報排隊資訊時改寫 ": queue position=3 eta=40"），完成後把整則訊息放在一個 chat.completion.chunk 裡送出，再送一個帶 finish_reason 與 usage
的結尾 chunk，最後是 "data: [DONE]"。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from llmop.client import sdk
from llmop.client.config import ClientConfig
from llmop.client.sdk import ClientError
from llmop.protocol import MAX_BODY_BYTES

log = logging.getLogger("llmop.client.proxy")

LOOPBACK_HOST = "127.0.0.1"
# Host 標頭允許的主機名稱（再加上代理實際監聽的 port）。
ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")
KEEPALIVE_SECONDS = 15.0
# 封裝前要從本文移除的欄位：這些由代理決定，放進標頭或根本不送。
STRIPPED_FIELDS = ("model", "stream", "stream_options", "max_tokens", "max_completion_tokens")


@dataclass
class _State:
    config: ClientConfig
    signing_key: Ed25519PrivateKey
    keepalive: float
    deadline: float
    session: aiohttp.ClientSession | None = None


STATE = web.AppKey("llmop_client_proxy", _State)


# ---------------------------------------------------------------------------
# 錯誤
# ---------------------------------------------------------------------------


def error_payload(exc: ClientError) -> dict:
    """OpenAI 的錯誤形狀。"""
    kind = "invalid_request_error" if exc.status < 500 else "server_error"
    return {"error": {"message": exc.message or exc.code, "type": kind, "code": exc.code}}


def _error_response(exc: ClientError) -> web.Response:
    return web.json_response(error_payload(exc), status=exc.status)


def _internal_error() -> ClientError:
    return ClientError("internal_error", "代理發生內部錯誤", status=500)


# ---------------------------------------------------------------------------
# 本機防護（Host、Origin、Sec-Fetch-Site、Content-Type）
# ---------------------------------------------------------------------------


def _local_port(request: web.Request) -> int | None:
    sockname = request.transport.get_extra_info("sockname") if request.transport is not None else None
    if isinstance(sockname, tuple) and len(sockname) >= 2 and isinstance(sockname[1], int):
        return sockname[1]
    return None


def host_allowed(host: str, port: int | None) -> bool:
    """Host 標頭是否為 loopback 名稱加上代理的 port。port 是 80 時也接受省略 port 的寫法。"""
    if port is None:
        return False
    host = host.lower()
    for name in ALLOWED_HOSTNAMES:
        if host == f"{name}:{port}" or (port == 80 and host == name):
            return True
    return False


def _guard_error(request: web.Request) -> ClientError | None:
    hosts = request.headers.getall("Host", [])
    if len(hosts) != 1 or not host_allowed(hosts[0], _local_port(request)):
        return ClientError("forbidden_host", "Host 標頭必須是本機代理的位址", status=403)
    if "Origin" in request.headers:
        return ClientError("forbidden_origin", "代理不接受瀏覽器的跨來源請求（帶有 Origin）", status=403)
    if any(v.strip().lower() == "cross-site" for v in request.headers.getall("Sec-Fetch-Site", [])):
        return ClientError("forbidden_origin", "代理不接受跨站請求", status=403)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.content_type != "application/json":
        return ClientError("unsupported_media_type", "Content-Type 必須是 application/json", status=415)
    return None


@web.middleware
async def local_guard(request: web.Request, handler):
    error = _guard_error(request)
    if error is not None:
        log.warning("拒絕請求 method=%s code=%s", request.method, error.code)
        return _error_response(error)
    return await handler(request)


# ---------------------------------------------------------------------------
# 請求本文
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> None:
    raise ValueError(f"JSON 不允許 {name}")


async def _read_json(request: web.Request) -> dict:
    try:
        raw = await request.read()
    except web.HTTPRequestEntityTooLarge:
        raise ClientError("payload_too_large", "請求本文太大") from None
    try:
        body = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        raise ClientError("bad_request", "請求本文不是合法的 JSON", status=400) from None
    if not isinstance(body, dict):
        raise ClientError("bad_request", "請求本文必須是 JSON 物件", status=400)
    return body


def split_request(body: dict, default_max_tokens: int) -> tuple[dict, str, int, bool]:
    """把 OpenAI 請求拆成 (要封裝的本文, model, max_tokens, stream)。不修改傳入的 dict。"""
    rest = {k: v for k, v in body.items() if k not in STRIPPED_FIELDS}
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ClientError("bad_request", "缺少 model", status=400)
    stream = body.get("stream", False)
    if stream is None:
        stream = False
    if not isinstance(stream, bool):
        raise ClientError("bad_request", "stream 必須是布林值", status=400)
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = default_max_tokens
    # model 長度與 max_tokens 範圍由 sdk.chat 檢查。
    return rest, model, max_tokens, stream


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def completion_to_chunks(completion: dict, model: str) -> list[dict]:
    """把一個 chat.completion 轉成兩個 chat.completion.chunk：整則訊息一個、結尾一個。"""
    base = {
        "id": completion.get("id") or "chatcmpl-llmop",
        "object": "chat.completion.chunk",
        "created": completion.get("created") or int(time.time()),
        "model": completion.get("model") or model,
    }
    content_choices, final_choices = [], []
    choices = completion.get("choices")
    for position, choice in enumerate(choices if isinstance(choices, list) else []):
        if not isinstance(choice, dict):
            continue
        index = choice.get("index", position)
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        delta: dict = {"role": message.get("role") or "assistant"}
        if "content" in message:
            delta["content"] = message["content"]
        if message.get("reasoning_content") is not None:
            delta["reasoning_content"] = message["reasoning_content"]
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            # 串流格式的每個 tool_call 都必須有 index。
            delta["tool_calls"] = [
                {"index": n, **{k: v for k, v in call.items() if k != "index"}}
                for n, call in enumerate(tool_calls)
                if isinstance(call, dict)
            ]
        content_choices.append({"index": index, "delta": delta, "finish_reason": None})
        final_choices.append({"index": index, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"})
    final = {**base, "choices": final_choices}
    if "usage" in completion:
        final["usage"] = completion["usage"]
    return [{**base, "choices": content_choices}, final]


async def _write_event(resp: web.StreamResponse, data: dict | str) -> None:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    await resp.write(f"data: {text}\n\n".encode())


def keepalive_comment(queue: dict[str, int] | None) -> bytes:
    """保活註解：沒有排隊資訊就是 ": waiting"，有就是 ": queue position=3 eta=40"。值只會是整數。"""
    if not queue:
        return b": waiting\n\n"
    fields = " ".join(f"{name}={int(queue[name])}" for name in sdk.QUEUE_FIELDS if name in queue)
    return f": queue {fields}\n\n".encode()


async def _stream(
    request: web.Request, state: _State, call: Awaitable[dict], model: str, queue: dict
) -> web.StreamResponse:
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    task = asyncio.ensure_future(call)
    try:
        while not (await asyncio.wait({task}, timeout=state.keepalive))[0]:
            await resp.write(keepalive_comment(queue))
        try:
            completion = task.result()
        except ClientError as exc:
            await _write_event(resp, error_payload(exc))
        except Exception:
            log.exception("處理串流請求時發生未預期的錯誤")
            await _write_event(resp, error_payload(_internal_error()))
        else:
            for chunk in completion_to_chunks(completion, model):
                await _write_event(resp, chunk)
            await _write_event(resp, "[DONE]")
        await resp.write_eof()
    except ConnectionResetError:
        log.info("呼叫端在回覆完成前斷線")
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    return resp


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


async def handle_models(request: web.Request) -> web.Response:
    state = request.app[STATE]
    try:
        entries = await sdk.list_models(state.session, state.config)
    except ClientError as exc:
        return _error_response(exc)
    models = dict.fromkeys(entry.model for entry in entries)
    return web.json_response(
        {
            "object": "list",
            "data": [{"id": m, "object": "model", "created": 0, "owned_by": "llmop"} for m in models],
        }
    )


async def handle_chat(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    try:
        body, model, max_tokens, stream = split_request(await _read_json(request), state.config.default_max_tokens)
    except ClientError as exc:
        return _error_response(exc)
    # 閘道最近一次回報的排隊資訊；只有串流模式能轉給應用程式（保活註解）。
    queue: dict[str, int] = {}

    def on_queue(info: dict[str, int]) -> None:
        queue.clear()
        queue.update(info)

    call = sdk.chat(
        state.session,
        state.config,
        state.signing_key,
        body,
        model=model,
        max_tokens=max_tokens,
        deadline=state.deadline,
        on_queue=on_queue if stream else None,
    )
    if stream:
        return await _stream(request, state, call, model, queue)
    try:
        return web.json_response(await call)
    except ClientError as exc:
        return _error_response(exc)
    except Exception:
        log.exception("處理請求時發生未預期的錯誤")
        return _error_response(_internal_error())


async def _client_session(app: web.Application) -> AsyncIterator[None]:
    async with aiohttp.ClientSession() as session:
        app[STATE].session = session
        yield


def make_app(
    config: ClientConfig,
    signing_key: Ed25519PrivateKey,
    *,
    keepalive: float = KEEPALIVE_SECONDS,
    deadline: float = sdk.DEADLINE_SECONDS,
) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[local_guard])
    app[STATE] = _State(config=config, signing_key=signing_key, keepalive=keepalive, deadline=deadline)
    app.cleanup_ctx.append(_client_session)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat)
    return app


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------


def check_bind_host(host: str) -> None:
    """代理沒有帳號驗證，只能綁 loopback 位址；其他位址一律拒絕。"""
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"代理沒有驗證，只能綁在 loopback 位址（例如 {LOOPBACK_HOST}），拒絕綁到 {host!r}")


async def start_proxy(
    config: ClientConfig,
    signing_key: Ed25519PrivateKey,
    *,
    host: str = LOOPBACK_HOST,
    port: int | None = None,
    keepalive: float = KEEPALIVE_SECONDS,
) -> web.AppRunner:
    """在背景啟動代理並回傳 AppRunner；呼叫端負責 await runner.cleanup()。"""
    check_bind_host(host)
    runner = web.AppRunner(make_app(config, signing_key, keepalive=keepalive))
    await runner.setup()
    try:
        await web.TCPSite(runner, host, config.proxy_port if port is None else port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


def run_proxy(
    config: ClientConfig, signing_key: Ed25519PrivateKey, *, host: str = LOOPBACK_HOST, port: int | None = None
) -> None:
    """在前景執行代理，直到收到中斷訊號。"""
    check_bind_host(host)
    port = config.proxy_port if port is None else port

    def announce(_message: str) -> None:
        # run_app 在開始監聽之後才呼叫這個函式。
        print(f"本機代理已啟動：http://{host}:{port}/v1（沒有驗證，只接受本機連線）", flush=True)

    web.run_app(make_app(config, signing_key), host=host, port=port, print=announce)
