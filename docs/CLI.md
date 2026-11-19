# llmop 指令參考（CLI）

`llmop` 是 LocalLLMOutsiderProvider 的命令列工具：把你電腦上的本機 LLM（Ollama、LM Studio、vLLM、llama.cpp 等）
分享給少數親友，附帶登入、API 金鑰、用量額度與（可選）Cloudflare 通道。本文件列出**每一個**指令與參數，
內容以 `src/llmop/cli.py`、`src/llmop/worker/cli.py`、`src/llmop/client/cli.py` 與實際 `--help` 為準。

執行方式（三種等價）：

```bat
llmop <指令> ...                         :: 已啟用 venv 或已安裝到 PATH
.venv\Scripts\llmop.exe <指令> ...        :: 直接呼叫 venv 內的執行檔
.venv\Scripts\python.exe -m llmop <指令>  :: .bat 檔實際用的方式
```

> 免安裝版（portable zip）可雙擊 `Command-Prompt.bat`，開出的視窗裡直接打 `llmop ...` 即可。

---

## 1. 常見流程（快速上手）

以下指令都在專案根目錄（有 `config.yaml` 的資料夾）執行。

### 1.1 第一次設定：Ollama

```bat
:: 先確認 Ollama 已啟動並拉好模型：ollama pull qwen3.5:9b
llmop init --backend-type ollama --model qwen3.5:9b
llmop check
llmop serve --open
```

`llmop init` 會詢問 admin 密碼（輸入時不顯示字元），建立 `config.yaml`。Ollama 預設網址 `http://127.0.0.1:11434`，通常不用指定 `--backend-url`。

### 1.2 第一次設定：LM Studio

LM Studio 的埠號以**它自己畫面上顯示的為準**（程式內建預設值是 1234，但常被改成別的，例如 1235）。
請務必用 `--backend-url` 明確指定：

```bat
:: 先在 LM Studio 載入模型並開啟本機伺服器，記下它顯示的網址
llmop init --backend-type lmstudio --backend-url http://localhost:1235 --model <LM Studio 顯示的模型名稱>
llmop check
llmop serve --open
```

`llmop check` 會實際連線測試後端；連不上就先檢查埠號與模型名稱。

### 1.3 啟動：不開通道 / 開通道

```bat
llmop serve                    :: 依 config.yaml 的 tunnels.*.enabled 決定（預設通常不開通道）
llmop serve --no-tunnel        :: 強制不開通道，只在本機使用
llmop serve --tunnel --open    :: 開兩條 Cloudflare 快速通道（需先安裝 cloudflared），並開啟本機主控台
```

開通道需要 `cloudflared`：`winget install --id Cloudflare.cloudflared`。快速通道的網址每次啟動都會變，
會印在視窗裡，也顯示在本機主控台（`http://127.0.0.1:8781/`）。關掉視窗即停止。

### 1.4 新增使用者

```bat
llmop add-user friend1 --generate                       :: 產生隨機密碼並印出
llmop add-user friend2 --tokens-per-day 50000 --models qwen3.5:9b
```

執行中的 llmop 會自動讀到新帳號，**不需重啟**。

### 1.5 更換 API 連結

API 連結含秘密路徑（`https://....trycloudflare.com/<秘密>/v1`），外流時要換掉：

```bat
llmop tunnel show-api                                    :: 看目前連結
llmop tunnel rotate-api                                  :: 立刻換新連結，舊的馬上失效
llmop tunnel rotate-api --grace 600 --reason "外流"      :: 舊連結再保留 10 分鐘
llmop tunnel rotate-api --change-host --grace 600        :: 連主機名一起換（需有執行中的 llmop serve）
```

### 1.6 檢查

```bat
llmop check                   :: 檢查設定、列出帳號、測試 LLM 後端
llmop usage --days 7          :: 近 7 天用量
llmop keys                    :: 列出 API 金鑰（不顯示秘密部分）
```

### 1.7 加密模式（端對端加密：worker + client）

需要安裝 `secure` 額外套件（`pip install "localllm-outsider-provider[secure]"`；免安裝版已包含）。

**伺服器端（有 GPU 的電腦）：**

```bat
:: config.yaml 設 roles: [gateway, worker]
llmop worker init              :: 建立 worker 金鑰與 config.json，印出 key_id 與 public_key
:: 編輯 worker 的 config.json，確認 backends 指向你的 LLM（LM Studio 請改成實際埠號）
llmop serve                    :: 重新啟動，worker 隨節點一起跑
llmop worker show-key          :: 把 public_key 以「閘道以外」的管道交給使用者
```

