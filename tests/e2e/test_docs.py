"""文件與程式一致：安全邊界段落兩處一字不差、文件裡寫的指令真的存在、相對連結都找得到檔案。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from llmop.__main__ import main as llmop_main

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DEPLOY = ROOT / "docs" / "DEPLOY.md"
SECURITY = ROOT / "docs" / "SECURITY.md"
DOCS = [README, DEPLOY]


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


def test_readme_security_boundary_is_verbatim_copy():
    expected = _boundary_block(SECURITY)
    assert len(expected) > 10
    assert _boundary_block(README) == expected


def _documented_commands() -> set[tuple[str, str, tuple[str, ...]]]:
    """文件裡出現的每一個 `llmop <角色> <命令> [--旗標…]`。行內註解（#）與反引號之後的文字不算。"""
    found = set()
    for path in DOCS:
        for line in path.read_text(encoding="utf-8").splitlines():
            for match in re.finditer(r"llmop (gateway|worker|client) ([a-z][a-z-]*)([^`#\n]*)", line):
                flags = tuple(re.findall(r"(?<!\S)--[a-z][a-z-]*", match.group(3)))
                found.add((match.group(1), match.group(2), flags))
    return found


def test_documents_mention_the_main_commands():
    commands = {(role, cmd) for role, cmd, _ in _documented_commands()}
    assert {
        ("gateway", "init"), ("gateway", "run"),
        ("worker", "init"), ("worker", "run"), ("worker", "show-key"), ("worker", "rotate-key"),
        ("worker", "prune-keys"),
        ("client", "keygen"), ("client", "proxy"), ("client", "chat"), ("client", "models"),
    } <= commands


@pytest.mark.parametrize("command", sorted(_documented_commands()), ids=lambda c: " ".join([c[0], c[1], *c[2]]))
def test_documented_commands_and_flags_exist(command, capsys):
    role, cmd, flags = command
    with pytest.raises(SystemExit) as exc:
        llmop_main([role, cmd, "--help"])
    assert exc.value.code == 0, f"llmop {role} {cmd} 不是有效的命令"
    help_text = capsys.readouterr().out
    for flag in flags:
        assert flag in help_text, f"llmop {role} {cmd} 沒有 {flag} 這個選項"


def test_relative_links_resolve():
    for path in DOCS:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            assert (path.parent / target).exists(), f"{path.name} 連到不存在的 {target}"


def test_deploy_covers_required_topics():
    text = DEPLOY.read_text(encoding="utf-8")
    for needle in (
        "cloudflared tunnel --url http://127.0.0.1:8787",
        "隧道只能指向閘道的連接埠",
        "每次啟動 `cloudflared` 都會拿到新的隨機網址",
        "llmop worker rotate-key",
        "llmop worker prune-keys",
        "五分鐘",
        "admin_local_only",
        "不支援 SSE",
        "200",
        "202",
    ):
        assert needle in text, needle
    readme = README.read_text(encoding="utf-8")
    for needle in ("`local`", "`other`", "`http://127.0.0.1:8788`", "隧道只能指向閘道的連接埠"):
        assert needle in readme, needle
