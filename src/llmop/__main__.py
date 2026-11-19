"""Command-line entry point (``llmop`` and ``python -m llmop``).

Dispatch only: ``gateway`` / ``worker`` / ``client`` go to the E2E role CLIs
(``llmop.<role>.cli:main``); anything else goes to the direct gateway CLI
(``llmop.cli:main``).
"""

from __future__ import annotations

import importlib
import sys

ROLES = ("gateway", "worker", "client")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] in ROLES:
        role = args[0]
        module_name = f"llmop.{role}.cli"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Only intercept a missing role module; errors from missing deps inside it propagate.
            if exc.name not in (f"llmop.{role}", module_name):
                raise
            print(f"「{role}」角色尚未安裝（找不到 {module_name}）。", file=sys.stderr)
            return 2
        return module.main(args[1:]) or 0

    from .cli import main as direct_main

    return direct_main(args)


if __name__ == "__main__":
    sys.exit(main())
