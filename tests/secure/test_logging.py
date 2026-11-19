"""紀錄裡不可以出現信封、封裝值、API 金鑰或權杖。

- 動態：由 B 的 tests/gateway/test_logging.py 移植，走一遍成功與錯誤路徑後檢查 DEBUG 紀錄。
- 靜態：沿用 B 的 tests/e2e/test_static_logging.py 的掃描器（同一套禁用字與規則），
  把範圍延伸到 src/llmop/secure/（不改動 tests/e2e）。
"""

from __future__ import annotations

import ast
import importlib.util
import logging
from pathlib import Path

from llmop import protocol

ROOT = Path(__file__).resolve().parents[2]
SECURE = ROOT / "src" / "llmop" / "secure"


def _static_scanner():
    path = ROOT / "tests" / "e2e" / "test_static_logging.py"
    spec = importlib.util.spec_from_file_location("_b_static_logging_for_secure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 例外清單，越短越好；鍵是 (相對於 src/llmop 的檔案, 名稱)。
ALLOWED = {
    # API 金鑰的公開代號（store 的 key id，不是金鑰本身），與 B 的 gateway/api.py 相同的例外。
    ("secure/api.py", "api_key_id"): "公開代號",
    ("secure/worker_ws.py", "api_key_id"): "公開代號",
}


def _sources() -> dict[str, str]:
    return {
        f"secure/{p.relative_to(SECURE).as_posix()}": p.read_text(encoding="utf-8")
        for p in sorted(SECURE.rglob("*.py"))
    }


def test_secure_modules_never_log_secrets_or_content():
    scanner = _static_scanner()
    total, problems, used = 0, [], set()
    for filename, source in _sources().items():
        for node in ast.walk(ast.parse(source, filename)):
            if not (isinstance(node, ast.Call) and scanner._is_output_call(node.func)):
                continue
            total += 1
            args = list(node.args) + [kw.value for kw in node.keywords
                                      if kw.arg not in ("file", "flush", "end", "sep")]
            for arg in args:
                for name in scanner._used_names(arg):
                    used.add((filename, name))
                    if scanner.FORBIDDEN.search(name) and (filename, name) not in ALLOWED:
                        problems.append(f"src/llmop/{filename}:{node.lineno}: {name!r} ← {ast.unparse(node)[:160]}")
        problems += scanner._exception_dumps(source, filename)
    assert total > 15, "沒有找到足夠的紀錄呼叫，檢查本身可能失效了"
    assert not problems, "紀錄或 print 的參數取用了秘密值或內容：\n" + "\n".join(problems)
    assert set(ALLOWED) <= used, f"多餘的例外：{sorted(set(ALLOWED) - used)}"


async def test_no_secrets_in_debug_log(make_gateway, caplog):
    caplog.set_level(logging.DEBUG)
    gw = await make_gateway(sync_wait_seconds=0.05)
    worker = await gw.connect_worker(silent=True)
    api_client = gw.new_api_client()
    envelope, opener = api_client.seal(worker.public)

    status, data = await gw.post(api_client, envelope)
    assert status == 202
    await worker.wait_received(1)
    await worker.answer(worker.received[0])
    status, data = await gw.poll(api_client, envelope.header.request_id)
    assert status == 200
    assert opener.open(data["sealed"])["ok"] is True

    # 也走一遍幾條錯誤路徑：重複、錯的金鑰、握手失敗（錯的權杖與註冊碼）。
    assert (await gw.post(api_client, envelope))[0] == 409
    status, _, _ = await gw.post_raw(json_body=envelope.to_json(),
                                     headers={"Authorization": "Bearer " + api_client.api_key + "x"})
    assert status == 401
    _, reg_token = gw.store.create_worker("w2")
    ws = await gw.ws_connect()
    await ws.send_json(protocol.hello(worker.worker_id, worker.worker_token + "x",
                                      [{"key_id": worker.key_id,
                                        "public_key": protocol.b64u_encode(worker.public)}], ["m1"], 1))
    assert (await ws.receive_json(timeout=5))["code"] == "unauthorized"
    await ws.close()
    ws = await gw.ws_connect()
    await ws.send_json(protocol.register(reg_token))
    assert (await ws.receive_json(timeout=5))["type"] == "registered"
    await ws.close()
    await worker.close()

    text = caplog.text
    assert envelope.header.request_id in text  # 確認紀錄確實有被收集
    for secret in (
        envelope.sealed, envelope.protected, envelope.sig, data["sealed"],
        api_client.api_key, worker.worker_token, reg_token, "秘密的問題",
    ):
        assert secret not in text
