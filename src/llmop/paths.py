"""執行期狀態的位置與 JSON 設定檔讀寫。

所有狀態都放在 $LLMOP_HOME（預設 ~/.llmop）底下、依角色分目錄，不放在專案目錄裡。
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


def home(role: str) -> Path:
    """回傳 $LLMOP_HOME/<role>，不存在就以 0700 權限建立。"""
    base = Path(os.environ.get("LLMOP_HOME") or "~/.llmop").expanduser()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = base / role
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def read_json(path: str | os.PathLike) -> dict:
    """讀取一個內容為 JSON 物件的檔案。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 的內容必須是 JSON 物件")
    return data


def write_json(path: str | os.PathLike, data: dict, *, secret: bool = False) -> None:
    """以原子方式寫入 JSON：先寫同目錄的暫存檔，再 os.replace。

    secret=True 時，暫存檔在寫入任何內容之前就是 0600 權限。
    失敗時不留下暫存檔，原本的檔案保持不變。
    """
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if secret else 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if secret:
                os.fchmod(fh.fileno(), 0o600)  # 不受 umask 影響，確定是 0600
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
