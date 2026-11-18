"""命令列進入點：只負責分派到 llmop.<角色>.cli:main。"""

from __future__ import annotations

import importlib
import sys

ROLES = ("gateway", "worker", "client")

USAGE = """用法：llmop <角色> [參數…]

角色：
  gateway   對外的閘道（HTTP API、worker WebSocket、管理介面）
  worker    在本機 LLM 伺服器旁執行、解密並推論的 worker
  client    用戶端工具（金鑰產生、本機 OpenAI 相容代理、對話）

各角色的子命令請用：llmop <角色> --help
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="", file=sys.stdout if args else sys.stderr)
        return 0 if args else 2
    role = args[0]
    if role not in ROLES:
        print(f"不認得的角色：{role}\n\n{USAGE}", end="", file=sys.stderr)
        return 2
    module_name = f"llmop.{role}.cli"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # 只有角色模組本身不存在時才攔截；模組內部缺少相依套件的錯誤照常拋出。
        if exc.name not in (f"llmop.{role}", module_name):
            raise
        print(f"「{role}」角色尚未安裝（找不到 {module_name}）。", file=sys.stderr)
        return 2
    return module.main(args[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
