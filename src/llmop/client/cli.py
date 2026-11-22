"""llmop client 的命令列。

- SDK 子指令（只需要 ``[sdk]``）：enroll、add-plain、profiles、set-link、pin、unpin、self-check、revoke、forget，
  實作在 llmop.sdk.cli。
- v0.2 的子指令（需要 ``[secure]``，用舊的單一設定檔 client/config.json）：keygen、proxy、chat、models。
  aiohttp 只在這幾個子指令裡才載入。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from llmop.sdk import cli as sdk_cli
from llmop.sdk.errors import SdkError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llmop client", description="LocalLLMOutsiderProvider 用戶端工具")
    parser.add_argument("-v", "--verbose", action="store_true", help="輸出除錯紀錄")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<命令>")
    sdk_cli.add_commands(sub)
    sub.add_parser("keygen", help="（v0.2）建立簽章金鑰（已存在就沿用），印出要貼到閘道管理介面的公鑰與 sig_key_id")
    sub.add_parser("proxy", help="（v0.2）在 127.0.0.1 啟動本機 OpenAI 相容代理")
    chat = sub.add_parser("chat", help="（v0.2）送出一筆請求並印出回覆")
    chat.add_argument("--model", required=True, help="模型名稱")
    chat.add_argument("--max-tokens", type=int, help="回覆的 token 上限（預設取設定檔的 default_max_tokens）")
    chat.add_argument("prompt", help="提示文字")
    sub.add_parser("models", help="（v0.2）列出已釘選 worker 提供的模型")
    return parser


def _keygen() -> int:
    from llmop.client.config import ensure_signing_key, signing_key_info

    key, created = ensure_signing_key()
    public_key, sig_key_id = signing_key_info(key)
    print("已建立新的簽章金鑰。" if created else "簽章金鑰已存在，沿用原本的金鑰。")
    print(f"public_key: {public_key}")
    print(f"sig_key_id: {sig_key_id}")
    print("請把 public_key 貼到閘道管理介面中這把 API 金鑰的簽章公鑰欄位，並以 sig_key_id 核對。")
    return 0


async def _models() -> int:
    import aiohttp

    from llmop.client import sdk
    from llmop.client.config import load_config

    config = load_config()
    async with aiohttp.ClientSession() as session:
        entries = await sdk.list_models(session, config)
    if not entries:
        print("目前沒有任何已釘選的 worker 在線上。", file=sys.stderr)
        return 1
    for entry in entries:
        print(f"{entry.model}\t{entry.key_id}")
    return 0


async def _chat(model: str, max_tokens: int | None, prompt: str) -> int:
    import aiohttp

    from llmop.client import sdk
    from llmop.client.config import load_config, load_signing_key

    config = load_config()
    signing_key = load_signing_key()
    body = {"messages": [{"role": "user", "content": prompt}]}
    async with aiohttp.ClientSession() as session:
        completion = await sdk.chat(
            session,
            config,
            signing_key,
            body,
            model=model,
            max_tokens=config.default_max_tokens if max_tokens is None else max_tokens,
        )
    choices = completion.get("choices") or [{}]
    message = choices[0].get("message") or {}
    if message.get("content"):
        print(message["content"])
    elif message.get("tool_calls"):
        print(json.dumps(message["tool_calls"], ensure_ascii=False, indent=2))
    return 0


def _legacy(args: argparse.Namespace) -> int:
    from llmop.client.config import ConfigError, load_config, load_signing_key

    try:
        from llmop.client.sdk import ClientError
    except ModuleNotFoundError:  # 沒裝 [secure]：讓 llmop.__main__ 印安裝提示
        raise
    try:
        if args.command == "keygen":
            return _keygen()
        if args.command == "models":
            return asyncio.run(_models())
        if args.command == "chat":
            return asyncio.run(_chat(args.model, args.max_tokens, args.prompt))
        from llmop.client.proxy import run_proxy

        run_proxy(load_config(), load_signing_key())
        return 0
    except (ConfigError, ClientError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    default_level = logging.INFO if args.command == "proxy" else logging.WARNING
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else default_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    handled = sdk_cli.run(args)
    if handled is not None:
        return handled
    return _legacy(args)


__all__ = ["main", "SdkError"]
