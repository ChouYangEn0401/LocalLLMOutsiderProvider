# 部署（加密模式）

本文件一步步說明怎麼把加密模式的三個角色跑起來，以及每一步要確認什麼。系統的樣子與安全邊界見 [README](README.md)，
線路格式見 [PROTOCOL.md](PROTOCOL.md)，密碼設計見 [SECURITY.md](SECURITY.md)。直連（明文）、帳號、等級與通道的設定見
[專案 README](../../README.md)。

v0.2 的配置：

| 角色 | 放在哪裡 | 對外連線 | 監聽 |
|---|---|---|---|
| 伺服器（`llmop serve`，含閘道） | 任何能執行 `cloudflared` 的機器 | 由 `cloudflared` 連出到 Cloudflare（兩個通道） | `127.0.0.1:8781`（本機控制台）、`8780`（帳號網站）、`8782`（API 連結），只有 loopback |
| worker | 本機 LLM 伺服器旁邊；可以就是伺服器本身 | WebSocket 連出到閘道 | 無 |
| 用戶端外掛 | 使用者的電腦 | HTTPS 連出到 API 連結 | 代理 `127.0.0.1:8788`（只有 loopback） |

伺服器的帳號、等級、用量、API 金鑰雜湊、worker 與其公鑰都在 `config.yaml` 與 `server.data_dir`（預設 `./data`）的 SQLite 裡。
worker 與用戶端的狀態檔在 `$LLMOP_HOME`（預設 `~/.llmop`；Windows 免安裝版是 `data\llmop_home`）底下、依角色分目錄，
目錄權限 0700，含秘密的檔案權限 0600（Windows 上權限位元沒有作用，請靠資料夾的存取權限保護）：

| 檔案 | 內容 |
|---|---|
| `worker/config.json` | worker 設定（閘道網址、後端、可選的 `pinned_clients`） |
| `worker/keys.json` | worker 的 X25519 私鑰，**沒有加密**（worker 必須能無人值守地啟動） |
| `worker/state.json` | 註冊後拿到的 `worker_id` 與 worker 權杖 |
| `client/config.json` | 用戶端設定（API 連結、API 金鑰、釘選的 worker 公鑰） |
| `client/signing_key.json` | 用戶端的 Ed25519 簽章私鑰 |

> **這些資料夾含私鑰。** 不要上傳、不要分享、不要放進雲端同步資料夾；備份時當成密碼看待。

---

## 1. 安裝

每台機器都需要 Python 3.11 以上（Windows 免安裝版已內含 Python 與以下套件）：

```
pip install "localllm-outsider-provider[secure]"
```

之後就有 `llmop` 指令（也可以用 `python -m llmop`）。`secure` 附加套件是 `cryptography`、`aiohttp`、`websockets`；
沒裝時 `llmop worker` 與 `llmop client` 會印出安裝提示，伺服器照常提供直連但不開加密面。

伺服器那台機器另外需要 Cloudflare 的 `cloudflared`（Windows：`winget install --id Cloudflare.cloudflared`）。
quick tunnel 不需要 Cloudflare 帳號。

---

## 2. 伺服器與兩個通道

```
llmop init
llmop serve --tunnel
```

`llmop init` 建立 `config.yaml` 並設定 admin 密碼（忘記時用 `llmop set-password admin`）。`llmop serve --tunnel` 啟動三個入口與兩個
quick tunnel（不加 `--tunnel` 時依 `tunnels.ui.enabled`／`tunnels.api.enabled`）。啟動後：

- 本機控制台：`http://127.0.0.1:8781/`（只有這台電腦，以 admin 登入）。
- 帳號網站：通道 `ui` 的 `https://<ui 主機>`。
- API 連結：通道 `api` 的 `https://<api 主機>/<秘密路徑>`。`llmop serve` 的啟動畫面只印遮罩版（`https://<api 主機>/k7Q…/v1`），
  完整連結用 `llmop tunnel show-api` 看，或在本機控制台看。

由 `llmop serve` 自己啟動的通道只會指向 `127.0.0.1:8780` 與 `127.0.0.1:8782`。

> **警告：隧道只能指向閘道的連接埠（帳號網站 8780、API 連結 8782），不可以指向其他任何東西。**
> 絕對不要把隧道指向本機控制台（8781）、Ollama、LM Studio、vLLM 或 worker 那台機器上的任何服務：那些服務沒有驗證、也沒有加密，
> 指過去就等於把模型與所有對話公開在網路上。worker 是主動連出的，不需要被連入。

