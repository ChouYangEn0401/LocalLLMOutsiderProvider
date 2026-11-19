# LocalLLMOutsiderProvider（`llmop`）v0.2

把自己電腦上的 **Ollama / LM Studio / vLLM / llama.cpp** 分享給少數親友，透過 **Cloudflare 通道**對外，前面加上
**帳號、API key、等級與用量、排隊**。API 跟 OpenAI 相容。只有**一個模型**：請求裡的 `model` 一律換成 `config.yaml` 設定的那一個。

交付物有兩樣：

1. **伺服器**（`llmop serve`）：閘道＋網頁＋（可選）worker，跑在有 GPU 的那台電腦上。
2. **用戶端外掛**（`llmop client`）：只有**加密模式**才需要，裝在使用者的電腦上。直連（明文）模式的使用者什麼都不用裝，
   用任何 OpenAI 相容工具即可。

> 這是給少數人用的小型服務，不是商業級服務。v0.2 還沒在真的 Windows／真的 LLM／真的 cloudflared 上實測過，見
> [docs/HANDOFF.md](docs/HANDOFF.md)。

## 三個入口

`llmop serve` 在同一個程式裡開三個入口，**全部只綁 `127.0.0.1`**；對外只靠兩個 Cloudflare 通道：

| 入口 | 位址 | 誰用 | 有什麼 |
|---|---|---|---|
| 本機控制台 | `http://127.0.0.1:8781/` | 只有管理員（這台電腦） | 帳號與等級、GUEST key、通道狀態與「更換 API 連結」、佇列、worker、設定檔；外加**免 key** 的 `http://127.0.0.1:8781/v1` |
| 帳號網站 | 本機 `127.0.0.1:8780` → 通道 `ui` 的 `https://<ui 主機>` | 有帳號的人 | 登入後：狀態、兩個用量表、自己的 API key（建立／撤銷／逐把選擇是否加密）、**目前的 API 連結** |
| API 連結 | 本機 `127.0.0.1:8782` → 通道 `api` 的 `https://<api 主機>/<秘密路徑>` | 程式 | `/v1/*`（明文直連）、`/e2e/v1/*`（加密）、`/worker/ws`（遠端 worker） |

- 秘密路徑是門牌，API key 另外帶（`Authorization: Bearer sk-llop-…`）。路徑不對一律 404。
- 本機控制台**沒有任何通道指向它**，而且會拒絕帶轉送標頭的請求；外網只能看、不能管理。
- 公開的 `/v1` 預設不串流（quick tunnel 對 SSE 不穩）：`stream: true` 會收到一個裝著完整回答的 SSE chunk。要開真串流設
  `server.public_stream: true`。本機 `/v1` 一律可以串流。

## 架設

### Windows 免安裝版（推薦）

不需要先裝 Python。

1. 準備好 LLM：Ollama（`ollama pull qwen3.5:9b`）或 LM Studio（載入模型、開啟本機伺服器）。
2. 下載 `llmop-0.2.0-windows-x64-portable.zip`，解壓縮到任意資料夾。
3. 雙擊 `Start.bat`（只在本機）或 `Start-With-Tunnel.bat`（加兩個公開網址；沒有 cloudflared 會問要不要用 winget 安裝）。
   第一次會問：用哪一種 LLM 伺服器、模型名稱、admin 密碼，然後建立 `config.yaml`。
4. 瀏覽器打開本機控制台 `http://127.0.0.1:8781/`，用 admin 登入。

| 檔案 | 用途 |
|---|---|
| `Start.bat` | 啟動整個節點，打開本機控制台（通道依 `config.yaml`） |
| `Start-With-Tunnel.bat` | 同上，並強制開兩個通道 |
| `Rotate-API-Link.bat` | 更換 API 連結（可加 `--change-host`、`--grace 600`） |
| `Add-User.bat` | 新增帳號 |
| `Check.bat` | 檢查設定與 LLM 連線 |
| `Start-Worker.bat` | 加密模式：只跑 worker 的另一台電腦 |
| `Start-Client-Proxy.bat` | 加密模式：使用者那一側的本機代理 |
| `Command-Prompt.bat` | 可以直接打 `llmop …` 指令的視窗 |

