# llmop 用戶端 SDK（`llmop.sdk`，v0.3）

讓**別的程式**跟一台 llmop 伺服器講話：經 Cloudflare 通道、帶 API key、可選端對端加密（e2e）。
SDK 只需要 `httpx` 與 `cryptography`，`import llmop.sdk` 不會載入 fastapi、uvicorn、aiohttp。

本文的網址與 key 全是假的：帳號網站 `https://account.example.test`、API 連結
`https://api.example.test/<秘密路徑>`、key `sk-llop-xxxx…`。

## 1. 安裝

```bash
pip install "localllm-outsider-provider[sdk]"
```

從 wheel 檔安裝時寫成 `pip install "<wheel 檔>[sdk]"`。伺服器那台另外要 `[server,secure]`（見 README）。

## 2. 建立設定檔（在使用者自己的電腦、終端機）

帳號由**服務提供者在伺服器那台的終端機**建立（`llmop add-user <名稱> --generate`），再把下面三樣交給使用者：
帳號網站網址＋帳號密碼、（加密時）worker 指紋。指紋是 22 個字元的 `key_id`，服務提供者用 `llmop worker show-key`
看，**用帳號網站以外的管道**（當面、訊息）交給使用者。

### 2.1 有帳號：`enroll`（建議，預設加密）

```bash
llmop client enroll --account-url https://account.example.test --user friend1 --fingerprint <22 字元指紋> --profile home --test
```

密碼會在終端機提示輸入（不回顯，不收命令列參數）。這個指令依序做：

1. 在這台電腦產生 Ed25519 簽章金鑰對。**私鑰不離開這台電腦**，只上傳公鑰。
2. 用 HTTPS 登入帳號網站（密碼只用這一次，不存檔、不寫紀錄）。
3. 建一把 API key（預設「必須加密」，綁上剛才的公鑰），拿到的 key 存進本機設定檔；登出。
4. API 連結：`--api-link` 指定，沒給就用帳號網站顯示的目前連結。
5. 到 `<API 連結>/e2e/v1/models` 找指紋相符的 worker 公鑰，**用公鑰重新算一次指紋**，對上了才釘選。
   對不上就停下來（`SdkError(kind_hint="trust")`，訊息「指紋不符，可能被調包，沒有釘選」），設定檔標 `incomplete`；
   跟服務提供者核對後用 `llmop client pin --profile home --fingerprint <指紋>` 補上。
6. `--test` 會送一句「ping」確認通了。

其他選項：`--encryption optional`（也可以走明文）、`--key-name`、`--expires-days`、
`--replace-key-id <舊 key id>`（帳號的 key 數到上限時，先撤銷舊的再建）。

### 2.2 只有一把 key：`add-plain`

服務提供者在本機控制台發的 GUEST key（或任何「加密：可選」的 key），只走明文：

```bash
llmop client add-plain --profile tiny --api-link https://api.example.test/<秘密路徑>
```

key 在終端機提示輸入。明文模式的內容，閘道與 Cloudflare 都看得到。

### 2.3 設定檔放在哪

| 檔案 | 內容 |
|---|---|
| `$LLMOP_HOME/client/<名稱>/config.json` | API 連結、API key、傳輸方式、釘選的 worker 公鑰、帳號網站網址與帳號名（撤銷用） |
| `$LLMOP_HOME/client/<名稱>/signing_key.json` | 簽章私鑰（只有加密設定檔才有） |

`$LLMOP_HOME` 預設是家目錄下的 `.llmop`（Windows 是 `%USERPROFILE%\.llmop`）。v0.2 的單一設定檔
`$LLMOP_HOME/client/config.json` 當成名稱 `default` 的設定檔照讀，不搬檔；對它的修改只寫舊格式的欄位，
舊的 `llmop client proxy` 照樣能用。

## 3. 在程式裡用

```python
from llmop.sdk import Client, SdkError

with Client.from_profile("home") as client:
    reply = client.chat({"messages": [{"role": "user", "content": "你好"}]}, max_tokens=512)
    print(reply["choices"][0]["message"]["content"])      # OpenAI 的 chat.completion
    print([m.id for m in client.models()])
    print(client.me())                                     # 設定檔與帳號資訊（秘密路徑、key 都遮掉）
```