閘道的紀錄只有 `request_id`、`api_key_id`、`worker_id`、模型、大小、狀態與耗時；不會有信封、封裝過的回覆、API 金鑰或任何權杖。
API 連結的存取紀錄不會印出秘密路徑。

### 2.1 網址會變：quick tunnel 與更換 API 連結

**每次啟動 `cloudflared` 都會拿到新的隨機網址**（quick tunnel）。重新啟動 `llmop serve`、機器重開或某個 `cloudflared` 當掉時，
那個通道的網址就會變。另外，管理員可以隨時故意更換 API 連結：

```
llmop tunnel rotate-api                           # 只換秘密路徑，舊連結立刻失效
llmop tunnel rotate-api --change-host             # 連主機名一起換（只重啟通道 api，不動帳號網站）
llmop tunnel rotate-api --grace 600               # 舊連結再通 10 分鐘
```

本機控制台的「通道與 API 連結」也有同樣的按鈕。API 連結換了以後要更新：

1. 每一台遠端 worker 的 `worker/config.json`：`gateway_url` 改成 `wss://<api 主機>/<新秘密路徑>/worker/ws`，然後重新啟動 worker。
   worker 斷線後會自己重連，但它重連的是設定檔裡的舊網址，所以一定要改設定、重新啟動。同一台機器的內建 worker 不受影響。
2. 每一個用戶端的 `client/config.json`：`gateway_url` 改成 `https://<api 主機>/<新秘密路徑>`，然後重新啟動 `llmop client proxy`。
   使用者登入帳號網站就看得到目前的 API 連結。

其他都不用動：API 金鑰、worker 的註冊、釘選的公鑰、用量都和網址無關；指向本機代理的程式（例如 SYNOX）也不用改，
它們連的是 `127.0.0.1:8788`。

要固定網址，請在 Cloudflare 帳號建立 named tunnel，在 `config.yaml` 的 `tunnels.api` 設 `mode: named`、`token`、`public_url`
（帳號網站同理用 `tunnels.ui`）。named tunnel 的 Cloudflare 端設定不在本文件範圍；它的 ingress 也只能指向上面兩個連接埠。

---

## 3. 建立 worker 並註冊

### 3.1 同一台機器（最簡單）

1. `config.yaml` 設 `roles: [gateway, worker]`。
2. 執行一次 `llmop worker init`，記下印出的 `key_id` 與 `public_key`（第 4 節）。
3. 檢查 `$LLMOP_HOME/worker/config.json` 的 `backends`（見 3.3 的表）；`gateway_url` 不用改，節點會改成 `ws://127.0.0.1:8781/worker/ws`。
4. 重新啟動 `llmop serve`。第一次啟動時節點會自己建立 worker、用一次性的註冊碼註冊；本機控制台的「Worker 與註冊碼」會顯示它上線。

### 3.2 另一台機器：在本機控制台建立 worker

在伺服器開 `http://127.0.0.1:8781/ui/admin`，在「Worker 與註冊碼」輸入名稱並建立。
畫面會顯示一個 `llmop_reg_…` 註冊碼：**只顯示這一次**、**只能用一次**、**1 小時後過期**。用安全的管道交給 worker 的管理者。
註冊碼遺失或過期，在該 worker 的列上重新發出即可（舊的隨之失效）。

### 3.3 在 worker 機器初始化

```
llmop worker init
```

它會建立 `keys.json`（第一把 X25519 金鑰）與設定範本 `config.json`，並在主控台印出：

```
目前的金鑰（請把 key_id 與 public_key 釘選到用戶端設定）：
  key_id     : <22 個字元>
  public_key : <43 個字元>
```

**這份主控台輸出就是釘選的依據**（第 4 節）。之後隨時可以用 `llmop worker show-key` 再印一次。

編輯 `worker/config.json`：

```json
{
  "gateway_url": "wss://<api 主機>/<秘密路徑>/worker/ws",
  "backends": [
    {"kind": "ollama", "base_url": "http://127.0.0.1:11434"},
    {"kind": "openai", "base_url": "http://127.0.0.1:1234"}
  ],
  "expose_models": [],
  "max_concurrency": 2,
  "request_timeout": 1800,
  "pinned_clients": []
}
```

