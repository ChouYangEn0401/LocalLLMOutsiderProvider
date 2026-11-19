"""靜態檢查：src/ 裡的紀錄與 print 呼叫，不可以把秘密值或內容當成參數（docs/SECURITY.md 檢討第 13 項）。

檢查方式：找出每一個 log.xxx(...)、logger.xxx(...)、logging.xxx(...) 與 print(...) 呼叫，看它的參數實際「取用」了哪些名稱：
- 變數 `x` → x；屬性 `a.b.c` → c；`frame["sealed"]` → sealed（取的是哪個欄位，不是容器叫什麼）。
- `len(...)` 與 `type(...)` 只得到大小與型別，不算取用內容。
- 字串常數（例如格式字串）不算。
名稱裡含有下列任何一個字（不分大小寫）就算違規。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# 計畫列出的字：sealed、protected、envelope、api_key、token、password、private、plaintext、body。
# 另外加上常拿來裝內容的名稱：訊框、後端本文、回覆、提示、錯誤訊息（後端錯誤訊息可能引用提示）等。
FORBIDDEN = re.compile(
    r"sealed|protected|envelope|api_key|token|password|private|plaintext|body"
    r"|frame|payload|completion|reply|prompt|secret|message|content|detail|data|text|raw",
    re.IGNORECASE,
)

# 明確的例外，越短越好；每一項都要說明為什麼安全。鍵是 (相對於 src/llmop 的檔案, 名稱)。
ALLOWED = {
    # API 金鑰的公開代號（k_ 加 12 個十六進位字元），docs/PROTOCOL.md 1.1 明定可以寫進紀錄。
    ("secure/api.py", "api_key_id"): "公開代號",
    ("secure/worker_ws.py", "api_key_id"): "公開代號",
    # `llmop client chat` 的用途就是把解開後的回答印給使用者本人看；這發生在用戶端本機，不經過閘道。
    ("client/cli.py", "content"): "用戶端顯示回答",
}

LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
SIZE_OR_TYPE = {"len", "type"}


def _is_output_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "print"
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        base = func.value
        name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
        return "log" in name.lower()
    return False


def _used_names(node: ast.expr) -> list[str]:
    """參數實際取用的名稱（見模組說明）。"""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Subscript):
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            return [node.slice.value]
        return _used_names(node.value) + _used_names(node.slice)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in SIZE_OR_TYPE:
            return []
        names = [] if isinstance(node.func, ast.Name) else _used_names(node.func)
        for arg in node.args:
            names += _used_names(arg)
        for kw in node.keywords:
            names += _used_names(kw.value)
        return names
    if isinstance(node, ast.Constant):
        return []
    names: list[str] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            names += _used_names(child)
    return names


def scan(source: str, filename: str) -> tuple[int, list[str]]:
    """回傳 (檢查了幾個輸出呼叫, 違規清單)。"""
    calls = 0
    problems = []
    for node in ast.walk(ast.parse(source, filename)):
        if not (isinstance(node, ast.Call) and _is_output_call(node.func)):
            continue
        calls += 1
        args = list(node.args) + [kw.value for kw in node.keywords if kw.arg not in ("file", "flush", "end", "sep")]
        for arg in args:
            for name in _used_names(arg):
                if FORBIDDEN.search(name) and (filename, name) not in ALLOWED:
                    problems.append(f"src/llmop/{filename}:{node.lineno}: {name!r} ← {ast.unparse(node)[:160]}")
    return calls, problems


def _exception_dumps(source: str, filename: str) -> list[str]:
    """閘道與 worker 不可以印出例外的追蹤或訊息原文：只能記例外類型（訊息裡可能夾帶輸入資料）。"""
    problems = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "exception" and _is_output_call(func):
            problems.append(f"src/llmop/{filename}:{node.lineno}: log.exception")
        if isinstance(func, ast.Attribute) and func.attr in ("print_exc", "format_exc", "print_exception"):
            problems.append(f"src/llmop/{filename}:{node.lineno}: traceback.{func.attr}")
        if _is_output_call(func) and any(kw.arg in ("exc_info", "stack_info") for kw in node.keywords):
            problems.append(f"src/llmop/{filename}:{node.lineno}: exc_info/stack_info")
    return problems


# 只掃加密相關（B）的模組；A 的直連模組本來就處理明文，不適用這條規則。
SCANNED = ("secure/", "worker/", "client/", "crypto.py", "protocol.py", "paths.py")


def _sources() -> dict[str, str]:
    root = SRC / "llmop"
    return {
        rel: path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if (rel := path.relative_to(root).as_posix()).startswith(SCANNED)
    }


def test_no_secrets_or_content_in_log_or_print_calls():
    total = 0
    problems: list[str] = []
    for filename, source in _sources().items():
        calls, found = scan(source, filename)
        total += calls
        problems += found
    assert total > 40, "沒有找到足夠的紀錄呼叫，檢查本身可能失效了"
    assert not problems, "紀錄或 print 的參數取用了秘密值或內容：\n" + "\n".join(problems)


def test_allow_list_entries_are_still_needed():
    """例外清單裡的每一項都必須還對應到真的呼叫；不再需要的例外要刪掉，清單才會一直很短。"""
    used = set()
    for filename, source in _sources().items():
        for node in ast.walk(ast.parse(source, filename)):
            if isinstance(node, ast.Call) and _is_output_call(node.func):
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    used |= {(filename, name) for name in _used_names(arg)}
    assert set(ALLOWED) <= used, f"多餘的例外：{sorted(set(ALLOWED) - used)}"


def test_gateway_and_worker_never_dump_exceptions():
    problems = []
    for filename, source in _sources().items():
        if filename.startswith(("secure/", "worker/")):
            problems += _exception_dumps(source, filename)
    assert not problems, "\n".join(problems)


def test_scanner_catches_typical_leaks():
    """檢查器本身的測試：常見的洩漏寫法都要抓得到，安全的寫法不能誤判。"""
    leaky = '''
log.info("got %s", envelope)
log.debug("frame %r", frame)
logger.warning(f"backend said {exc.message}")
print(frame["sealed"])
print("token:", state.worker_token)
log.info("x %s", json.dumps(body))
logging.error("%s", request.headers["Authorization"], self.config.api_key)
'''
    _, problems = scan(leaky, "probe.py")
    assert len(problems) >= 7, problems

    safe = '''
log.info("request %s model=%s sealed_bytes=%d", request_id, header.model, len(frame["sealed"]))
log.warning("忽略非預期的訊框 type=%s", frame["type"])
log.error("錯誤（%s）", type(exc).__name__)
print(f"key_id: {key.key_id}")
'''
    calls, problems = scan(safe, "probe.py")
    assert calls == 4
    assert problems == []
    assert _exception_dumps("log.exception('x')\nlog.error('x', exc_info=True)\n", "probe.py")
