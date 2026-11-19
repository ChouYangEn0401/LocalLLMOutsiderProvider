"""文件與程式一致：安全邊界段落三處一字不差、文件裡寫的指令真的存在、相對連結都找得到檔案。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from llmop.__main__ import main as llmop_main

ROOT = Path(__file__).resolve().parents[2]
SECURE_DOCS = ROOT / "docs" / "secure"
README = SECURE_DOCS / "README.md"
DEPLOY = SECURE_DOCS / "DEPLOY.md"
SECURITY = SECURE_DOCS / "SECURITY.md"
PROTOCOL = SECURE_DOCS / "PROTOCOL.md"
ROOT_README = ROOT / "README.md"
HANDOFF = ROOT / "docs" / "HANDOFF.md"
DOCS = [ROOT_README, README, DEPLOY, HANDOFF]

ROLES = ("gateway", "worker", "client")
TUNNEL_ACTIONS = ("rotate-api", "show-api")


def _boundary_block(path: Path) -> list[str]:
    """從「> **安全邊界**」開始、連續以 > 開頭的那一段。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index("> **安全邊界**")
    block = []
    for line in lines[start:]:
        if not line.startswith(">"):
            break
        block.append(line)
    return block


@pytest.mark.parametrize("path", [ROOT_README, README], ids=["README.md", "docs/secure/README.md"])
def test_readme_security_boundary_is_verbatim_copy(path):
    expected = _boundary_block(SECURITY)
    assert len(expected) > 10
    assert _boundary_block(path) == expected


def _documented_commands() -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    """文件裡出現的每一個 `llmop <命令…> [--旗標…]`。行內註解（#）與反引號之後的文字不算。

    ``worker``／``client``／``gateway`` 後面一定接子命令；``tunnel`` 後面可接 rotate-api／show-api。
    """
    found = set()
    for path in DOCS:
        for line in path.read_text(encoding="utf-8").splitlines():
            for match in re.finditer(r"(?<![\w.-])llmop ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?([^`#\n|]*)", line):
                first, second, rest = match.group(1), match.group(2), match.group(3)
                if first in ROLES:
                    if not second:
                        continue  # 「llmop client 外掛」之類的說明文字
                    words = (first, second)
                elif first == "tunnel" and second in TUNNEL_ACTIONS:
                    words = (first, second)
                else:
                    words = (first,)
                    rest = (f" {second}" if second else "") + rest
                flags = tuple(re.findall(r"(?<!\S)--[a-z][a-z-]*", rest))
                found.add((words, flags))
    return found


def test_documents_mention_the_main_commands():
    commands = {words for words, _ in _documented_commands()}
    assert {
        ("init",), ("serve",), ("add-user",), ("check",),
        ("tunnel", "show-api"), ("tunnel", "rotate-api"),
        ("worker", "init"), ("worker", "run"), ("worker", "show-key"), ("worker", "rotate-key"),
        ("worker", "prune-keys"),
        ("client", "keygen"), ("client", "proxy"), ("client", "chat"), ("client", "models"),
    } <= commands


def _run_help(words: tuple[str, ...]) -> int:
    try:
        code = llmop_main([*words, "--help"])
    except SystemExit as exc:  # the worker / client CLIs exit through argparse
        code = exc.code
    return int(code or 0)


@pytest.mark.parametrize("command", sorted(_documented_commands()),
                         ids=lambda c: " ".join([*c[0], *c[1]]))
def test_documented_commands_and_flags_exist(command, capsys):
    words, flags = command
    assert _run_help(words) == 0, f"llmop {' '.join(words)} 不是有效的命令"
    help_text = capsys.readouterr().out
    assert help_text.strip(), f"llmop {' '.join(words)} --help 沒有輸出"
    for flag in flags:
        assert flag in help_text, f"llmop {' '.join(words)} 沒有 {flag} 這個選項"


def test_relative_links_resolve():
    for path in [*DOCS, SECURITY, PROTOCOL]:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            assert (path.parent / target).exists(), f"{path.name} 連到不存在的 {target}"


def test_deploy_covers_required_topics():
    text = DEPLOY.read_text(encoding="utf-8")
    for needle in (
        "隧道只能指向閘道的連接埠",
        "每次啟動 `cloudflared` 都會拿到新的隨機網址",
        "llmop tunnel rotate-api",
        "/<秘密路徑>/worker/ws",
        "llmop worker rotate-key",
        "llmop worker prune-keys",
        "五分鐘",
        "127.0.0.1:8781",
        "ssh -L 8781:127.0.0.1:8781",
        "不支援 SSE",
        "202",
        "/e2e/v1/requests/{request_id}",
        "含私鑰",
    ):
        assert needle in text, needle
    readme = README.read_text(encoding="utf-8")
    for needle in ("`local`", "`other`", "`http://127.0.0.1:8788`", "隧道只能指向閘道的連接埠", "/e2e/v1"):
        assert needle in readme, needle


def test_secure_docs_use_the_v02_paths():
    """加密面在 API 連結的 /<gate>/e2e/v1；v0.1 B 版的 8787、/admin/、llmop gateway run 不能再出現。"""
    for path in (README, DEPLOY, PROTOCOL, SECURITY):
        text = path.read_text(encoding="utf-8")
        # (`GET /v1/models` on the local client proxy 127.0.0.1:8788 is still correct.)
        for stale in ("8787", "/admin/", "llmop gateway run", "llmop gateway init", "/healthz",
                      "GET /v1/requests/", "admin_local_only: true", "llmop_sk_…\""):
            assert stale not in text, f"{path.name}: {stale!r}"
    protocol = PROTOCOL.read_text(encoding="utf-8")
    for needle in ("### 3.1 `POST /e2e/v1/chat/completions`", "### 3.2 `GET /e2e/v1/requests/{request_id}`",
                   "### 3.3 `GET /e2e/v1/models`", "<秘密路徑>", "queue_wait"):
        assert needle in protocol, needle


def test_root_readme_covers_the_v02_topics():
    text = ROOT_README.read_text(encoding="utf-8")
    for needle in (
        "http://127.0.0.1:8781",          # local console + key-less /v1
        "http://127.0.0.1:8788",          # encrypted client proxy
        "openai_compat",                  # SYNOX direct
        "<秘密路徑>",                     # API link with secret path
        "GUEST",
        "必須加密",
        "llmop tunnel rotate-api",
        "Start-Worker.bat", "Start-Client-Proxy.bat", "Rotate-API-Link.bat",
        "含私鑰",
        "llmop-0.2.0-windows-x64-portable.zip",
    ):
        assert needle in text, needle