| 欄位 | 說明 |
|---|---|
| `gateway_url` | 必須是 `wss://…/<秘密路徑>/worker/ws`；只有閘道在同一台機器時才可以用 `ws://127.0.0.1:8781/worker/ws` |
| `backends` | 至少一個。`kind` 是 `ollama`（Ollama 原生 API）或 `openai`（LM Studio、vLLM 等 OpenAI 相容伺服器，可加 `api_key`）。`base_url` 只填到埠號，而且**必須是 loopback**（`127.0.0.0/8`、`::1`、`localhost`），否則 worker 拒絕啟動 |
| `expose_models` | 只對外提供這些模型；空陣列表示後端有的全部提供。同名模型以前面的後端為準 |
| `max_concurrency` | 同時呼叫後端的上限 |
| `request_timeout` | 呼叫後端的逾時秒數 |
| `pinned_clients` | 選用的強化：列出用戶端的 Ed25519 簽章公鑰（`llmop client keygen` 印出的 `public_key`）。設定後，不論閘道轉來什麼，worker 只接受這些用戶端的請求（見 SECURITY.md 檢討第 3 項） |

本機 LLM 伺服器本身也只能聽 loopback：Ollama 預設就是 `127.0.0.1:11434`（不要把 `OLLAMA_HOST` 設成 `0.0.0.0`）；
vLLM 請加 `--host 127.0.0.1`；LM Studio 不要開「Serve on Local Network」。worker 一律以非串流方式呼叫後端，也從不執行工具呼叫，
只把 `tool_calls` 原樣交還給用戶端。

### 3.4 第一次啟動（註冊）

```
llmop worker run --reg-token llmop_reg_…
```

worker 會先印出目前的金鑰，然後連到閘道、用註冊碼換到 `worker_id` 與 worker 權杖，存進 `state.json`（0600），
再公告自己的公鑰與模型。本機控制台上這台 worker 會變成上線，並列出它的 key_id 與模型。

之後每次都只要：

```
llmop worker run
```

worker 用 `state.json` 裡的權杖重新連線。斷線時會以指數退避（最長 60 秒）自動重連；後端的模型清單每 60 秒更新一次。
閘道拒絕它的憑證時（例如 worker 在本機控制台被撤銷），worker 會印出原因並停止，不會一直重試。
如果 `state.json` 遺失，請管理員重新發出註冊碼，刪掉 `state.json` 後以新的 `--reg-token` 再註冊一次。

撤銷 worker：本機控制台該列的「撤銷」。閘道立刻切斷它的連線，之後再也連不進來。

---

## 4. 比對指紋並在用戶端釘選

用戶端只會把請求加密給**設定檔裡釘選的** worker 公鑰。閘道在 `GET /e2e/v1/models` 也會列出每台 worker 的公鑰，
但那只拿來比對，用戶端從不直接採用：如果閘道被入侵，它可以換上自己的公鑰，信任它就等於把內容交給它。沒有「第一次使用就信任」的模式。

1. 在 worker 機器執行 `llmop worker show-key`（或看 `init`、`run` 啟動時印出的內容），記下 `key_id` 與 `public_key`。
2. 在本機控制台的「Worker 與註冊碼」找到這台 worker，把顯示的 key_id 和 worker 主控台的 `key_id` **逐字比對**。
   不一致就表示閘道上登記的不是你那台 worker 的金鑰，停下來查清楚。
3. 把 **worker 主控台**印出的 `public_key` 放進用戶端 `config.json` 的 `pinned_workers`。不要從控制台或帳號網站複製：
   從閘道複製來的值，等於信任了閘道。最好由 worker 的管理者透過閘道以外的管道（當面、簽章過的訊息）交給使用者。

用戶端每次送出前都會重新計算 `key_id`，和閘道給的不一致就略過那一筆。`pinned_workers` 可以列好幾把；
同一個模型由多把已釘選的金鑰提供時，用排在前面的那一把。

---

## 5. 加密 key 與用戶端簽章金鑰

每一筆加密請求都要同時有 API 金鑰（`Authorization: Bearer`）與用戶端簽章。閘道與 worker 都會驗簽，
所以只偷到 API 金鑰（Cloudflare 看得到它）的人送不出請求。

1. **使用者**執行：

   ```
   llmop client keygen
   ```

   它建立 `client/signing_key.json`（0600；已存在就沿用、絕不覆寫），並印出 `public_key` 與 `sig_key_id`。

2. **使用者**登入帳號網站，在「我的 API 金鑰」建立 key：「加密」選「必須加密」（`required`：這把 key 走明文 `/v1` 一律被拒）
   或「可選」，貼上 `public_key`。畫面顯示的 `sk-llop-…` **只會出現這一次**；伺服器只存它的 SHA-256。列表上的 sig_key_id
   應該和 `keygen` 印出的一致。GUEST key 由管理員在本機控制台的「GUEST 金鑰」發，加密的 GUEST key 一樣要貼簽章公鑰。