> **警告：資料夾裡的 `data\` 含私鑰。** 資料庫（API key 雜湊、帳號用量）在 `data\`，worker 與用戶端的**私鑰**在
> `data\llmop_home\`。不要上傳、分享、放進雲端同步資料夾；更新版本時把 `config.yaml` 與整個 `data\` 複製到新資料夾。

### 已經有 Python（3.11 以上）

```bash
pip install "localllm_outsider_provider-0.2.0-py3-none-any.whl[secure]"
```

```bash
llmop init --backend-type ollama --model qwen3.5:9b
```

```bash
llmop serve --tunnel --open
```

`llmop init` 建立 `config.yaml` 並設定 admin 密碼（LM Studio 用 `--backend-type lmstudio`）。不加 `--tunnel` 時，通道依
`tunnels.ui.enabled`／`tunnels.api.enabled`；`--no-tunnel` 則完全不開。`[secure]` 是加密模式要的套件；只用直連可以省略。
worker 與用戶端的私鑰放在 `$LLMOP_HOME`（預設 `~/.llmop`），同樣不能外流。

### 直連模式（明文）

上面那樣啟動就是直連模式。使用者拿 **API 連結＋API key** 就能用任何 OpenAI 相容工具：

```python
from openai import OpenAI
client = OpenAI(base_url="https://<api 主機>/<秘密路徑>/v1", api_key="sk-llop-...")
print(client.chat.completions.create(model="any", messages=[{"role": "user", "content": "你好"}]).choices[0].message.content)
```

端點：`GET /v1/models`（只有一筆）、`POST /v1/chat/completions`、`POST /v1/completions`、`POST /v1/embeddings`、`GET /v1/me`
（等級、NORMAL／LOW、兩個用量表的剩餘量與重置時間、排隊位置）。錯誤格式跟 OpenAI 一樣，429 會帶 `Retry-After`。
直連的內容閘道與 Cloudflare 都看得到。

### 加密模式

內容在使用者的電腦上加密，只有 worker 解得開；閘道與 Cloudflare 只看得到中繼資料。

1. 伺服器的 `config.yaml` 設 `roles: [gateway, worker]`，執行一次 `llmop worker init`，確認 `~/.llmop/worker/config.json`
   （免安裝版是 `data\llmop_home\worker\config.json`）的 `backends` 指向你的 LLM，重新啟動 `llmop serve`。
   worker 也可以放在另一台電腦（`Start-Worker.bat` 或 `llmop worker run`），步驟見 [docs/secure/DEPLOY.md](docs/secure/DEPLOY.md)。
2. 記下 worker 印出的 `public_key`（`llmop worker show-key`），用閘道以外的管道交給使用者。
3. 使用者：`llmop client keygen` → 在帳號網站建立 key，「加密」選「必須加密」並貼上簽章公鑰 → 建立
   `~/.llmop/client/config.json`（`gateway_url` 是 API 連結、`api_key`、`pinned_workers`）→ `llmop client proxy`。
4. 程式指向 `http://127.0.0.1:8788/v1`。

細節、金鑰輪替與錯誤碼：[docs/secure/README.md](docs/secure/README.md)、[docs/secure/DEPLOY.md](docs/secure/DEPLOY.md)。

## 怎麼發 key

- **帳號**：`llmop add-user friend1`（或 `Add-User.bat`；`--generate` 產生隨機密碼）。等級寫在 `config.yaml`：
  `accounts.friend1.tier: normal`（`vip3`／`vip2`／`vip1`／`normal`），存檔就生效。把帳號網站網址與密碼交給對方。
- **帳號的 key**：對方登入帳號網站，在「我的 API 金鑰」自己建立；每把 key 選「加密：可選／必須加密」。key **只顯示一次**。
  預設每個帳號一把（`max_api_keys` 可放寬）。管理員可以在本機控制台撤銷任何 key（或 `llmop keys`、`llmop revoke-key ID`）。
- **GUEST（臨時）key**：沒有帳號的人。在本機控制台「GUEST 金鑰」發：標籤、總額（預設 50,000 tokens）、到期天數、是否加密、
  是否每週重置。把 **API 連結＋key** 交給對方，對方用 `GET <API 連結>/v1/me` 查剩餘量。用完或到期後回 401。

## 等級與用量（6 行）

