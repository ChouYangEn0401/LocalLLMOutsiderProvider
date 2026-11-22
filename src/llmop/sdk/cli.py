"""``llmop client`` 的 SDK 子指令：enroll、add-plain、profiles、set-link、pin、unpin、self-check、revoke、forget。

密碼與 API key 只從終端機提示輸入（getpass，不回顯），不收命令列參數，免得留在 shell 歷史與程序清單裡。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import profiles as store
from .client import Client, add_plain, enroll
from .errors import SdkError

COMMANDS = ("enroll", "add-plain", "profiles", "set-link", "pin", "unpin", "self-check", "revoke", "forget")


def _profile_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", default="default", help="設定檔名稱（預設 default）")


def add_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("enroll", help="用帳號密碼建 key＋上傳公鑰＋核對並釘選 worker 指紋，建好設定檔（密碼會提示輸入）")
    p.add_argument("--account-url", required=True, help="帳號網站網址（https://…）")
    p.add_argument("--user", required=True, help="帳號名稱")
    p.add_argument("--fingerprint", help="worker 指紋（22 字元 key_id，向服務提供者用另一個管道拿）")
    _profile_arg(p)
    p.add_argument("--encryption", choices=("required", "optional"), default="required",
                   help="required＝只能加密使用（預設）；optional＝也可以明文")
    p.add_argument("--api-link", help="API 連結（不給就用帳號網站顯示的目前連結）")
    p.add_argument("--key-name", default="", help="這把 key 在網站上的名稱")
    p.add_argument("--expires-days", type=int, help="幾天後過期（不給＝不過期）")
    p.add_argument("--replace-key-id", help="先撤銷這把舊 key 再建（帳號的 key 數到上限時用）")
    p.add_argument("--test", action="store_true", help="建好後送一句測試")

    p = sub.add_parser("add-plain", help="只有一把 key（例如 GUEST key）：建一個明文設定檔（key 會提示輸入）")
    p.add_argument("--api-link", required=True, help="API 連結（https://…/<秘密路徑>）")
    p.add_argument("--max-tokens", type=int, help="預設的回覆 token 上限")
    _profile_arg(p)

    sub.add_parser("profiles", help="列出這台電腦上的設定檔")

    p = sub.add_parser("set-link", help="API 連結換了：換成服務提供者給的新連結")
    p.add_argument("url", help="新的 API 連結")
    p.add_argument("--no-check", action="store_true", help="不試連")
    _profile_arg(p)

    p = sub.add_parser("pin", help="核對並釘選一把 worker 公鑰（指紋由服務提供者用另一個管道給）")
    p.add_argument("--fingerprint", required=True, help="22 字元的 key_id")
    _profile_arg(p)

    p = sub.add_parser("unpin", help="取消釘選一把 worker 公鑰")
    p.add_argument("key_id", help="要取消的指紋")
    _profile_arg(p)

    p = sub.add_parser("self-check", help="檢查設定檔：檔案權限、連結、key、明文與沒簽章的請求是否被拒")
    p.add_argument("--fix", action="store_true", help="先把檔案權限重新收緊")
    _profile_arg(p)

    p = sub.add_parser("revoke", help="在伺服器撤銷這個設定檔的 key（密碼會提示輸入）")
    _profile_arg(p)

    p = sub.add_parser("forget", help="刪掉這台電腦上的設定檔（不撤銷伺服器上的 key）")
    p.add_argument("--yes", action="store_true", help="不再確認")
    _profile_arg(p)


def _secret(prompt: str) -> str:
    value = getpass.getpass(prompt)
    if not value:
        raise SdkError("bad_request", "沒有輸入")
    return value


def _ok(flag: bool) -> str:
    return "PASS" if flag else "FAIL"


def run(args: argparse.Namespace) -> int | None:
    """處理 SDK 子指令；不是 SDK 的子指令回 None。"""
    cmd = args.command
    if cmd not in COMMANDS:
        return None
    try:
        return _run(cmd, args)
    except SdkError as exc:
        print(f"錯誤（{exc.kind_hint}）：{exc.message or exc.code}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


def _run(cmd: str, args: argparse.Namespace) -> int:
    if cmd == "profiles":
        names = store.list_profiles()
        if not names:
            print("還沒有任何設定檔。用 llmop client enroll 或 llmop client add-plain 建立。")
            return 0
        for name in names:
            try:
                p = store.load(name)
                print(f"{name}\t{p.resolved_transport()}\t{p.status}\t{store.mask_link(p.api_link)}"
                      f"\t釘選 {len(p.pinned_workers)}{'（舊版設定檔）' if p.legacy else ''}")
            except SdkError as exc:
                print(f"{name}\t（讀不到：{exc.message}）")
        return 0

    if cmd == "enroll":
        password = _secret(f"{args.user} 的密碼：")
        result = enroll(args.account_url, args.user, password, worker_fingerprint=args.fingerprint,
                        profile=args.profile, encryption=args.encryption, key_name=args.key_name,
                        expires_days=args.expires_days, replace_key_id=args.replace_key_id,
                        api_link=args.api_link, test=args.test)
        if result.sig_key_id:
            print(("已在這台電腦建立簽章金鑰" if result.signing_key_created else "沿用這台電腦的簽章金鑰")
                  + f"（sig_key_id {result.sig_key_id}；私鑰不會離開這台電腦）")
        print(f"API key 已存在本機設定檔「{result.profile}」（{result.key_display}）")
        if result.pinned:
            print(f"伺服器指紋相符，已釘選：{', '.join(result.pinned)}")
        print(f"傳輸方式：{result.transport}　狀態：{result.status}　API 連結：{result.api_link_display}")
        if result.test_reply is not None:
            print(f"測試回覆：{result.test_reply}")
        return 0

    if cmd == "add-plain":
        key = _secret("API key（不會顯示）：")
        p = add_plain(args.api_link, key, profile=args.profile, max_tokens=args.max_tokens)
        print(f"已建立明文設定檔「{p.name}」（{store.mask_key(p.api_key)}，{store.mask_link(p.api_link)}）")
        return 0

    if cmd == "forget":
        p = store.load(args.profile)
        if not args.yes:
            answer = input(f"確定刪掉這台電腦上的設定檔「{p.name}」（含簽章私鑰）？伺服器上的 key 不會被撤銷。[y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("沒有刪。")
                return 1
        removed = Client(p).forget()
        print(f"已刪除設定檔「{p.name}」：{', '.join(removed) or '（沒有檔案）'}")
        return 0

    with Client.from_profile(args.profile) as client:
        if cmd == "set-link":
            ok = client.set_link(args.url, check=not args.no_check)
            print(f"已換成新的 API 連結：{store.mask_link(client.profile.api_link)}")
            if ok is False:
                print("（試連失敗：連結不通或伺服器沒在跑；已經存下，稍後再試）", file=sys.stderr)
            return 0
        if cmd == "pin":
            result = client.pin_worker(args.fingerprint)
            state = "已釘選" if result.newly_pinned else "早就釘選了"
            print(f"{state}：{result.key_id}（模型：{', '.join(result.models) or '—'}）")
            return 0
        if cmd == "unpin":
            client.unpin_worker(args.key_id)
            print(f"已取消釘選：{args.key_id}")
            return 0
        if cmd == "self-check":
            results = client.self_check(fix=args.fix)
            for item, ok, note in results:
                print(f"{_ok(ok)}  {item}{('　' + note) if note else ''}")
            return 0 if all(ok for _i, ok, _n in results) else 1
        if cmd == "revoke":
            password = _secret(f"{client.profile.username or '帳號'} 的密碼：")
            client.revoke(password)
            print(f"已在伺服器撤銷設定檔「{client.name}」的 key；本機設定檔標成 revoked（要刪檔用 forget）")
            return 0
    return 2  # pragma: no cover
