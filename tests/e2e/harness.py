"""端對端測試的組件：錄音轉送器、假後端、假 cloudflared、假閘道、網頁用戶端，以及整套跑起來的系統。

v0.2 的整套系統（Stack）：

                         ┌──────────── 統一節點（真的 llmop.node.Node，三個 uvicorn）────────────┐
    瀏覽器／管理員 ──────▶ local      127.0.0.1:<local>   本機控制台 + 免 key 的 /v1             │
    帳號網站使用者 ──────▶ public-ui  127.0.0.1:<ui>      登入、入口網頁（自己的 key）            │
                         │ public-api 127.0.0.1:<api>     /<gate>/v1、/<gate>/e2e/v1、/<gate>/worker/ws
                         └──────────────────────────────▲────────────────────────────────────────┘
                                                        │
    測試 ──HTTP──▶ 本機代理（真的 llmop.client）──HTTP──▶ 錄音轉送器（隧道的位置）
                                                        ▲
    假 Ollama ◀──┐                                      │ WebSocket（worker 主動連出）
    假 OpenAI ◀──┴── worker（真的 llmop.worker）─────────┘

錄音轉送器扮演 Cloudflare API 隧道的位置：用戶端與 worker 都只連它，它把位元組原封不動轉給
public-api 入口，同時記下閘道收到與送出的每一個位元組。WebSocket 的內容另外解框（去遮罩、
解 permessage-deflate），因為原始位元組裡 worker 送出的訊框有遮罩、也可能被壓縮。

假 cloudflared（FakeCloudflared）是一個小的 HTTP 反向代理：從 127.0.0.1 連進入口，但像真的
cloudflared 一樣加上 ``Cf-Connecting-Ip``、``X-Forwarded-*``，並把 ``Host`` 改成公開網址的主機名。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from llmop import paths, protocol, security
from llmop.auth import SESSION_COOKIE
from llmop.client.config import ClientConfig
from llmop.client.proxy import make_app as make_proxy_app
from llmop.protocol import Envelope
from llmop.worker.config import load_config as load_worker_config
from llmop.worker.keystore import load_keys
from llmop.worker.link import serve as serve_worker

ROOT_PASSWORD = "e2e 管理員密碼 correct horse"
ALICE_PASSWORD = "e2e alice 的密碼 battery staple"
# scrypt 用很小的 n，測試才不會花時間在雜湊上（格式與正式的雜湊相同）。
ROOT_HASH = security.hash_password(ROOT_PASSWORD, n=2**4)
ALICE_HASH = security.hash_password(ALICE_PASSWORD, n=2**4)

OLLAMA_MODEL = "e2e-ollama:8b"
OPENAI_MODEL = "e2e-openai-7b"
MODELS = {"ollama": OLLAMA_MODEL, "openai": OPENAI_MODEL}
DEFAULT_REPLY = "這是假後端的回答。"
# 假後端回報的 token 數（輸入, 輸出），閘道會把它們加進用量。
FAKE_USAGE = {"ollama": (11, 5), "openai": (13, 6)}
CSRF_HEADER = "X-CSRF-Token"

# 假 cloudflared 加上的標頭（真的 cloudflared 一定會加，外面的人拿不掉）。
CF_IP = "203.0.113.7"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_until(predicate: Callable[[], object | Awaitable[object]], timeout: float = 10.0,
                     interval: float = 0.02, what: str = "條件") -> object:
    """反覆檢查 predicate（可以是 async），直到結果為真；逾時就讓測試失敗。"""
    try:
        async with asyncio.timeout(timeout):
            while True:
                result = predicate()
                if asyncio.iscoroutine(result):
                    result = await result
                if result:
                    return result
                await asyncio.sleep(interval)
    except TimeoutError:
        raise AssertionError(f"等待{what}逾時（{timeout} 秒）") from None


def parse_key_blocks(console: str) -> list[dict]:
    """從 `llmop worker init/show-key/rotate-key` 的主控台輸出取出 key_id 與公鑰（依出現順序）。"""
    pattern = re.compile(r"key_id\s*:\s*(\S+)\s*\n\s*public_key\s*:\s*(\S+)")
    return [{"key_id": m.group(1), "public_key": m.group(2)} for m in pattern.finditer(console)]


def parse_sse(text: str) -> tuple[list[object], list[str]]:
    """把 text/event-stream 拆成 (data 事件, 註解行)。data 是 JSON 就解析，否則保留字串（例如 [DONE]）。"""
    events: list[object] = []
    comments: list[str] = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                data = line[len("data: "):]
                try:
                    events.append(json.loads(data))
                except ValueError:
                    events.append(data)
            elif line.startswith(":"):
                comments.append(line)
    return events, comments


# ---------------------------------------------------------------------------
# 解析錄下來的位元組：HTTP/1.1 與 WebSocket
# ---------------------------------------------------------------------------


@dataclass
class HttpMessage:
    start_line: str  # 請求行（"POST /v1/... HTTP/1.1"）或狀態行（"HTTP/1.1 200 OK"）
    headers: dict[str, str]
    body: bytes

    @property
    def status(self) -> int:
        return int(self.start_line.split(" ")[1])

    @property
    def method_path(self) -> tuple[str, str]:
        method, path, _ = self.start_line.split(" ", 2)
        return method, path


def parse_http(data: bytes) -> list[HttpMessage]:
    """把一條連線單一方向的位元組拆成 HTTP/1.1 訊息（支援 Content-Length 與 chunked）。"""
    return _parse_http(data)[0]


def _parse_http(data: bytes) -> tuple[list[HttpMessage], int | None]:
    """回傳 (訊息, 升級點)。遇到 WebSocket 升級請求或 101 回應就停，升級點是它結束的位置，之後是訊框。"""
    messages: list[HttpMessage] = []
    pos = 0
    while pos < len(data):
        end = data.find(b"\r\n\r\n", pos)
        if end < 0:
            break
        lines = data[pos:end].decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        pos = end + 4
        body = bytearray()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                eol = data.find(b"\r\n", pos)
                if eol < 0:
                    break
                size = int(data[pos:eol].split(b";")[0], 16)
                pos = eol + 2
                if size == 0:
                    pos += 2  # 沒有 trailer，只剩最後的空行
                    break
                body += data[pos:pos + size]
                pos += size + 2
        else:
            length = int(headers.get("content-length", "0"))
            body += data[pos:pos + length]
            pos += length
        message = HttpMessage(lines[0], headers, bytes(body))
        messages.append(message)
        if message.start_line.startswith("HTTP/"):
            if message.status == 101:
                return messages, pos
        elif headers.get("upgrade", "").lower() == "websocket":
            return messages, pos
    return messages, None


def ws_messages(data: bytes, *, deflate: bool) -> list[bytes]:
    """把 WebSocket 一個方向的原始訊框還原成訊息內容：去遮罩、組合分段、解 permessage-deflate。

    控制訊框（ping/pong/close）略過。permessage-deflate 每個方向共用一個解壓縮狀態（context takeover）；
    就算對方每則訊息都重設壓縮狀態，連續解壓縮也一樣正確。
    """
    messages: list[bytes] = []
    inflater = zlib.decompressobj(-15)
    parts: list[bytes] = []
    compressed = False
    pos = 0
    while pos + 2 <= len(data):
        b0, b1 = data[pos], data[pos + 1]
        fin, rsv1, opcode = b0 & 0x80, b0 & 0x40, b0 & 0x0F
        masked, length = b1 & 0x80, b1 & 0x7F
        pos += 2
        if length == 126:
            length = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2
        elif length == 127:
            length = int.from_bytes(data[pos:pos + 8], "big")
            pos += 8
        mask = b""
        if masked:
            mask = data[pos:pos + 4]
            pos += 4
        payload = data[pos:pos + length]
        pos += length
        if len(payload) < length:
            break  # 連線在訊框中途被切斷
        if masked:
            key = (mask * (length // 4 + 1))[:length]
            payload = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(length, "big")
        if opcode >= 0x8:
            continue
        if opcode != 0x0:  # 新訊息的第一個訊框
            parts, compressed = [], bool(rsv1) and deflate
        parts.append(payload)
        if fin:
            message = b"".join(parts)
            if compressed:
                message = inflater.decompress(message + b"\x00\x00\xff\xff")
            messages.append(message)
    return messages


# ---------------------------------------------------------------------------
# 錄音轉送器（扮演 API 隧道）
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class RecordedConnection:
    """一條經過轉送器的 TCP 連線。to_gateway 是閘道收到的位元組，from_gateway 是閘道送出的位元組。"""

    to_gateway: bytearray = field(default_factory=bytearray)
    from_gateway: bytearray = field(default_factory=bytearray)
    writers: list[asyncio.StreamWriter] = field(default_factory=list, repr=False)

    @property
    def is_websocket(self) -> bool:
        return self._upgrade() is not None

    @property
    def compressed(self) -> bool:
        """WebSocket 是否協商了 permessage-deflate。"""
        upgrade = self._upgrade()
        return upgrade is not None and upgrade[0]

    def _upgrade(self) -> tuple[bool, bytes, bytes] | None:
        """這條連線若升級成 WebSocket（不一定是第一個請求），回傳 (是否壓縮, 閘道收到的訊框位元組, 閘道送出的)。"""
        responses, resp_end = _parse_http(bytes(self.from_gateway))
        if resp_end is None:
            return None
        _, req_end = _parse_http(bytes(self.to_gateway))
        deflate = "permessage-deflate" in responses[-1].headers.get("sec-websocket-extensions", "").lower()
        return deflate, bytes(self.to_gateway[req_end or len(self.to_gateway):]), bytes(self.from_gateway[resp_end:])

    def ws_frames(self) -> tuple[list[dict], list[dict]]:
        """(閘道收到的訊框, 閘道送出的訊框)，已解析成 JSON。"""
        upgrade = self._upgrade()
        if upgrade is None:
            return [], []
        deflate, received, sent = upgrade
        return (
            [json.loads(m) for m in ws_messages(received, deflate=deflate)],
            [json.loads(m) for m in ws_messages(sent, deflate=deflate)],
        )

    def ws_payloads(self) -> list[bytes]:
        upgrade = self._upgrade()
        if upgrade is None:
            return []
        deflate, received, sent = upgrade
        return ws_messages(received, deflate=deflate) + ws_messages(sent, deflate=deflate)

    def http_exchanges(self) -> list[tuple[HttpMessage, HttpMessage | None]]:
        requests = _parse_http(bytes(self.to_gateway))[0]
        responses = _parse_http(bytes(self.from_gateway))[0]
        return [(req, responses[i] if i < len(responses) else None) for i, req in enumerate(requests)]


class RecordingRelay:
    """在 public-api 入口前面的 TCP 轉送器：轉送並記錄每一條連線的雙向位元組，不改動任何內容。

    retarget() 讓之後的新連線改接到另一個埠（例如假閘道）；sever_websockets() 直接中斷 worker 的連線，
    效果和隧道斷線或 worker 主機斷電時一樣（沒有 WebSocket 關閉訊框）。
    """

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.connections: list[RecordedConnection] = []
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def retarget(self, port: int) -> None:
        self.target_port = port

    def sever_websockets(self) -> int:
        count = 0
        for conn in self.connections:
            if conn.is_websocket and conn.writers and not conn.writers[0].transport.is_closing():
                for writer in conn.writers:
                    writer.transport.abort()
                count += 1
        return count

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._tasks.add(asyncio.current_task())
        conn = RecordedConnection()
        self.connections.append(conn)
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            writer.transport.abort()
            self._tasks.discard(asyncio.current_task())
            return
        conn.writers = [writer, up_writer]
        try:
            await asyncio.gather(
                self._pump(reader, up_writer, conn.to_gateway),
                self._pump(up_reader, writer, conn.from_gateway),
            )
        finally:
            self._tasks.discard(asyncio.current_task())

    @staticmethod
    async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, record: bytearray) -> None:
        try:
            while data := await reader.read(65536):
                record += data
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            # 任一方向結束就整條關掉；另一個方向的 read 會跟著收到 EOF。
            writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for conn in self.connections:
            for writer in conn.writers:
                writer.transport.abort()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- 查詢錄音 ----------------------------------------------------------

    def blobs(self) -> list[bytes]:
        """所有錄到的位元組：每條連線的原始雙向位元組，加上解框後的 WebSocket 訊息內容。"""
        out: list[bytes] = []
        for conn in self.connections:
            out += [bytes(conn.to_gateway), bytes(conn.from_gateway)]
            out += conn.ws_payloads()
        return out

    def http_exchanges(self) -> list[tuple[HttpMessage, HttpMessage | None]]:
        """所有連線上的 HTTP 請求與回應（升級成 WebSocket 之前的部分）。"""
        return [ex for conn in self.connections for ex in conn.http_exchanges()]

    def posted_envelopes(self) -> list[Envelope]:
        """閘道從用戶端收到的每一個信封（POST /<gate>/e2e/v1/chat/completions 的本文）。"""
        return [
            Envelope.from_json(json.loads(req.body))
            for req, _ in self.http_exchanges()
            if req.method_path[0] == "POST" and req.method_path[1].endswith("/e2e/v1/chat/completions")
        ]

    def ws_frames(self) -> tuple[list[dict], list[dict]]:
        """(閘道從 worker 收到的訊框, 閘道送給 worker 的訊框)，所有連線合併。"""
        received: list[dict] = []
        sent: list[dict] = []
        for conn in self.connections:
            r, s = conn.ws_frames()
            received += r
            sent += s
        return received, sent


# ---------------------------------------------------------------------------
# 假 cloudflared：從 loopback 連進入口，帶上 Cloudflare 的轉送標頭與公開的 Host
# ---------------------------------------------------------------------------

_HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te",
               "trailer", "host", "content-length"}


class FakeCloudflared:
    """一個 HTTP 反向代理，行為像 cloudflared：把每個請求轉給 127.0.0.1:<target_port>，
    並加上 ``Cf-Connecting-Ip``、``Cf-Ray``、``X-Forwarded-For``、``X-Forwarded-Proto: https``，
    ``Host`` 改成 ``public_host``。cookie 等其他標頭原樣轉送（攻擊者可以帶任何 cookie）。"""

    def __init__(self, target_port: int, public_host: str) -> None:
        self.target_port = target_port
        self.public_host = public_host
        self.url = ""
        self.seen: list[tuple[str, str, int]] = []
        self._session: aiohttp.ClientSession | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._forward)

        async def session_ctx(_app):
            async with aiohttp.ClientSession(auto_decompress=False) as session:
                self._session = session
                yield

        app.cleanup_ctx.append(session_ctx)
        return app

    async def _forward(self, request: web.Request) -> web.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers.update({
            "Host": self.public_host,
            "Cf-Connecting-Ip": CF_IP,
            "Cf-Ray": "8c0ffee0e2e00000-TPE",
            "X-Forwarded-For": CF_IP,
            "X-Forwarded-Proto": "https",
            "Cdn-Loop": "cloudflare",
        })
        body = await request.read()
        url = f"http://127.0.0.1:{self.target_port}{request.rel_url}"
        async with self._session.request(request.method, url, headers=headers, data=body or None,
                                         allow_redirects=False) as resp:
            data = await resp.read()
            out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP]
            self.seen.append((request.method, request.path, resp.status))
            response = web.Response(status=resp.status, body=data)
            for k, v in out_headers:
                response.headers.add(k, v)
            return response


# ---------------------------------------------------------------------------
# 假的本機 LLM 後端
# ---------------------------------------------------------------------------


class FakeBackend:
    """假的 Ollama（/api/tags、/api/show、/api/chat，加上 Ollama 也有的 OpenAI 相容 /v1/*）
    或 OpenAI 相容伺服器（/v1/models、/v1/chat/completions）。

    節點（A 的後端轉接層）走 OpenAI 相容的 /v1/chat/completions；worker（B 的後端）對 Ollama 走
    /api/chat。reply 是回答的文字；delay 秒後才回答（release 事件可以提早放行，測試結束時一律放行）。
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.model = MODELS[kind]
        self.reply = DEFAULT_REPLY
        self.delay = 0.0
        self.usage = FAKE_USAGE[kind]
        self.calls: list[dict] = []  # 每一筆生成請求的本文（不論路徑）
        self.paths: list[str] = []  # 對應的路徑
        self.release = asyncio.Event()
        self.base_url = ""

    def app(self) -> web.Application:
        app = web.Application()
        if self.kind == "ollama":
            app.router.add_get("/api/tags", self._tags)
            app.router.add_post("/api/show", self._show)
            app.router.add_post("/api/chat", self._chat)
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/chat/completions", self._chat)
        return app

    async def _tags(self, request: web.Request) -> web.Response:
        return web.json_response({"models": [{"name": self.model, "model": self.model}]})

    async def _show(self, request: web.Request) -> web.Response:
        return web.json_response({"model_info": {"llama.context_length": 8192}})

    async def _models(self, request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": self.model, "object": "model"}]})

    async def _chat(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.calls.append(payload)
        self.paths.append(request.path)
        if self.delay:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.release.wait(), self.delay)
        prompt_tokens, completion_tokens = self.usage
        if request.path == "/api/chat":
            return web.json_response({
                "model": payload["model"],
                "created_at": "2026-11-18T00:00:00Z",
                "message": {"role": "assistant", "content": self.reply},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": prompt_tokens,
                "eval_count": completion_tokens,
            })
        return web.json_response({
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", self.model),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens},
        })


# ---------------------------------------------------------------------------
# 假閘道：讓測試「繞過真的閘道」直接把訊框送給 worker
# ---------------------------------------------------------------------------


class FakeGateway:
    """接受任何 hello 的假 worker 端點（任何路徑，因為 worker 連的是 /<gate>/worker/ws）。
    扮演被入侵或冒充的閘道，把任意 request 訊框直接送給 worker。"""

    def __init__(self) -> None:
        self.hello: dict | None = None
        self.ws: web.WebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future] = {}

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._handle)
        return app

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            frame = protocol.parse_frame(msg.data)
            if frame["type"] == "hello":
                self.hello = frame
                await ws.send_json(protocol.ready())
                self.ws = ws
            elif frame["type"] == "response":
                future = self._pending.pop(frame["request_id"], None)
                if future is not None and not future.done():
                    future.set_result(frame)
        return ws

    async def send_request(self, envelope: Envelope, client_sig_pub: str, timeout: float = 10.0) -> dict:
        """像閘道一樣轉送一個信封，回傳 worker 的 response 訊框。"""
        request_id = envelope.header.request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.ws.send_json(protocol.request(request_id, envelope, client_sig_pub))
        return await asyncio.wait_for(future, timeout)


# ---------------------------------------------------------------------------
# 網頁用戶端：本機控制台與帳號網站（跟網頁做的事一樣：登入、CSRF 標頭、JSON 本文）
# ---------------------------------------------------------------------------


class WebClient:
    """登入某個入口（本機控制台或帳號網站）後，以 /ui/api/* 操作。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        # cookie 的網域是 IP 位址（127.0.0.1），aiohttp 預設的 cookie jar 不收，所以要 unsafe=True。
        # 每個入口各用一個 session：瀏覽器的 cookie 不分 port，但測試要分清楚是哪個入口發的。
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.csrf = ""

    async def close(self) -> None:
        await self.session.close()

    @property
    def session_id(self) -> str:
        for cookie in self.session.cookie_jar:
            if cookie.key == SESSION_COOKIE:
                return cookie.value
        return ""

    async def login(self, username: str, password: str) -> dict:
        async with self.session.post(self.base_url + "/ui/api/login",
                                     json={"username": username, "password": password}) as resp:
            data = await resp.json()
            assert resp.status == 200, data
        self.csrf = data["csrf"]
        return data

    async def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        kwargs: dict = {}
        if method != "GET":
            kwargs = {"json": body or {}, "headers": {CSRF_HEADER: self.csrf}}
        async with self.session.request(method, self.base_url + path, **kwargs) as resp:
            try:
                return resp.status, await resp.json(content_type=None)
            except ValueError:
                return resp.status, {"text": await resp.text()}

    # -- 控制台（管理員） --------------------------------------------------

    async def overview(self) -> dict:
        status, data = await self.call("GET", "/ui/api/admin/overview")
        assert status == 200, data
        return data

    async def create_worker(self, name: str) -> tuple[dict, str]:
        status, data = await self.call("POST", "/ui/api/admin/workers", {"name": name})
        assert status == 201, data
        return data["worker"], data["reg_token"]

    async def workers(self) -> list[dict]:
        return (await self.overview())["workers"]

    async def worker(self, worker_id: str) -> dict:
        return next(w for w in await self.workers() if w["id"] == worker_id)

    async def create_guest_key(self, label: str, budget_tokens: int, **extra) -> tuple[dict, str]:
        status, data = await self.call("POST", "/ui/api/admin/guest-keys",
                                       {"label": label, "budget_tokens": budget_tokens, **extra})
        assert status == 201, data
        return data["record"], data["key"]

    # -- 帳號網站（使用者自己的 key） ------------------------------------------

    async def create_key(self, name: str, *, sig_public_key: str | None = None,
                         encryption: str = "optional") -> tuple[dict, str]:
        status, data = await self.call("POST", "/ui/api/keys", {
            "name": name, "sig_public_key": sig_public_key, "encryption": encryption})
        assert status == 201, data
        return data["record"], data["key"]

    async def portal(self) -> dict:
        status, data = await self.call("GET", "/ui/api/portal")
        assert status == 200, data
        return data

    async def revoke_key(self, key_id: str) -> None:
        status, data = await self.call("DELETE", f"/ui/api/keys/{key_id}")
        assert status == 200, data


# ---------------------------------------------------------------------------
# 假的通道（TunnelManager 的替身；不啟動 cloudflared）
# ---------------------------------------------------------------------------


class FakeTunnel:
    """跟 llmop.tunnel.TunnelManager 介面相同的替身：start() 立刻拿到一個 trycloudflare 網址。"""

    counter = 0

    def __init__(self, settings, local_url, store=None, data_dir=None, **kw) -> None:
        FakeTunnel.counter += 1
        self.settings, self.local_url, self.kw = settings, local_url, kw
        self.name = kw.get("name")
        self.mode = settings.mode
        self.status, self.url = "off", None
        self.starts = self.stops = 0
        self.host = f"{self.name}-e2e-{FakeTunnel.counter}.trycloudflare.com"

    def start(self) -> bool:
        self.starts += 1
        self.status = "ready"
        self.url = f"https://{self.host}"
        return True

    def stop(self, timeout: float = 15.0) -> None:
        self.stops += 1
        self.status, self.url = "stopped", None


# ---------------------------------------------------------------------------
# worker（在同一個事件迴圈裡跑真的 llmop.worker.link.serve）
# ---------------------------------------------------------------------------


class WorkerRunner:
    """啟動與停止真的 worker。每次 start() 都從磁碟重新讀取 config.json 與 keys.json，就像重新啟動程序。

    `llmop worker run` 用 asyncio.run 包住 serve()，不能在測試的事件迴圈裡呼叫，所以這裡直接呼叫 serve()，
    其餘（設定、金鑰、state.json、與閘道的連線）都是同一套程式。
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.task: asyncio.Task | None = None

    def start(self, reg_token: str | None = None) -> asyncio.Task:
        assert self.task is None or self.task.done(), "worker 已經在執行"
        config = load_worker_config(self.home / "config.json")
        keys = load_keys(self.home / "keys.json")
        self.task = asyncio.create_task(serve_worker(
            config, keys, self.home, reg_token,
            # worker 的開機門檻拒絕 ts（整數秒）早於啟動時刻的請求；在啟動的同一秒內封裝的請求，
            # ts 會被截掉小數而早於啟動時刻。讓 worker 以為自己早一秒啟動，測試就不必等到下一秒。
            clock=lambda: time.time() - 1.0,
            min_backoff=0.05, max_backoff=0.2, refresh_seconds=3600,
        ))
        return self.task

    async def stop(self) -> None:
        """停止 worker（取消整個 serve）。"""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    @property
    def state(self) -> dict:
        return paths.read_json(self.home / "state.json")


# ---------------------------------------------------------------------------
# 用戶端身分
# ---------------------------------------------------------------------------


@dataclass
class ClientIdentity:
    signing_key: Ed25519PrivateKey = field(repr=False)
    public_key: str  # `llmop client keygen` 印出的 Ed25519 公鑰
    sig_key_id: str
    api_key: str = field(default="", repr=False)
    api_key_id: str = ""

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass
class Stack:
    """一整套跑起來的系統。欄位說明見模組開頭的圖。"""

    node: Any  # llmop.node.Node
    config_path: Path
    home: Path  # LLMOP_HOME（worker／client 的檔案）
    relay: RecordingRelay  # API 隧道的位置：用戶端與 worker 都經過這裡
    console: WebClient  # 管理員（root）登入本機控制台
    portal: WebClient  # alice 登入帳號網站
    backends: dict[str, FakeBackend]
    worker: WorkerRunner
    worker_id: str
    reg_token: str = field(repr=False)
    worker_keys: list[dict]  # worker 主控台印出的金鑰（第一把是目前的）
    client: ClientIdentity
    proxy_url: str
    http: aiohttp.ClientSession
    start_proxy: Callable[..., Awaitable[str]] = field(repr=False)
    start_cloudflared: Callable[..., Awaitable[FakeCloudflared]] = field(repr=False)
    restart_node: Callable[[], Awaitable[None]] = field(repr=False)

    @property
    def state(self):
        return self.node.state

    @property
    def cfg(self):
        return self.node.cfg

    @property
    def data_dir(self) -> Path:
        return self.cfg.data_path

    @property
    def gate(self) -> str:
        return self.state.api_link.gate

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.listeners.local}"

    @property
    def ui_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.listeners.public_ui}"

    @property
    def api_url(self) -> str:
        """public-api 入口（直接連，不經過錄音轉送器），不含 gate。"""
        return f"http://127.0.0.1:{self.cfg.listeners.public_api}"

    @property
    def gateway_url(self) -> str:
        """直接連閘道的 API 連結（含 gate）；測試直接重送封包用。"""
        return f"{self.api_url}/{self.gate}"

    @property
    def tunnel_url(self) -> str:
        """經過錄音轉送器（隧道）的 API 連結（含 gate）。"""
        return f"{self.relay.url}/{self.gate}"

    @property
    def hub(self):
        return self.state.hub

    def set_sync_wait(self, seconds: float) -> None:
        """閘道同步等待 worker 的秒數（之後回 202 pending）。"""
        self.state.secure_router.secure.sync_wait_seconds = seconds

    async def chat(self, model: str, prompt: str = "你好", *, proxy_url: str | None = None, stream: bool = False,
                   max_tokens: int | None = 77, **extra) -> tuple[int, object, str]:
        """以一般的 OpenAI 請求呼叫本機代理，回傳 (HTTP 狀態, JSON 或 SSE 文字, Content-Type)。"""
        body: dict = {"model": model, "messages": [{"role": "user", "content": prompt}], **extra}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if stream:
            body["stream"] = True
        async with self.http.post((proxy_url or self.proxy_url) + "/v1/chat/completions", json=body) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype.startswith("text/event-stream"):
                return resp.status, await resp.text(), ctype
            return resp.status, await resp.json(), ctype

    async def post_envelope(self, envelope: Envelope, *, api_key: str | None = None) -> tuple[int, dict]:
        """直接把信封 POST 給閘道（不經過代理）。"""
        headers = {"Authorization": f"Bearer {api_key or self.client.api_key}"}
        async with self.http.post(self.gateway_url + "/e2e/v1/chat/completions", json=envelope.to_json(),
                                  headers=headers) as resp:
            return resp.status, await resp.json()

    async def plain(self, method: str, path: str, *, api_key: str, base: str | None = None,
                    json_body: dict | None = None) -> tuple[int, dict]:
        """明文 /v1（A 的直連面）：``base`` 預設是經過隧道的 API 連結。"""
        headers = {"Authorization": f"Bearer {api_key}"}
        async with self.http.request(method, (base or self.tunnel_url) + path, json=json_body,
                                     headers=headers) as resp:
            try:
                return resp.status, await resp.json(content_type=None)
            except ValueError:
                return resp.status, {"text": await resp.text()}

    async def announced_keys(self) -> set[str]:
        """閘道 /e2e/v1/models 目前公告的 worker key_id（就是用戶端看得到的）。"""
        async with self.http.get(self.gateway_url + "/e2e/v1/models", headers=self.client.auth) as resp:
            data = await resp.json()
        return {entry["llmop"]["key_id"] for entry in data["data"]}

    async def wait_announced(self, key_ids: set[str]) -> None:
        async def check() -> bool:
            return await self.announced_keys() == key_ids
        await wait_until(check, what=f" worker 公告金鑰 {sorted(key_ids)}")

    async def wait_online(self, online: bool = True) -> None:
        async def check() -> bool:
            return (await self.console.worker(self.worker_id))["online"] is online
        await wait_until(check, what=" worker 上線" if online else " worker 離線")

    def gateway_files(self) -> dict[str, bytes]:
        """節點資料目錄裡的每一個檔案（llmop.sqlite3 以及 SQLite 可能留下的 -journal/-wal、gate 檔）。"""
        return {p.name: p.read_bytes() for p in self.data_dir.rglob("*") if p.is_file()}


def proxy_app(config: ClientConfig, signing_key: Ed25519PrivateKey, keepalive: float = 0.1) -> web.Application:
    """真的本機代理。keepalive 調短，串流測試才看得到等待中的註解行。"""
    return make_proxy_app(config, signing_key, keepalive=keepalive, deadline=60)