- 等級：VIP III、VIP II（不設用量表）→ VIP I → NORMAL（預設）→ GUEST；排隊時依這個順序，同級輪流。
- 兩個用量表：「1.5 小時用量」是會慢慢回水的桶（NORMAL 80,000 tokens，90 分鐘從空到滿），「每週用量」每週日 04:00 重置（NORMAL 1,000,000）。
- NORMAL 的桶見底會變 **LOW**（排在所有人後面），桶回到 25% 自動回 NORMAL；VIP I 見底只降到 NORMAL 的順位。
- GUEST 每把 key 有總額，預設不重置；桶見底回 429 等回水。
- 全域同時名額 `queue.slots` 預設 6，必須配合 LLM 伺服器真正的平行數與 context（LM Studio／llama.cpp 會平分 context）。
- 用量在請求結束後才記帳；所有數字都在 `config.yaml` 的 `tiers`／`usage`／`queue`，規則見 [docs/plan/TIERS_AND_USAGE_RULES.md](docs/plan/TIERS_AND_USAGE_RULES.md)。

## 兩個通道與更換 API 連結

- 通道 `ui`（帳號網站）幾乎不重啟；通道 `api`（API 連結）可以隨時換掉，兩者互不影響。
- quick tunnel 的網址在每次啟動 cloudflared 時都會變；帳號網站的網址變了，要另外通知使用者（LINE 等）。要固定網址，在
  `tunnels.ui`／`tunnels.api` 設 `mode: named`、`token`、`public_url`。
- 看目前的 API 連結：

```bash
llmop tunnel show-api
```

- 更換（舊連結立刻失效，**API key 不受影響**；使用者登入帳號網站就看得到新連結，程式改一個 `base_url` 即可）：

```bash
llmop tunnel rotate-api
```

```bash
llmop tunnel rotate-api --change-host --grace 600
```

  `--change-host` 連主機名一起換（只重啟通道 `api`，要有執行中的 `llmop serve`）；`--grace 600` 讓舊連結再通 10 分鐘。
  本機控制台與 `Rotate-API-Link.bat` 做的是同一件事。每次更換都會留稽核紀錄。

## 讓 SYNOX 使用（在 SYNOX 那一側設定）

本專案不改 SYNOX。以下是擁有者在 SYNOX 設定裡要做的事，三種擇一：

1. **同一台電腦，免 key**：SYNOX 的 LLM 設定 `kind: local`、`server: other`、`base_url: http://127.0.0.1:8781`
   （要求含 `/v1` 時填 `http://127.0.0.1:8781/v1`），API key 欄位必填就隨便填。用量記在 `local_account`（預設 admin）。
2. **直連，經 API 連結**：用 `openai_compat`，`base_url: https://<api 主機>/<秘密路徑>/v1`，API key 填一把「加密：可選」的
   `sk-llop-…`。更換 API 連結後要改 `base_url`。公開入口預設不串流，SYNOX 若要求串流會收到一個完整的 chunk。
3. **加密，經用戶端外掛**：在 SYNOX 那台電腦跑 `llmop client proxy`（設定見上面「加密模式」），SYNOX 設
   `kind: local`、`server: other`、`base_url: http://127.0.0.1:8788`，`model` 填 `llmop client models` 列出的名稱。
   更換 API 連結時只改代理的 `gateway_url`，SYNOX 不用動。

## 常用指令

| 指令 | 用途 |
|---|---|
| `llmop init` | 建立 `config.yaml` 和 admin 密碼（`--backend-type`、`--backend-url`、`--model`） |
| `llmop serve` | 啟動整個節點（`--tunnel`／`--no-tunnel`、`--worker`／`--no-worker`、`--open`） |
| `llmop check` | 檢查設定和 LLM 是否連得上 |
| `llmop add-user NAME` | 新增帳號；`llmop set-password NAME` 改密碼 |
| `llmop usage` / `llmop keys` / `llmop revoke-key ID` | 看用量、key，撤銷 key |
| `llmop tunnel show-api` / `llmop tunnel rotate-api` | 看、更換 API 連結 |
| `llmop worker init` / `llmop worker run` | 加密模式的 worker |
| `llmop client keygen` / `llmop client proxy` | 加密模式的用戶端外掛 |

全域選項 `-c PATH` 指定設定檔（預設 `$LLMOP_CONFIG` 或 `config.yaml`）。範例設定見 [config.example.yaml](config.example.yaml)。

## 一定要知道的限制

- **非串流請求超過約 100 秒會被 Cloudflare 回 524。** 加密模式自動改用輪詢；直連的長回答請開 `server.public_stream` 或縮短 `max_tokens`。
- **Ollama 預設 context 很小**，超過會靜默截斷前面的內容：在 Ollama 那邊設 `OLLAMA_CONTEXT_LENGTH=8192`。LM Studio 回
  「Context size has been exceeded」時，閘道轉成 400 `context_length_exceeded`；完整處理排在 v0.3。
