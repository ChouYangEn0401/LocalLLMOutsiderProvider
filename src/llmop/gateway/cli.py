"""閘道命令列：llmop gateway init | run。"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from .config import load_config
from .store import Store

MIN_PASSWORD_LEN = 8


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llmop gateway", description="LocalLLMOutsiderProvider 閘道")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")
    sub.add_parser("init", help="設定（或重設）管理員密碼")
    sub.add_parser("run", help="啟動閘道")
    args = parser.parse_args(argv)
    if args.command == "init":
        return _init()
    if args.command == "run":
        return _run()
    parser.print_help(sys.stderr)
    return 2


def _init() -> int:
    password = getpass.getpass("管理員密碼：")
    if len(password) < MIN_PASSWORD_LEN:
        print(f"密碼至少要 {MIN_PASSWORD_LEN} 個字元。", file=sys.stderr)
        return 2
    if getpass.getpass("再輸入一次：") != password:
        print("兩次輸入的密碼不一致。", file=sys.stderr)
        return 2
    store = Store.open_default()
    try:
        store.set_admin_password(password)
        print(f"已儲存管理員密碼（{store.path}）。")
    finally:
        store.close()
    return 0


def _run() -> int:
    from aiohttp import web

    from .app import build_app

    try:
        config = load_config()
    except (ValueError, OSError) as exc:
        print(f"閘道設定錯誤：{exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store.open_default()
    try:
        if store.admin_password_record() is None:
            print("尚未設定管理員密碼，請先執行：llmop gateway init", file=sys.stderr)
            return 2
        url = f"http://{_url_host(config.host)}:{config.port}"
        print(f"閘道位址：{url}")
        print(f"管理介面：{url}/admin/")
        print(f"對外公開請另外執行：cloudflared tunnel --url {url}")
        web.run_app(build_app(config, store), host=config.host, port=config.port, print=None)
    finally:
        store.close()
    return 0


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host