- `transport`：設定檔預設 `auto`＝有簽章金鑰而且有釘選的 worker 就走 `e2e`，否則 `plain`；`chat(..., transport="plain")` 可以強制。
- `model`：加密模式不給（或給 `any`）就用第一個已釘選 worker 的模型；明文模式由伺服器決定（llmop 是單一模型）。
- 不支援串流：`stream: true` 會被拒。加密模式自動用 202＋輪詢，避開 Cloudflare 約 100 秒的 524。
- `on_queue=lambda q: ...`：加密模式排隊時收到 `{"position": 3, "eta": 40}`。
- async 程式用 `AsyncClient.from_profile(...)`，方法同名、都是 `await`（內部把同步版丟到執行緒）。

## 4. 錯誤

所有錯誤都是 `SdkError(code, message, status, retry_after, kind_hint)`：

| kind_hint | 什麼時候 | 例 |
|---|---|---|
| `quota` | 額度用完 | 429 `insufficient_quota`、401 `key_exhausted`、e2e 429 `quota_exceeded`（帶 `retry_after`） |
| `auth` | key 無效、過期、撤銷；帳號停用；密碼錯 | 401 |
| `rate` | 太頻繁、排隊太多 | 429 `queue_wait` |
| `link_changed` | API 連結的秘密路徑回 404 | 訊息「API 連結已更換，請向服務提供者拿新連結」 |
| `offline` | 連不上、通道沒在跑（Cloudflare 530）、worker 斷線 | |
| `timeout` | 逾時（Cloudflare 524、閘道 504、整體期限） | |
| `trust` | 指紋不符、簽章不對、解不開回覆、沒有釘選 | |
| `bad_request` | 請求或設定有問題（含「這把 key 只能加密使用」 `encryption_required`） | 400、403、404 |
| `server` | 伺服器那邊的錯 | 5xx、`queue_full` |
| `unknown` | 其他 | |

`trust` 是安全問題，呼叫端不應該自動換到別的來源後默默繼續。

## 5. API 連結換了

API 連結預設固定、長期不換。真的換了（例如服務提供者跑了 `llmop tunnel rotate-api`），SDK 回
`SdkError(kind_hint="link_changed")`。**SDK 不會自己去找新連結**；向服務提供者拿新連結後：

```bash
llmop client set-link --profile home https://api.example.test/<新的秘密路徑>
```

程式裡是 `client.set_link(url)`（預設會試連 `<連結>/health`，回 True／False）。API key 與釘選都不受影響。

## 6. 金鑰：釘選、撤銷、刪除

| 指令 | 程式 | 做什麼 |
|---|---|---|
| `llmop client profiles` | `list_profiles()` | 列出設定檔（連結只顯示主機） |
| `llmop client pin --fingerprint X` | `client.pin_worker(X)` | 核對並釘選 worker 公鑰（伺服器換 worker 金鑰後用） |
| `llmop client unpin X` | `client.unpin_worker(X)` | 取消釘選 |
| — | `client.pending_worker_keys()` | 伺服器公布了、但還沒釘選的指紋（只提醒，**絕不自動信任**） |
| — | `client.rotate_signing_key(password)` | 換簽章金鑰：要密碼登入（不准只憑 API key 換公鑰） |
| `llmop client revoke` | `client.revoke(password)` | 在伺服器撤銷這把 key（密碼提示輸入），本機標 `revoked` |
| `llmop client forget [--yes]` | `client.forget()` | 刪掉本機設定檔與簽章私鑰（不撤銷伺服器上的 key） |
| `llmop client self-check [--fix]` | `client.self_check(fix=False)` | 見下 |

`self-check` 不送任何對話內容，檢查：檔案權限只有本人、設定檔完整、API 連結連得到、key 有效、
（必須加密的 key）明文被拒、（加密）用一把沒登記過的簽章金鑰送的請求被拒、有沒有還沒釘選的新 worker 金鑰。

## 7. 私鑰檔的權限

`config.json`（有 API key）與 `signing_key.json`（有簽章私鑰）都收緊成**只有目前使用者**：

- **Linux／macOS**：資料夾 `0700`、檔案 `0600`，跟 OpenSSH 對 `~/.ssh` 的要求一樣。檔案先以 `0600` 建立再寫入內容。
- **Windows**：沒有 `0600`，改用 ACL。SDK 建檔後執行（`<SID>` 是目前使用者的 SID，用 `whoami /user` 取得）：

  ```bat
  icacls <檔案> /inheritance:r /grant:r *<SID>:(F)
  icacls <設定檔資料夾> /inheritance:r /grant:r *<SID>:(OI)(CI)(F)
  ```

  `/inheritance:r` 移除從上層繼承來的權限，`/grant:r` 只給本人完整控制；資料夾加 `(OI)(CI)` 讓之後在裡面新建的檔案也只給本人。
  用 SID（`*S-1-5-…`）而不是帳號名稱，避免中文版 Windows 的群組名稱與名稱解析問題；`whoami.exe`、`icacls.exe`
  都用 `%SystemRoot%\System32` 的完整路徑呼叫，不靠 PATH。
