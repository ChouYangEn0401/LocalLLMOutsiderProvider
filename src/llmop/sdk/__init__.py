"""llmop 的用戶端 SDK：讓別的程式跟一台 llmop 伺服器講話（經通道、帶 API key、可選端對端加密）。

    pip install "localllm-outsider-provider[sdk]"      # 只裝 httpx＋cryptography

    from llmop.sdk import Client, SdkError
    with Client.from_profile("home") as c:
        reply = c.chat({"messages": [{"role": "user", "content": "你好"}]})
        print(reply["choices"][0]["message"]["content"])

設定檔用終端機建立：``llmop client enroll``（帳號密碼，產生金鑰對、上傳公鑰、核對並釘選 worker 指紋）或
``llmop client add-plain``（只有一把 key）。說明見 docs/SDK.md。

``import llmop.sdk`` 不會載入 fastapi、uvicorn、aiohttp（有測試鎖住）。
"""

from __future__ import annotations

from .client import AsyncClient, Client, EnrollResult, ModelEntry, PinResult, add_plain, enroll, set_link
from .errors import KIND_HINTS, SdkError
from .profiles import Profile, list_profiles

__all__ = [
    "KIND_HINTS",
    "AsyncClient",
    "Client",
    "EnrollResult",
    "ModelEntry",
    "PinResult",
    "Profile",
    "SdkError",
    "add_plain",
    "enroll",
    "list_profiles",
    "set_link",
]
