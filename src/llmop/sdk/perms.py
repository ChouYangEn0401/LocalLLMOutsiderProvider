"""把私鑰與 API 金鑰所在的檔案、資料夾收緊成「只有本人讀得到」。

做法（詳見 docs/SDK.md「私鑰檔的權限」）：

- POSIX：資料夾 0700、檔案 0600（跟 OpenSSH 的 ~/.ssh 一樣）。
- Windows：沒有 0600；改用 ACL。以「目前使用者的 SID」執行
  ``icacls <路徑> /inheritance:r /grant:r *<SID>:(F)``（資料夾加 ``(OI)(CI)``，讓裡面新建的檔案也只給本人）：
  先移除從上層繼承來的權限，再只給本人完整控制。用 SID 而不是帳號名稱，避免本地化的群組名與名稱解析問題；
  whoami.exe、icacls.exe 都從 %SystemRoot%\\System32 用完整路徑叫，不靠 PATH。
  系統管理員仍然可以取得擁有權再讀檔——這是 Windows 的設計，任何檔案權限都擋不住本機管理員。

check_private() 只讀，不改任何東西；self-check 用它回報。
"""

from __future__ import annotations

import csv
import io
import logging
import os
import stat
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("llmop.sdk")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _system32(exe: str) -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return str(Path(root) / "System32" / exe)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=30, creationflags=_NO_WINDOW, check=False)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs", "cp950", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")  # pragma: no cover


@lru_cache(maxsize=1)
def current_user() -> tuple[str, str]:
    """Windows：(DOMAIN\\name, SID)；拿不到丟 OSError。"""
    proc = _run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"])
    if proc.returncode != 0:
        raise OSError("whoami /user 失敗")
    rows = list(csv.reader(io.StringIO(_decode(proc.stdout).strip())))
    if not rows or len(rows[-1]) < 2 or not rows[-1][1].startswith("S-1-"):
        raise OSError("無法從 whoami 取得目前使用者的 SID")
    return rows[-1][0], rows[-1][1]


def restrict(path: Path) -> tuple[bool, str]:
    """把 path（檔案或資料夾）收緊成只有本人。回傳 (成功?, 說明)；失敗只記警告、不丟例外。"""
    path = Path(path)
    is_dir = path.is_dir()
    if os.name != "nt":
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
            return True, ""
        except OSError as exc:
            log.warning("無法收緊權限 %s（%s）", path.name, type(exc).__name__)
            return False, f"chmod 失敗（{type(exc).__name__}）"
    try:
        _name, sid = current_user()
        grant = f"*{sid}:(OI)(CI)(F)" if is_dir else f"*{sid}:(F)"
        proc = _run([_system32("icacls.exe"), str(path), "/inheritance:r", "/grant:r", grant, "/Q"])
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("無法收緊權限 %s（%s）", path.name, exc)
        return False, f"icacls 無法執行（{exc}）"
    if proc.returncode != 0:
        log.warning("icacls 失敗 %s（exit %s）", path.name, proc.returncode)
        return False, f"icacls 失敗（exit {proc.returncode}）"
    return True, ""


def check_private(path: Path) -> tuple[bool, str]:
    """只讀檢查：path 是否只有本人能存取。回傳 (通過?, 說明)。"""
    path = Path(path)
    if not path.exists():
        return False, "檔案不存在"
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return False, f"權限是 {mode:o}，其他人讀得到；應該是 600（資料夾 700）"
        return True, f"權限 {mode:o}"
    try:
        name, _sid = current_user()
        proc = _run([_system32("icacls.exe"), str(path)])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"無法讀取 ACL（{exc}）"
    if proc.returncode != 0:
        return False, f"icacls 失敗（exit {proc.returncode}）"
    text = _decode(proc.stdout).replace(str(path), "")
    aces = [line.strip() for line in text.splitlines() if ":(" in line]
    if len(aces) == 1 and aces[0].lower().startswith(name.lower() + ":"):
        return True, "只有本人"
    others = [a.split(":(")[0] for a in aces if not a.lower().startswith(name.lower() + ":")]
    return False, f"還有其他人有權限：{', '.join(others) or '?'}（用 llmop client self-check --fix 收緊）"