- **加密模式在 v0.2 不做真串流**（代理送一個完整的 chunk）。
- **不要把 LLM 伺服器（11434、1234）或本機控制台（8781）對外。** 只用 `llmop serve` 自己開的兩個通道。
- **`image_url` 沒有過濾（v0.3 才處理）。** 閘道原樣轉送訊息裡的 `image_url`；如果你的後端（例如 vLLM、部分 LM Studio
  版本）會自己去下載網址，持有 key 的人就能讓後端去連你內網的網址（SSRF）。v0.2 請用不會抓遠端圖片的後端，或只發 key 給信任的人。
- **VIP 帳號的 API 金鑰只能在本機控制台建立**（AI 預設，R-10）：VIP 沒有或只有很高的用量上限，帳號網站（外網）拒絕 VIP I／II／III 建立 key；一般帳號照常在帳號網站自己建立。
- **更換 API 連結後，別台電腦的 worker 要改設定。** 它們的 `gateway_url` 是 `wss://<API 連結>/worker/ws`；經由舊連結連入的 worker 在舊連結失效時會被切斷（本機的 worker 不受影響）。
- Docker 與 CI 檔案是 v0.1 時代的，還沒有為 v0.2 更新（[docs/HANDOFF.md](docs/HANDOFF.md)）。

## 安全邊界

以下是加密模式的安全邊界（與 [docs/secure/SECURITY.md](docs/secure/SECURITY.md) 一字不差）：

> **安全邊界**
>
> 本系統的端對端加密保護的是「內容」：對話訊息、工具定義與模型的回覆，從用戶端加密後才送出，只有持有 worker 私鑰的那台機器能解開。閘道（gateway）的營運者、閘道的紀錄檔、Cloudflare 以及網路路徑上的任何人，都看不到內容，也無法竄改而不被發現。
>
> 以下是本系統**不**保護、也無法保護的部分：
>
> - **直連（明文）的請求。** 送到 `/v1`（API 連結或本機入口）的請求沒有端對端加密，閘道與 Cloudflare 都看得到內容。只有經用戶端外掛送到 `/e2e/v1` 的請求受上面的保護；要讓一把 key 只能加密使用，建立時選「必須加密」（`encryption: required`）。
> - **worker 機器本身。** 模型必須看到明文才能推論。能控制 worker 機器的人（或該機器上的惡意程式）看得到全部內容。請只把請求送給你信任的 worker。閘道與 worker 在同一台機器時（`roles: [gateway, worker]`），閘道的營運者也就看得到內容。
> - **中繼資料。** 閘道與 Cloudflare 看得到：哪一把 API 金鑰、哪個模型、哪一台 worker、什麼時候、`max_tokens`、用掉的 token 數，以及密文大小（以 1 KiB 為單位）。
> - **請求沒有前向保密。** 請求是用 worker 的長期公鑰加密的。若有人先錄下密文，日後又取得該 worker 的私鑰，就能解開當時錄下的請求。多輪對話的請求裡會帶著先前的回覆，所以那些回覆也會跟著曝光。回覆本身用一次性金鑰加密，單獨來看不受長期金鑰外洩影響。請定期輪替 worker 金鑰並刪除舊私鑰。
> - **金鑰的來源。** 用戶端必須用 worker 主控台顯示的指紋來釘選公鑰。若直接相信閘道提供的公鑰，惡意的閘道可以換成自己的金鑰。
> - **可用性。** 閘道或 Cloudflare 可以丟棄、延遲請求，本系統不防阻斷服務。
> - **被入侵的閘道可以佔用 worker 的運算資源**（除非 worker 設定了 `pinned_clients`），但仍然讀不到任何人的內容。
>
> 用戶端與 worker 的時鐘誤差必須在五分鐘以內，否則請求會被當成過期而拒絕。

## 開發

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev,secure]"
```

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/python -m build
```

```bash
.venv/bin/python scripts/build_portable.py
```

文件：[交接文件](docs/HANDOFF.md)（狀態、v0.3 待辦、發版方式）· [加密模式](docs/secure/README.md) ·
[等級與用量規則](docs/plan/TIERS_AND_USAGE_RULES.md) · [合併計畫（決策表在第 10 節）](docs/plan/MERGE_PLAN.md)

## License

MIT