3. **使用者**建立 `client/config.json`，權限設成 0600（`chmod 600`；權限太寬時用戶端會警告）：

   ```json
   {
     "gateway_url": "https://<api 主機>/<秘密路徑>",
     "api_key": "sk-llop-…",
     "pinned_workers": ["<worker 主控台印出的 public_key>"],
     "proxy_port": 8788,
     "default_max_tokens": 1024
   }
   ```

   `gateway_url` 必須是 `https://`，帶秘密路徑、不帶 `/e2e/v1`；只有閘道在同一台機器時才可以用 `http://127.0.0.1:…`。

4. 試用：

   ```
   llmop client models
   llmop client chat --model qwen3:8b "你好"
   llmop client proxy
   ```

   `llmop client proxy` 在 `127.0.0.1:8788` 提供 OpenAI 相容的 `/v1/models` 與 `/v1/chat/completions`；
   一般程式怎麼指向它見 README 的「讓 SYNOX 使用」。

用量：加密請求和明文請求共用同一套等級、兩個用量表與排隊（專案 README 的「等級與用量」）。worker 回報的 token 數在請求結束後記帳。
用量表見底時回 `quota_exceeded`（HTTP 429，附 `Retry-After`）；排隊太久或這個帳號排隊太多時回 `queue_wait`（429）。

撤銷 API 金鑰：帳號網站（自己的 key）或本機控制台（任何 key），之後的請求一律 `unauthorized`。要換用戶端的簽章金鑰：把舊的
`signing_key.json` 移走，再執行一次 `llmop client keygen`，然後在帳號網站把那把 key 的簽章公鑰換成新的。如果 worker 設了
`pinned_clients`，也要更新。

---

## 6. worker 金鑰輪替

請求是用 worker 的長期公鑰加密的，沒有前向保密：錄下密文的人日後拿到私鑰，就能解開當時的請求（SECURITY.md 檢討第 1 項）。
緩解方式是定期輪替並**刪除**舊私鑰：刪掉之後，用舊金鑰加密的密文就再也解不開。v0.2 沿用手動流程（無縫換鑰排在 v0.3）。

1. 在 worker 機器產生新金鑰：

   ```
   llmop worker rotate-key
   ```

   主控台印出新的 `key_id` 與 `public_key`。舊金鑰仍會被接受。

2. **重新啟動 worker**（`llmop worker run`；內建 worker 則重新啟動 `llmop serve`）。worker 只在啟動時讀 `keys.json`；
   重新啟動後它公告兩把金鑰，新的那把是目前的金鑰。

3. 依第 4 節把新的 `public_key` 交給使用者，請他們加進 `pinned_workers`，並放在舊的前面。這段期間，
   只釘選舊金鑰的用戶端照常可用。

4. 所有用戶端都換好之後，刪除舊私鑰並再重新啟動 worker：

   ```
   llmop worker prune-keys
   llmop worker run
   ```

   `prune-keys` 會列出要刪的 key_id 並要求輸入 `yes` 確認（`--yes` 可略過確認）。重新啟動後 worker 只公告新金鑰。
   仍然只釘選舊金鑰的用戶端會在本機直接報錯（沒有任何已釘選的 worker 提供這個模型），不會改用未釘選的金鑰；
   就算有人把請求加密給舊金鑰送來，閘道與 worker 也都會回 `unknown_key_id`。

5. 使用者把舊的 `public_key` 從 `pinned_workers` 移除。

---

## 7. 時鐘：誤差必須在五分鐘以內

每一筆請求都帶有用戶端填入的時間戳。閘道與 worker 都會拒絕和自己的時鐘相差超過 300 秒的請求（`stale_timestamp`），
這是重放防禦的一部分。所以**用戶端、閘道與 worker 三方的時鐘都要對時**（Windows 的「自動設定時間」、`systemd-timesyncd`、`chrony`）。

另外，worker 會拒絕時間戳早於它這次啟動時刻的請求（重放防禦的開機門檻）。代價是：worker 剛重新啟動後，
時鐘比 worker 慢的用戶端會有一小段時間收到 `stale_timestamp`，慢多少秒就持續多久。對好時就不會遇到。

---

## 8. 本機控制台的存取範圍