worker 若放在另一台電腦：

```bat
llmop worker init
:: 編輯 config.json：gateway_url 改成 wss://<API 連結>/worker/ws
llmop worker run --reg-token llmop_reg_...    :: 第一次需要註冊權杖，向閘道管理員索取
```

**使用者端：**

```bat
llmop client keygen            :: 建立簽章金鑰，印出 public_key 與 sig_key_id
:: 在帳號網站建立 API key，「加密」選「必須加密」，貼上簽章公鑰
:: 建立 client 的 config.json（見 4.3 節範例）：gateway_url、api_key、pinned_workers
llmop client models            :: 列出已釘選 worker 提供的模型
llmop client chat --model <名稱> "你好"
llmop client proxy             :: 在 127.0.0.1:8788 啟動 OpenAI 相容代理，其他程式連它
```

---

## 2. 指令總表

| 指令 | 一句話用途 |
|---|---|
| `llmop init` | 由範本建立 `config.yaml`、設定 admin 密碼與 LLM 後端 |
| `llmop hash-password` | 印出密碼雜湊，可手動貼進 `config.yaml` |
| `llmop add-user` | 在 `config.yaml` 新增帳號（保留註解） |
| `llmop set-password` | 修改帳號密碼 |
| `llmop serve` | 啟動整個節點（主控台、帳號網站、API 連結、通道、worker） |
| `llmop tunnel` | 只跑 Cloudflare 通道（給已在跑的伺服器） |
| `llmop tunnel show-api` | 印出目前的 API 連結 |
| `llmop tunnel rotate-api` | 產生新的秘密 API 連結 |
| `llmop check` | 驗證設定、列出帳號、測試 LLM 後端 |
| `llmop usage` | 各帳號 token 用量 |
| `llmop keys` | 列出 API 金鑰 |
| `llmop revoke-key` | 以 ID 撤銷 API 金鑰 |
| `llmop gateway run` / `gateway init` | `serve` / `init` 的別名 |
| `llmop worker init` | 建立 worker 金鑰與設定範本（加密模式） |
| `llmop worker show-key` | 印出目前與仍接受的 worker 金鑰 |
| `llmop worker rotate-key` | 產生新 worker 金鑰 |
| `llmop worker prune-keys` | 永久刪除舊 worker 私鑰 |
| `llmop worker run` | 連到閘道並開始服務 |
| `llmop client keygen` | 建立用戶端簽章金鑰 |
| `llmop client proxy` | 本機 OpenAI 相容加密代理 |
| `llmop client chat` | 送一筆請求並印出回覆 |
| `llmop client models` | 列出已釘選 worker 的模型 |

### 全域選項（節點類指令）

| 參數 | 說明 |
|---|---|
| `--config PATH`、`-c PATH` | 設定檔路徑。預設 `$LLMOP_CONFIG`，否則 `config.yaml`（相對於目前資料夾）。可放在指令前或後。 |
| `--version` | 顯示版本（只能放在指令之前：`llmop --version`）。 |
| `-h`、`--help` | 說明。 |

> `worker` 與 `client` 是獨立的子 CLI，**不吃** `--config`；它們的設定在 `$LLMOP_HOME`（見第 4 節）。

---

## 3. 節點指令

### 3.1 `llmop init`

**用途**：由內建範本建立 `config.yaml`，設定 admin 密碼，並可一併設定第一個 LLM 後端與模型。

**語法**
```
llmop init [--force] [--admin-password PW] [--backend-type TYPE] [--backend-url URL] [--model NAME]
```

| 參數 | 意義 / 預設 / 何時用 |
|---|---|
| `--force` | 覆寫已存在的 `config.yaml`（舊檔會備份成 `config.yaml.bak`）。沒加此參數且檔案已存在時，指令直接失敗、不改任何東西。 |
| `--admin-password PW` | admin 密碼。預設讀環境變數 `LLMOP_ADMIN_PASSWORD`，沒有就互動詢問（不顯示輸入）。給腳本用；寫在命令列會留在歷史紀錄，一般手動執行建議用互動輸入。 |
| `--backend-type TYPE` | LLM 伺服器種類，可選 `ollama`、`vllm`、`lmstudio`、`llamacpp`、`openai`。未指定時沿用範本（Ollama）。 |
| `--backend-url URL` | LLM 伺服器網址。預設依種類：ollama `http://127.0.0.1:11434`、vllm `http://127.0.0.1:8000`、lmstudio `http://127.0.0.1:1234`、llamacpp `http://127.0.0.1:8080`、openai `http://127.0.0.1:8000`。**LM Studio 請務必以它畫面顯示的埠號指定**。 |
| `--model NAME` | 所有請求共用的那一個模型，例如 `qwen3.5:9b`。 |

