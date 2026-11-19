"""Command-line entry point (``llmop`` and ``python -m llmop``).

Dispatch only:

* ``worker`` / ``client`` go to the E2E role CLIs (``llmop.<role>.cli:main``); when the
  ``secure`` extra is missing they print a one-line install hint instead of a traceback.
* ``gateway run`` / ``gateway init`` are aliases of ``serve`` / ``init`` (the v0.1
  encrypted gateway is now part of the node).
* anything else goes to the node CLI (``llmop.cli:main``).
"""

from __future__ import annotations

import importlib
import sys

ROLES = ("gateway", "worker", "client")
SECURE_ROLES = ("worker", "client")
SECURE_DEPS = ("cryptography", "aiohttp", "websockets", "yarl", "multidict")
INSTALL_HINT = 'pip install "localllm-outsider-provider[secure]"'
GATEWAY_ALIASES = {"run": "serve", "init": "init"}
GATEWAY_USAGE = (
    "usage: llmop gateway {run,init} [options]\n"
    "  run   = llmop serve  (the whole node: console, account website, API link, encrypted relay)\n"
    "  init  = llmop init   (create config.yaml)"
)


def _missing_secure(exc: ModuleNotFoundError) -> bool:
    return (exc.name or "").split(".")[0] in SECURE_DEPS


def _gateway(args: list[str]) -> int:
    from .cli import main as direct_main

    if not args or args[0] in ("-h", "--help"):
        print(GATEWAY_USAGE)
        return 0 if args else 2
    command = GATEWAY_ALIASES.get(args[0])
    if command is None:
        print(GATEWAY_USAGE, file=sys.stderr)
        return 2
    if "-h" in args[1:] or "--help" in args[1:]:
        from .cli import build_parser

        # Like the other role CLIs (argparse): --help prints and raises SystemExit(0).
        build_parser().parse_args([command, *args[1:]])
    return direct_main([command, *args[1:]])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] == "gateway":
        return _gateway(args[1:])
    if args and args[0] in SECURE_ROLES:
        role = args[0]
        module_name = f"llmop.{role}.cli"
        try:
            module = importlib.import_module(module_name)
            return module.main(args[1:]) or 0
        except ModuleNotFoundError as exc:
            if _missing_secure(exc):
                print(f"llmop {role} needs the secure extra: {INSTALL_HINT}", file=sys.stderr)
                return 2
            # Only intercept a missing role module; other errors propagate.
            if exc.name not in (f"llmop.{role}", module_name):
                raise
            print(f"「{role}」角色尚未安裝（找不到 {module_name}）。", file=sys.stderr)
            return 2

    from .cli import main as direct_main

    return direct_main(args)


if __name__ == "__main__":
    sys.exit(main())