- **限制**：本機系統管理員一律可以取得檔案擁有權再讀（Windows 的設計，任何檔案權限都擋不住）；有人拿到這台電腦的
  系統管理員權限，就當作私鑰外流，請撤銷 key（`llmop client revoke`）再重新 enroll。更進一步（用 DPAPI 加密私鑰檔、
  或用系統的金鑰圈）這一版沒有做。
- 收緊失敗只記警告、不擋使用；`llmop client self-check` 會列出 FAIL，`--fix` 重新收緊。

## 8. 公開介面（v0.3 凍結）

```python
from llmop.sdk import (AsyncClient, Client, EnrollResult, KIND_HINTS, ModelEntry, PinResult, Profile,
                       SdkError, add_plain, enroll, list_profiles, set_link)

def list_profiles(*, home: str | os.PathLike | None = None) -> list[str]
def enroll(account_url: str, username: str, password: str, *, worker_fingerprint: str | None = None,
           profile: str = "default", encryption: str = "required", key_name: str = "",
           expires_days: int | None = None, replace_key_id: str | None = None, api_link: str | None = None,
           test: bool = False, home=None, http: httpx.Client | None = None) -> EnrollResult
def add_plain(api_link: str, api_key: str, *, profile: str = "default", max_tokens: int | None = None,
              home=None) -> Profile
def set_link(url: str, *, profile: str = "default", home=None) -> None

class Client:
    @classmethod
    def from_profile(cls, name: str = "default", *, home=None, http: httpx.Client | None = None) -> Client
    profile: Profile                 # 唯讀
    name: str                        # 唯讀
    transport: str                   # 唯讀；"e2e" | "plain"（auto 已解開）
    def models(self, *, transport: str | None = None) -> list[ModelEntry]
    def chat(self, body: dict, *, model: str | None = None, max_tokens: int | None = None,
             transport: str | None = None, deadline: float = 1800.0,
             on_queue: Callable[[dict], None] | None = None) -> dict          # OpenAI chat.completion
    def me(self) -> dict
    def set_link(self, url: str, *, check: bool = True) -> bool | None
    def link_ok(self) -> bool
    def pin_worker(self, fingerprint: str) -> PinResult
    def unpin_worker(self, key_id: str) -> None
    def pending_worker_keys(self) -> list[str]
    def rotate_signing_key(self, password: str) -> str                         # 回新的 sig_key_id
    def revoke(self, password: str) -> None
    def forget(self) -> list[str]
    def self_check(self, *, fix: bool = False) -> list[tuple[str, bool, str]]
    def close(self) -> None                                                    # 也可以 with Client... as c

class AsyncClient:                   # 同名方法都是 async；from_profile 同上；aclose()；async with
    ...

class SdkError(Exception):
    code: str; message: str; status: int | None; retry_after: float | None
    kind_hint: str                   # KIND_HINTS 其中之一

@dataclass(frozen=True) class ModelEntry:  id: str; owned_by: str = ""; key_id: str = ""; context_length: int | None = None
@dataclass(frozen=True) class PinResult:   key_id: str; newly_pinned: bool; models: tuple[str, ...] = ()
@dataclass(frozen=True) class EnrollResult:
    profile: str; transport: str; status: str; key_record_id: str; key_display: str
    sig_key_id: str = ""; signing_key_created: bool = False; pinned: tuple[str, ...] = ()
    api_link_display: str = ""; test_reply: str | None = None
```

`Profile` 是唯讀的設定檔內容（`name`、`api_link`、`transport`、`encryption`、`pinned_workers`、`status`…），
`api_key` 不出現在 repr。

## 9. 這一版不做

e2e 串流、前向保密、自動探索新的 API 連結、網頁自助註冊。v0.2 的 `llmop client proxy`（本機 OpenAI 相容代理，埠 8788）
與 `llmop.client.sdk`（aiohttp）照舊保留，給其他 OpenAI 相容工具用。