**範例**
```bat
llmop init --backend-type ollama --model qwen3.5:9b
llmop init --backend-type lmstudio --backend-url http://localhost:1235 --model my-model
llmop init --force
```

**注意**
- 會寫入 `config.yaml`（admin 密碼以雜湊存放，不存明碼）。
- 只指定 `--backend-url` 而不給 `--backend-type` 時，種類視為 `ollama`。
- 之後要改密碼用 `llmop set-password admin`，不要重跑 `init --force`。

### 3.2 `llmop hash-password`

**用途**：算出密碼雜湊，手動貼進 `config.yaml` 的 `password_hash`。

**語法**：`llmop hash-password [--password PW]`

| 參數 | 說明 |
|---|---|
| `--password PW` | 要雜湊的密碼；預設互動詢問（不顯示）。 |

**範例**：`llmop hash-password`

**注意**：只印出雜湊，不改任何檔案。一般情況直接用 `add-user` / `set-password` 即可。

### 3.3 `llmop add-user`

**用途**：在 `config.yaml` 新增帳號，保留檔案內的註解。

**語法**
```
llmop add-user USERNAME [--role {user,admin}] [--password PW | --generate] [--display-name TEXT]
                [--models LIST] [--tokens-per-day N] [--tokens-per-month N] [--requests-per-minute N]
```

| 參數 | 意義 / 預設 / 何時用 |
|---|---|
| `USERNAME`（必填） | 帳號名稱，可用英數字與 `. _ @ -`。 |
| `--role` | `user`（預設）或 `admin`。 |
| `--password PW` | 密碼。預設互動詢問（不顯示）。 |
| `--generate` | 產生隨機密碼並印出（只會顯示這一次，請當場轉交）。分享給朋友時最方便。 |
| `--display-name TEXT` | 顯示名稱。 |
| `--models LIST` | 此帳號可用的模型，以逗號分隔；預設 `"*"`（全部）。 |
| `--tokens-per-day N` | 每日 token 上限；`0` 為不限；省略時用 `config.yaml` 的 `default_quota`。 |
| `--tokens-per-month N` | 每月 token 上限。 |
| `--requests-per-minute N` | 每分鐘請求數上限。 |

**範例**
```bat
llmop add-user bob --generate
llmop add-user bob --tokens-per-day 50000 --requests-per-minute 10 --generate
llmop add-user carol --role admin
```

**注意**：修改 `config.yaml`。**執行中的 llmop 會自動套用，不需重啟。** `Add-User.bat` 就是它的互動式包裝。

### 3.4 `llmop set-password`

**用途**：修改某帳號在 `config.yaml` 中的密碼。

**語法**：`llmop set-password USERNAME [--password PW | --generate]`

| 參數 | 說明 |
|---|---|
| `USERNAME`（必填） | 要改密碼的帳號。 |
| `--password PW` | 新密碼；預設互動詢問。 |
| `--generate` | 產生隨機密碼並印出。 |

**範例**：`llmop set-password admin`、`llmop set-password bob --generate`

**注意**：修改 `config.yaml`。（是否需重啟請以實測為準；`add-user` 已確認會熱載入，本指令走同一個設定檔編輯機制。）

### 3.5 `llmop serve`

**用途**：啟動整個節點。依 `config.yaml` 的 `listeners` / `tunnels` / `roles`，同時提供：
本機主控台與免金鑰 `/v1`（`http://127.0.0.1:8781`，僅本機）、帳號網站（`8780`，通道 `ui`）、
API 連結（`8782`，通道 `api`）、以及（若 `roles` 含 worker）worker。

**語法**
```
llmop serve [--host HOST] [--port N] [--tunnel | --no-tunnel] [--worker | --no-worker]
            [--open] [--log-level LEVEL]
```