v0.1 的 `admin_local_only` 設定已經不需要：管理（建立 worker、發 GUEST key、更換 API 連結、改設定）只在本機控制台
`127.0.0.1:8781`，而這個連接埠**沒有任何通道指向它**。控制台另外檢查每一個請求：對端必須是 loopback、`Host` 必須是本機名稱，
而且不能帶任何轉送標頭（`Cf-Connecting-Ip`、`X-Forwarded-For` 等）；寫入動作還要求在本機登入的工作階段與 CSRF 標頭。
帳號網站與 API 連結上沒有任何管理用的寫入路徑。

要從別台電腦管理，用 SSH 轉送：

```
ssh -L 8781:127.0.0.1:8781 <伺服器>
```

然後在自己電腦開 `http://127.0.0.1:8781/`。對伺服器來說這仍然是本機連線。

> **注意：** 不要用 `ssh -R`、ngrok、自己架的 nginx 之類把 8781 帶到網路上。那些轉送程式在伺服器看來就是 loopback 連線，
> 如果它們又不加轉送標頭，本機判斷擋不住，控制台等於公開。只用 `llmop serve` 自己啟動的兩個通道對外。

---

## 9. quick tunnel 的限制

- **不支援 SSE（Server-Sent Events）。** 加密面不受影響：閘道從不串流，回覆一次送完。用戶端程式要求 `stream: true` 時，
  SSE 只發生在用戶端程式與本機代理之間（`127.0.0.1:8788`），不經過隧道。（明文 `/v1` 在 API 連結上預設也不串流，見 `server.public_stream`。）
- **同時進行中的請求約 200 筆。** 每一筆等待中的請求或輪詢都佔一筆。v0.2 的排隊上限（`queue.max_length` 預設 30）遠低於這個數字。
- **約 100 秒沒有回應就會被切斷（Cloudflare 錯誤 524）。** 所以閘道同步最多只等 80 秒；
  還沒好就回 `202`，用戶端用同一個 `request_id` 長輪詢 `GET /e2e/v1/requests/{request_id}`，直到完成或整體 1800 秒逾時。
  長時間的推論就是這樣完成的，用戶端 SDK 與代理會自動處理。完成的結果在閘道保留 600 秒。
- **每次啟動網址都不同**（第 2.1 節），也沒有可用性保證。長期使用請改用 named tunnel。

另外，單一請求的信封上限約 16 MiB。明文會先補齊到 1 KiB 的倍數、加密後再做 base64，所以實際可送的對話內容
（含 base64 圖片）大約是 12 MiB；回覆也有相同等級的上限。

---

## 10. 重新啟動與常見錯誤

- **重新啟動伺服器**：資料庫裡的一切都保留；記憶體裡進行中的請求與尚未取回的結果會遺失，正在輪詢的用戶端會收到
  `unknown_request`，需要重送。worker 會自動重連。quick tunnel 的網址會變（第 2.1 節）。
- **重新啟動 worker**：掛在它身上的請求以 `worker_disconnected` 失敗；worker 重連後即可接新請求。
- **worker 在請求途中斷線**：該請求回 `worker_disconnected`（HTTP 502）。

用戶端看到的錯誤碼（完整列表見 PROTOCOL.md 第 3.5 節）：

| 錯誤碼 | 常見原因 |
|---|---|
| `unauthorized` | API 金鑰錯誤、已撤銷、已到期，或 GUEST key 已用完 |
| `bad_signature` | 這把 API 金鑰沒有登記簽章公鑰，或登記的不是這個用戶端的公鑰 |
| `untrusted_client` | worker 設了 `pinned_clients`，而這個用戶端的公鑰不在裡面 |
| `stale_timestamp` | 時鐘誤差超過五分鐘，或 worker 剛重啟而用戶端時鐘偏慢（第 7 節） |
| `quota_exceeded` | 用量表見底（1.5 小時桶或每週表），看 `Retry-After` |
| `queue_wait` | 排隊太久，或這個帳號排隊中的請求太多，看 `Retry-After` |
| `gateway_busy` | 整個佇列已滿，稍後再試 |
| `model_unavailable` | 沒有任何**已釘選**的 worker 在線上提供這個模型（檢查 worker 是否上線、`pinned_workers` 是否正確） |
| `unknown_key_id` | 送出的請求加密給了不在線上或已刪除的 worker 金鑰 |
| `worker_disconnected` | worker 在請求途中斷線 |
| `timeout` | worker 在 1800 秒內沒有回覆 |
| `backend_error`／`backend_timeout` | 本機 LLM 伺服器出錯或逾時（這類錯誤是加密送回的，閘道看不到細節） |

HTTP 404（沒有 JSON 本文）：秘密路徑不對——API 連結已經換過了，到帳號網站看目前的連結。
