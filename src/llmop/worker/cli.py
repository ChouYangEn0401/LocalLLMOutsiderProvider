"""`llmop worker` 命令列：init、show-key、rotate-key、prune-keys、run。

主控台印出的 key_id 與公鑰，才是用戶端釘選的依據（docs/SECURITY.md）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from llmop import paths
from llmop.worker.config import ConfigError, default_config, load_config
from llmop.worker.keystore import (
    KeyStore,
    KeyStoreError,
    WorkerKey,
    init_keys,
    load_keys,
    load_state,
    prune,
    rotate,
    save_keys,
)
from llmop.worker.link import LinkStopped, serve


def _files(home: Path) -> tuple[Path, Path, Path]:
    return home / "config.json", home / "keys.json", home / "state.json"


def _print_key(key: WorkerKey, title: str) -> None:
    created = datetime.fromtimestamp(key.created_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(title)
    print(f"  key_id     : {key.key_id}")
    print(f"  public_key : {key.public_key}")
    print(f"  建立時間   : {created}")


def _require_keys(path: Path) -> KeyStore:
    store = load_keys(path)
    if store is None:
        raise KeyStoreError(f"找不到 {path}；請先執行 llmop worker init")
    return store


def cmd_init(args: argparse.Namespace, home: Path) -> int:
    config_path, keys_path, _ = _files(home)
    store, created = init_keys(keys_path)
    print(f"已建立金鑰檔 {keys_path}（權限 0600）。" if created else f"金鑰檔 {keys_path} 已存在，沒有變更。")
    _print_key(store.current_key(), "目前的金鑰（請把 key_id 與 public_key 釘選到用戶端設定）：")
    if config_path.exists():
        print(f"設定檔 {config_path} 已存在，沒有變更。")
    else:
        # 設定檔可能放後端的 api_key，所以也用 0600。
        paths.write_json(config_path, default_config(), secret=True)
        print(f"已建立設定檔 {config_path}；請確認 gateway_url 與 backends 後再執行 llmop worker run。")
    return 0


def cmd_show_key(args: argparse.Namespace, home: Path) -> int:
    store = _require_keys(_files(home)[1])
    keys = store.ordered()
    _print_key(keys[0], "目前的金鑰：")
    for key in keys[1:]:
        _print_key(key, "仍然接受的舊金鑰：")
    return 0


def cmd_rotate_key(args: argparse.Namespace, home: Path) -> int:
    keys_path = _files(home)[1]
    store = rotate(_require_keys(keys_path))
    save_keys(keys_path, store)
    _print_key(store.current_key(), "已產生新的目前金鑰：")
    print("舊金鑰仍會被接受，直到執行 llmop worker prune-keys。")
    print("請更新用戶端的釘選值，並重新啟動執行中的 worker，閘道才會看到新金鑰。")
    return 0


def cmd_prune_keys(args: argparse.Namespace, home: Path) -> int:
    keys_path = _files(home)[1]
    store = _require_keys(keys_path)
    old = [k for k in store.keys if k.key_id != store.current]
    if not old:
        print("只有目前的金鑰，沒有可刪除的舊金鑰。")
        return 0
    print("將永久刪除以下舊私鑰；以它們加密的請求從此再也解不開：")
    for key in old:
        print(f"  {key.key_id}")
    if not args.yes:
        try:
            answer = input("確定要刪除嗎？請輸入 yes 確認：")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("已取消，沒有刪除任何金鑰。")
            return 1
    save_keys(keys_path, prune(store))
    print(f"已刪除 {len(old)} 把舊金鑰。請重新啟動執行中的 worker 讓變更生效。")
    return 0


def cmd_run(args: argparse.Namespace, home: Path) -> int:
    config_path, keys_path, state_path = _files(home)
    config = load_config(config_path)
    store = _require_keys(keys_path)
    _print_key(store.current_key(), "目前的金鑰（用戶端應釘選這個值）：")
    reg_token = args.reg_token
    if load_state(state_path) is None:
        if not reg_token:
            print("這台 worker 尚未註冊：第一次執行請加上 --reg-token（向閘道管理員索取）。", file=sys.stderr)
            return 1
    elif reg_token:
        print("已經註冊過（state.json 存在），忽略 --reg-token。")
        reg_token = None
    sys.stdout.flush()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(serve(config, store, home, reg_token))
    except LinkStopped as exc:
        print(f"worker 已停止：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("worker 已停止。")
    return 0


COMMANDS = {
    "init": cmd_init,
    "show-key": cmd_show_key,
    "rotate-key": cmd_rotate_key,
    "prune-keys": cmd_prune_keys,
    "run": cmd_run,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llmop worker", description="在本機 LLM 伺服器旁執行的 worker。")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<命令>")
    sub.add_parser("init", help="建立第一把金鑰與設定範本，並印出 key_id 與公鑰")
    sub.add_parser("show-key", help="印出目前與仍然接受的金鑰")
    sub.add_parser("rotate-key", help="產生新金鑰並設為目前金鑰")
    prune_parser = sub.add_parser("prune-keys", help="刪除目前金鑰以外的所有舊私鑰")
    prune_parser.add_argument("--yes", action="store_true", help="不再詢問確認")
    run_parser = sub.add_parser("run", help="連到閘道並開始服務")
    run_parser.add_argument("--reg-token", metavar="TOKEN", help="第一次執行時的註冊權杖（llmop_reg_…）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = paths.home("worker")
    try:
        return COMMANDS[args.command](args, home)
    except (ConfigError, KeyStoreError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
