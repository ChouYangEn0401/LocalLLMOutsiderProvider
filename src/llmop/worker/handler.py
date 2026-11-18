"""處理閘道轉來的一筆請求：驗簽 → 重放檢查 → 解密 → 呼叫後端 → 封裝回覆。

步驟順序依 docs/SECURITY.md「開啟請求與封裝回覆」。解密成功之前，只回裸錯誤碼；
解密成功之後，所有結果（包括後端失敗）都封裝給用戶端的一次性回覆金鑰，明文訊框不帶任何後端文字。
紀錄只寫 request_id、sig_key_id、模型、後端、耗時與狀態碼，絕不寫明文、回覆、金鑰或權杖。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from llmop import protocol
from llmop.crypto import ReplySealer, open_request, signing_key_id, verify_request
from llmop.protocol import MAX_BODY_BYTES, Envelope, Header, ProtocolError, b64u_decode
from llmop.worker.backends import Backend, BackendError
from llmop.worker.keystore import KeyStore
from llmop.worker.replay import ReplayGuard

log = logging.getLogger(__name__)

# 封裝後的回覆加上訊框外殼必須放得進一則 WebSocket 訊息；預留一點空間給外殼。
_MAX_SEALED_CHARS = MAX_BODY_BYTES - 4096


@dataclass
class Handler:
    keys: KeyStore
    replay: ReplayGuard
    max_concurrency: int
    pinned_clients: frozenset[bytes] = frozenset()
    routes: dict[str, Backend] = field(default_factory=dict)  # 模型 → 後端，由連線端定期更新

    def __post_init__(self) -> None:
        self._slots = asyncio.Semaphore(self.max_concurrency)

    async def handle(self, frame: dict) -> dict:
        """frame 是已由 protocol.parse_frame 檢查過的 request 訊框；回傳要送回閘道的 response 訊框。"""
        request_id = frame["request_id"]
        started = time.monotonic()
        header: Header | None = None
        try:
            envelope = Envelope.from_json(frame["envelope"])
            header = envelope.header
            body, sealer = self._open(envelope, header, frame["client_sig_pub"])
        except ProtocolError as exc:
            self._log(request_id, header, None, exc.code, started)
            return protocol.response_error(request_id, exc.code)
        except Exception as exc:  # 不該發生；只記錄例外類型，避免任何內容進入紀錄
            log.error("request %s 解密前發生未預期的錯誤（%s）", request_id, type(exc).__name__)
            return protocol.response_error(request_id, "internal_error")

        reply, usage, backend_name = await self._call_backend(request_id, header, body)
        try:
            sealed = sealer.seal(reply)
            if len(sealed) > _MAX_SEALED_CHARS:
                reply, usage = _failure("backend_error", "回覆太大，超過單一訊息的上限"), (0, 0)
                sealed = sealer.seal(reply)
        except ProtocolError as exc:
            # 用戶端給的回覆公鑰無法使用：沒有辦法封裝，只能回裸錯誤碼。
            self._log(request_id, header, backend_name, exc.code, started)
            return protocol.response_error(request_id, exc.code)
        status = "ok" if reply["ok"] else reply["error"]["code"]
        self._log(request_id, header, backend_name, status, started)
        return protocol.response_ok(request_id, sealed, *usage)

    def _open(self, envelope: Envelope, header: Header, client_sig_pub: str) -> tuple[dict, ReplySealer]:
        """解密前的所有檢查，依規定順序；任何一步失敗都丟 ProtocolError。"""
        worker_key = self.keys.private_key(header.key_id)
        if worker_key is None:
            raise ProtocolError("unknown_key_id", "這台 worker 沒有這把金鑰")
        client_pub = b64u_decode(client_sig_pub)
        if len(client_pub) != 32 or signing_key_id(client_pub) != header.sig_key_id:
            raise ProtocolError("bad_signature", "簽章公鑰與標頭的 sig_key_id 不符")
        if self.pinned_clients and client_pub not in self.pinned_clients:
            raise ProtocolError("untrusted_client", "用戶端公鑰不在 pinned_clients 裡")
        verify_request(envelope, client_pub)
        self.replay.check(header)
        if header.model not in self.routes:
            raise ProtocolError("model_unavailable", "這台 worker 沒有提供這個模型")
        return open_request(envelope, worker_key)

    async def _call_backend(self, request_id: str, header: Header, body: dict) -> tuple[dict, tuple[int, int], str | None]:
        """回傳 (回覆明文, (prompt_tokens, completion_tokens), 後端名稱)。失敗時 token 數為 0。"""
        if "stream" in body and body["stream"] is not False:
            return _failure("bad_request", "不支援串流：stream 必須是 false 或省略"), (0, 0), None
        backend = self.routes.get(header.model)
        if backend is None:  # 解密期間模型清單剛好更新，模型消失了
            return _failure("model_unavailable", "這台 worker 已不再提供這個模型"), (0, 0), None
        queued = time.monotonic()
        async with self._slots:
            began = time.monotonic()
            try:
                completion = await backend.chat(body, header.model, header.max_tokens)
            except BackendError as exc:
                result = _failure(exc.code, exc.message), (0, 0)
            except Exception as exc:  # 翻譯或解析的程式錯誤：仍然封裝送回，紀錄只寫類型
                log.error("request %s 呼叫 %s 時發生未預期的錯誤（%s）", request_id, backend.name, type(exc).__name__)
                result = _failure("backend_error", "worker 處理後端回應時發生錯誤"), (0, 0)
            else:
                result = {"ok": True, "body": completion}, _usage(completion)
            log.debug(
                "request %s backend=%s queue_seconds=%.3f backend_seconds=%.3f",
                request_id,
                backend.name,
                began - queued,
                time.monotonic() - began,
            )
        return (*result, backend.name)

    @staticmethod
    def _log(request_id: str, header: Header | None, backend: str | None, status: str, started: float) -> None:
        level = logging.INFO if status == "ok" else logging.WARNING
        log.log(
            level,
            "request %s sig_key_id=%s model=%s backend=%s status=%s seconds=%.3f",
            request_id,
            header.sig_key_id if header else "-",
            header.model if header else "-",
            backend or "-",
            status,
            time.monotonic() - started,
        )


def _failure(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _usage(completion: dict) -> tuple[int, int]:
    """取出 completion 的 token 數，作為明文 usage；格式不對就當 0。"""
    usage = completion.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    counts = []
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name)
        counts.append(value if type(value) is int and value >= 0 else 0)
    return counts[0], counts[1]