| 參數 | 意義 / 預設 / 何時用 |
|---|---|
| `--tunnel` / `--no-tunnel` | 強制開啟 / 不開通道。預設（都不給）依 `tunnels.*.enabled`；若用 `--port`（舊模式）則依 `tunnel.enabled`。 |
| `--worker` / `--no-worker` | 是否在此程序內執行 worker。預設依 `roles` 是否含 worker。 |
| `--open` | 啟動後用瀏覽器開啟本機網頁。 |
| `--log-level` | `critical` `error` `warning` `info`（預設）`debug` `trace`。 |
| `--host HOST` | **v0.1 單一入口相容模式**：監聽位址。一般不要用。 |
| `--port N` | **v0.1 單一入口相容模式**：埠號（舊預設 8780）。一般不要用；用了就不是 v0.2 的三入口架構。 |

**範例**
```bat
llmop serve
llmop serve --tunnel --open
llmop serve --no-tunnel --log-level debug
```

**注意**
- 前景執行，關閉視窗或 Ctrl+C 即停止。
- 修改 `config.yaml` 的大部分結構性設定（listeners、tunnels、roles、backends）**需重啟 `serve`**；新增帳號不用。
- 會在 `data\`（`server.data_dir`，或環境變數 `LLMOP_DATA_DIR` 覆寫）建立資料庫、API 連結的秘密與簽章等狀態；加密模式的 worker 私鑰在 `$LLMOP_HOME\worker\`。這些都不可外流。
- 快速通道網址每次啟動不同；想固定網址需設定具名通道／`server.public_url`（見 `config.example.yaml`）。
- 對應 `.bat`：`Start.bat`（`serve`）、`Start-With-Tunnel.bat`（`serve --tunnel`）。

### 3.6 `llmop tunnel`

**用途**：不帶動作時，**只**跑 Cloudflare 通道，給已在另一個視窗執行的伺服器用（若 `llmop serve --tunnel` 已在跑就不要再開，兩條通道沒意義）。
帶動作 `show-api` / `rotate-api` 則管理 API 連結。

**語法**
```
llmop tunnel [show-api | rotate-api] [--change-host] [--grace SECONDS] [--reason TEXT]
             [--url URL] [--cloudflared PATH]
```

| 參數 | 意義 / 預設 / 何時用 |
|---|---|
| `show-api` | 印出目前的 API 連結（含秘密路徑，不要貼到公開處）。 |
| `rotate-api` | 產生新的秘密 API 連結。舊連結預設立刻失效。 |
| `--change-host` | 僅 `rotate-api`：連同通道主機名一起換（重啟 `api` 通道）。需有執行中的 `llmop serve`。 |
| `--grace SECONDS` | 僅 `rotate-api`：舊連結再保留多少秒，預設 `0`。要讓使用者有時間改設定時用，例如 `600`。 |
| `--reason TEXT` | 僅 `rotate-api`：寫進稽核紀錄的原因，預設空。 |
| `--url URL` | 僅「只跑通道」模式：本機伺服器網址，預設取自設定（`http://127.0.0.1:8780`）。 |
| `--cloudflared PATH` | `cloudflared(.exe)` 的路徑，預設自動尋找。 |

**範例**
```bat
llmop tunnel show-api
llmop tunnel rotate-api --grace 600 --reason "link leaked"
llmop tunnel rotate-api --change-host --grace 600
llmop tunnel --cloudflared "C:\Tools\cloudflared.exe"
```

**注意**
- `rotate-api` 會改寫 `data\` 內保存的 API 連結秘密；換完後，所有使用者與加密模式 worker 的 `gateway_url`（`wss://<API 連結>/worker/ws`）都要跟著更新。
- 另有一個隱藏參數 `--wait`（不在 `--help` 內），可忽略。
- 對應 `.bat`：`Rotate-API-Link.bat`（portable 版，會轉給 `tunnel rotate-api`）。

### 3.7 `llmop check`

**用途**：驗證 `config.yaml`、列出帳號、實際測試 LLM 後端是否連得上。**唯讀**，沒有自己的參數（只有全域 `--config`）。

**範例**：`llmop check`

**注意**：第一次設定完、換模型或換埠號後都先跑一次。對應 `.bat`：`Check.bat`。

### 3.8 `llmop usage`

**用途**：顯示各帳號的 token 用量。唯讀。

| 參數 | 說明 |
|---|---|
| `--days N` | 統計最近 N 天，預設 `30`。 |
| `--user NAME` | 只看某一帳號的明細。 |

**範例**：`llmop usage --days 7`、`llmop usage --user bob`

**注意**：資料庫尚不存在時只會提示尚無紀錄。

### 3.9 `llmop keys`

**用途**：列出 API 金鑰（**永不顯示秘密部分**，只顯示 ID 等資訊）。唯讀。

| 參數 | 說明 |
|---|---|
| `--user NAME` | 只列某帳號的金鑰。 |
| `--all` | 連已撤銷的金鑰一起列。 |

**範例**：`llmop keys --user bob --all`

### 3.10 `llmop revoke-key`

**用途**：以 ID 撤銷一把 API 金鑰（ID 從 `llmop keys` 取得）。

**語法**：`llmop revoke-key ID`

**範例**：`llmop revoke-key <ID>`

**注意**：寫入資料庫，撤銷後該金鑰立即不能用；無法復原，需讓使用者重新建立。

### 3.11 `llmop gateway run` / `llmop gateway init`

**用途**：舊稱的別名。`gateway run` 等同 `serve`，`gateway init` 等同 `init`，其後的參數原樣轉交。
`llmop gateway` 不帶或帶其他動作會印出用法。

---

## 4. 加密模式指令（需要 `secure` 額外套件）

沒有安裝 `secure` 套件時，`llmop worker ...` / `llmop client ...` 會印出一行安裝提示（`pip install "localllm-outsider-provider[secure]"`）而不是錯誤堆疊。
這兩組指令的狀態都放在 `$LLMOP_HOME`（見第 5 節），**包含私鑰與 API 金鑰，不可外流或放進雲端同步資料夾**。

### 4.1 `llmop worker`

在本機 LLM 伺服器旁執行的 worker。檔案位置：`$LLMOP_HOME\worker\`（`config.json`、`keys.json`、`state.json`）。

| 子指令 | 用途與注意 |
|---|---|
| `init` | 建立第一把金鑰（`keys.json`）與設定範本（`config.json`），印出 `key_id` 與 `public_key`。已存在則不變更。範本預設 `gateway_url` 為同機閘道 `ws://127.0.0.1:8781/worker/ws`、後端為 Ollama `http://127.0.0.1:11434`；**用 LM Studio 或 worker 在別台電腦時要手動編輯 `config.json`**。 |
| `show-key` | 印出目前金鑰與仍接受的舊金鑰（`key_id`、`public_key`、建立時間）。用戶端釘選的就是這裡的值。 |
| `rotate-key` | 產生新金鑰並設為目前金鑰；舊金鑰仍被接受直到 `prune-keys`。之後要通知用戶端更新釘選值，並**重啟執行中的 worker**。 |
| `prune-keys [--yes]` | **永久刪除**目前金鑰以外的舊私鑰（用它們加密的請求再也解不開）。預設會要求輸入 `yes` 確認；`--yes` 略過確認。刪除後要**重啟 worker**。 |
| `run [--reg-token TOKEN]` | 連到閘道並開始服務。第一次執行（尚無 `state.json`）必須帶 `--reg-token llmop_reg_...`（向閘道管理員索取）；已註冊後會忽略此參數。 |

```bat
llmop worker init
llmop worker show-key
llmop worker run --reg-token llmop_reg_xxxxxxxx
llmop worker rotate-key
llmop worker prune-keys --yes
```

對應 `.bat`（portable 版）：`Start-Worker.bat`（首次自動 `worker init` 並開啟編輯 `config.json`，之後 `worker run`）。若 `roles` 含 worker，`llmop serve` 會自己帶起 worker，不需另外 `worker run`。

### 4.2 `llmop client`

使用者電腦上的加密用戶端。全域選項：`-v` / `--verbose` 輸出除錯紀錄。檔案位置：`$LLMOP_HOME\client\`（`config.json`、`signing_key.json`）。

| 子指令 | 用途與注意 |
|---|---|
| `keygen` | 建立簽章金鑰（已存在就沿用），印出 `public_key` 與 `sig_key_id`。把 `public_key` 貼到帳號網站該 API 金鑰的「簽章公鑰」欄位，並用 `sig_key_id` 核對。 |
| `proxy` | 在 `127.0.0.1`（預設埠 `8788`，可由 `config.json` 的 `proxy_port` 改）啟動本機 OpenAI 相容代理；其他程式把 `base_url` 指到 `http://127.0.0.1:8788` 即可。 |
| `chat --model NAME [--max-tokens N] PROMPT` | 送出一筆請求並印出回覆。`--model` 必填；`--max-tokens` 預設取 `config.json` 的 `default_max_tokens`（1024）。 |
| `models` | 列出已釘選 worker 提供的模型（`模型<TAB>key_id`）；沒有在線 worker 時以代碼 1 結束。 |

```bat
llmop client keygen
llmop client models
llmop client chat --model qwen3.5:9b "請用一句話介紹你自己"
llmop client proxy
```

### 4.3 用戶端 `config.json` 範例

路徑：`$LLMOP_HOME\client\config.json`（免安裝版為 `data\llmop_home\client\config.json`）。以下全是佔位符：

```json
{
  "gateway_url": "https://<名稱>.trycloudflare.com/<gate>",
  "api_key": "sk-llop-...",
  "pinned_workers": ["<worker 的 public_key（b64u）>"],
  "proxy_port": 8788,
  "default_max_tokens": 1024
}
```

`gateway_url` 是管理員給的 API 連結（含秘密路徑，視同密碼）。API 連結更換後要同步更新。
`pinned_workers` 的值必須由**閘道以外的管道**取得（`llmop worker show-key`），這是防止被中間人冒充的關鍵。

---

## 5. 環境變數與 `.bat` 對照

### 環境變數

| 變數 | 作用 |
|---|---|
| `LLMOP_CONFIG` | 預設的 `config.yaml` 路徑（等同全域 `--config` 的預設值）。`.bat` 檔會設成專案根目錄的 `config.yaml`。 |
| `LLMOP_HOME` | worker / client 的狀態根目錄，預設 `~/.llmop`（Windows 即使用者資料夾下的 `.llmop`）。其下 `worker\`、`client\` 放私鑰與設定。免安裝版 `.bat` 會設成 `data\llmop_home`。 |
| `LLMOP_DATA_DIR` | 覆寫 `config.yaml` 的 `server.data_dir`（Docker 映像設為 `/data`）。 |
| `LLMOP_ADMIN_PASSWORD` | `llmop init` 的 admin 密碼預設來源（免互動）。 |
| `LLMOP_IN_CONTAINER` | 程式內有定義（Docker 環境旗標），一般使用者不用設。 |
| `LLMOP_BACKEND_TYPE`、`LLMOP_MODEL` | 僅 portable 版 `_first_run.bat` 讀取，用來預填首次設定問題。 |

### 哪些指令碰到私鑰 / 機敏資料

| 指令 | 讀寫的機敏資料 |
|---|---|
| `init`、`add-user`、`set-password`、`hash-password` | `config.yaml`（含密碼雜湊） |
| `serve`、`tunnel`（含 `rotate-api`）、`keys`、`revoke-key`、`usage` | `data\` 資料庫、API 連結秘密 |
| `worker init/rotate-key/prune-keys/run` | `$LLMOP_HOME\worker\keys.json`（worker 私鑰）、`state.json` |
| `client keygen/proxy/chat/models` | `$LLMOP_HOME\client\signing_key.json`（簽章私鑰）、`config.json`（API 金鑰） |

### `.bat` 對照（`scripts\windows\`；portable 版另有 `scripts\windows\portable\`）

| `.bat` | 實際執行 |
|---|---|
| `Setup.bat` | 建 `.venv`、安裝 llmop、`llmop init`、`llmop check` |
| `Start.bat` | `llmop serve`（額外參數會轉傳，例如 `--open`、`--no-tunnel`）；portable 版固定加 `--open` |
| `Start-With-Tunnel.bat` | `llmop serve --tunnel`（portable 版再加 `--open`） |
| `Add-User.bat` | `llmop add-user ...`（無參數時互動詢問） |
| `Check.bat` | `llmop check` |
| `Rotate-API-Link.bat`（portable） | `llmop tunnel rotate-api %*` |
| `Start-Worker.bat`（portable） | 首次 `llmop worker init`，之後 `llmop worker run %*` |
| `Start-Client-Proxy.bat`（portable） | 首次 `llmop client keygen`，之後 `llmop client proxy` |
| `Command-Prompt.bat`（portable） | 開一個可直接輸入 `llmop ...` 的命令視窗 |

---

## 6. 常見問題

- **`llmop check` 連不上 LM Studio**：埠號不對。以 LM Studio 畫面為準，重新 `llmop init --force --backend-type lmstudio --backend-url http://localhost:<埠號> --model <名稱>`，或直接編輯 `config.yaml` 的 `backends`。
- **`init` 說檔案已存在**：加 `--force`（會留 `.bak`），或改用 `set-password` 改密碼。
- **加密模式報 `needs the secure extra`**：`pip install "localllm-outsider-provider[secure]"`。
- **API 連結外流**：立刻 `llmop tunnel rotate-api`，並通知使用者與遠端 worker 更新連結。
