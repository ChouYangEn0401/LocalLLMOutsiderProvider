# 部署

本文件一步步說明怎麼把三個角色跑起來，以及每一步要確認什麼。系統的樣子與安全邊界見 [README](../README.md)，
線路格式見 [PROTOCOL.md](PROTOCOL.md)，密碼設計見 [SECURITY.md](SECURITY.md)。

典型的配置是三台機器，但也可以全部放在同一台：

| 角色 | 放在哪裡 | 對外連線 | 監聽 |
|---|---|---|---|
| 閘道 | 任何能執行 `cloudflared` 的機器 | 由 `cloudflared` 連出到 Cloudflare | `127.0.0.1:8787`（只有 loopback） |
| worker | 本機 LLM 伺服器旁邊 | WebSocket 連出到閘道 | 無 |
| 用戶端 | 使用者的電腦 | HTTPS 連出到閘道 | 代理 `127.0.0.1:8788`（只有 loopback） |

所有狀態檔都在 `$LLMOP_HOME`（預設 `~/.llmop`）底下、依角色分目錄，目錄權限 0700，含秘密的檔案權限 0600：

| 檔案 | 內容 |
|---|---|
| `gateway/config.json` | 閘道設定（可省略，全部用預設值） |
| `gateway/gateway.db` | SQLite：管理員密碼雜湊、API 金鑰雜湊與上限、每日用量、worker 與其公鑰、模型清單 |
| `worker/config.json` | worker 設定（閘道網址、後端、可選的 `pinned_clients`） |
| `worker/keys.json` | worker 的 X25519 私鑰，**沒有加密**（worker 必須能無人值守地啟動） |
| `worker/state.json` | 註冊後拿到的 `worker_id` 與 worker 權杖 |
| `client/config.json` | 用戶端設定（閘道網址、API 金鑰、釘選的 worker 公鑰） |
| `client/signing_key.json` | 用戶端的 Ed25519 簽章私鑰 |

---

## 1. 安裝

每台機器都需要 Python 3.11 以上。三個角色是同一個套件，在專案目錄執行：

```
pip install .
```

之後就有 `llmop` 指令（也可以用 `python -m llmop`）。執行期相依套件只有 `aiohttp` 與 `cryptography`。

閘道那台機器另外需要 Cloudflare 的 `cloudflared`，依 Cloudflare 的說明安裝即可。quick tunnel 不需要 Cloudflare 帳號。

---

## 2. 閘道

### 2.1 初始化

```
llmop gateway init
```

輸入兩次管理員密碼（至少 8 個字元）。密碼以 scrypt 雜湊存進 `gateway.db`。忘記密碼時再執行一次就會重設，其他資料不受影響。

### 2.2 設定（可省略）

需要時建立 `~/.llmop/gateway/config.json`，每個欄位都可以省略：

| 欄位 | 預設值 | 說明 |
|---|---|---|
| `host` | `"127.0.0.1"` | 只接受 loopback 位址；設成其他位址閘道會拒絕啟動 |
| `port` | `8787` | |
| `admin_local_only` | `true` | 見第 9 節 |
| `sync_wait_seconds` | `80` | 一筆請求同步等待多久，之後改回 202 讓用戶端輪詢；必須低於 Cloudflare 約 100 秒的逾時 |
| `request_timeout_seconds` | `1800` | 轉給 worker 之後最多等多久，逾時回 `timeout` |
| `max_inflight` | `150` | 整個閘道同時進行中的請求上限，超過回 `gateway_busy` |

### 2.3 啟動

```
llmop gateway run
```

它會印出閘道位址、管理介面位址（`http://127.0.0.1:8787/admin/`）與要執行的 `cloudflared` 指令。
`GET /healthz` 回 `{"ok": true}`，可以拿來確認服務活著。

閘道的紀錄只有 `request_id`、`api_key_id`、`worker_id`、模型、大小、狀態與耗時；不會有信封、封裝過的回覆、API 金鑰或任何權杖。

---

## 3. 隧道

在閘道那台機器另開一個終端機：

```
cloudflared tunnel --url http://127.0.0.1:8787
```

它會印出一個 `https://<隨機名稱>.trycloudflare.com` 網址，這就是對外的閘道網址：

- 用戶端的 `gateway_url` 是 `https://<名稱>.trycloudflare.com`。
- 遠端 worker 的 `gateway_url` 是 `wss://<名稱>.trycloudflare.com/worker/ws`。

> **警告：隧道只能指向閘道的連接埠（`http://127.0.0.1:8787`），不可以指向其他任何東西。**
> 絕對不要把隧道指向 Ollama、LM Studio、vLLM 或 worker 那台機器上的任何服務：那些服務沒有驗證、也沒有加密，
> 指過去就等於把模型與所有對話公開在網路上。整個系統只需要閘道這一個入口；worker 是主動連出的，不需要被連入。

### 3.1 quick tunnel 每次啟動都會換網址

`cloudflared tunnel --url …` 開的是 quick tunnel：**每次啟動 `cloudflared` 都會拿到新的隨機網址**。
只重新啟動閘道不會換網址；重新啟動 `cloudflared`（或機器重開）就會。網址換了以後要更新：

1. 每一台遠端 worker 的 `~/.llmop/worker/config.json`：`gateway_url` 改成 `wss://<新名稱>.trycloudflare.com/worker/ws`，
   然後重新啟動 worker。worker 斷線後會自己重連，但它重連的是設定檔裡的舊網址，所以一定要改設定、重新啟動。
2. 每一個用戶端的 `~/.llmop/client/config.json`：`gateway_url` 改成 `https://<新名稱>.trycloudflare.com`，
   然後重新啟動 `llmop client proxy`。

其他都不用動：API 金鑰、worker 的註冊、釘選的公鑰、配額與用量都存在 `gateway.db` 與各自的金鑰檔裡，跟網址無關；
指向本機代理的程式（例如 SYNOX）也不用改，它們連的是 `127.0.0.1:8788`。

要固定網址，請改用 Cloudflare 帳號建立 named tunnel（`cloudflared tunnel create`、`cloudflared tunnel route dns`），
ingress 一樣只設一條 `service: http://127.0.0.1:8787`。named tunnel 的其他設定不在本文件範圍。

---

## 4. 建立 worker 並註冊

### 4.1 在閘道建立 worker

在閘道那台機器開 `http://127.0.0.1:8787/admin/`，登入後在「Worker」區塊輸入名稱，按「建立並取得註冊權杖」。
畫面會顯示一個 `llmop_reg_…` 註冊權杖：**只顯示這一次**、**只能用一次**、**1 小時後過期**。用安全的管道交給 worker 的管理者。
權杖遺失或過期，在該 worker 的列上「重新發出註冊權杖」即可（舊權杖隨之失效）。

### 4.2 在 worker 機器初始化

```
llmop worker init
```

它會建立 `keys.json`（第一把 X25519 金鑰）與設定範本 `config.json`，並在主控台印出：

```
目前的金鑰（請把 key_id 與 public_key 釘選到用戶端設定）：
  key_id     : <22 個字元>
  public_key : <43 個字元>
```

**這份主控台輸出就是釘選的依據**（第 5 節）。之後隨時可以用 `llmop worker show-key` 再印一次。

編輯 `~/.llmop/worker/config.json`：

```json
{
  "gateway_url": "wss://<名稱>.trycloudflare.com/worker/ws",
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
| `gateway_url` | 必須是 `wss://…/worker/ws`；只有閘道在同一台機器時才可以用 `ws://127.0.0.1:8787/worker/ws`（範本的預設值） |
| `backends` | 至少一個。`kind` 是 `ollama`（Ollama 原生 API）或 `openai`（LM Studio、vLLM 等 OpenAI 相容伺服器，可加 `api_key`）。`base_url` 只填到埠號，而且**必須是 loopback**（`127.0.0.0/8`、`::1`、`localhost`），否則 worker 拒絕啟動 |
| `expose_models` | 只對外提供這些模型；空陣列表示後端有的全部提供。同名模型以前面的後端為準 |
| `max_concurrency` | 同時呼叫後端的上限；閘道最多替這台 worker 排隊 8 倍的請求，再多回 `worker_busy` |
| `request_timeout` | 呼叫後端的逾時秒數 |
| `pinned_clients` | 選用的強化：列出用戶端的 Ed25519 簽章公鑰（`llmop client keygen` 印出的 `public_key`）。設定後，不論閘道轉來什麼，worker 只接受這些用戶端的請求（見 SECURITY.md 檢討第 3 項） |

本機 LLM 伺服器本身也只能聽 loopback：Ollama 預設就是 `127.0.0.1:11434`（不要把 `OLLAMA_HOST` 設成 `0.0.0.0`）；
vLLM 請加 `--host 127.0.0.1`；LM Studio 不要開「Serve on Local Network」。worker 一律以非串流方式呼叫後端，也從不執行工具呼叫，
只把 `tool_calls` 原樣交還給用戶端。

### 4.3 第一次啟動（註冊）

```
llmop worker run --reg-token llmop_reg_…
```

worker 會先印出目前的金鑰，然後連到閘道、用註冊權杖換到 `worker_id` 與 worker 權杖，存進 `state.json`（0600），
再公告自己的公鑰與模型。管理介面上這台 worker 會變成上線，並列出它的 key_id 與模型。

之後每次都只要：

```
llmop worker run
```

worker 用 `state.json` 裡的權杖重新連線。斷線時會以指數退避（最長 60 秒）自動重連；後端的模型清單每 60 秒更新一次。
閘道拒絕它的憑證時（例如 worker 在管理介面被撤銷），worker 會印出原因並停止，不會一直重試。
如果 `state.json` 遺失，請管理員重新發出註冊權杖，刪掉 `state.json` 後以新的 `--reg-token` 再註冊一次（仍是同一個 `worker_id`）。

撤銷 worker：管理介面該列的「撤銷」。閘道立刻切斷它的連線，之後再也連不進來。

---

## 5. 比對指紋並在用戶端釘選

用戶端只會把請求加密給**設定檔裡釘選的** worker 公鑰。閘道在 `GET /v1/models` 也會列出每台 worker 的公鑰，
但那只拿來比對，用戶端從不直接採用：如果閘道被入侵，它可以換上自己的公鑰，信任它就等於把內容交給它。沒有「第一次使用就信任」的模式。

1. 在 worker 機器執行 `llmop worker show-key`（或看 `init`、`run` 啟動時印出的內容），記下 `key_id` 與 `public_key`。
2. 在閘道管理介面的「Worker」區塊找到這台 worker，把顯示的 key_id 和 worker 主控台的 `key_id` **逐字比對**。
   不一致就表示閘道上登記的不是你那台 worker 的金鑰，停下來查清楚。
3. 把 **worker 主控台**印出的 `public_key` 放進用戶端 `config.json` 的 `pinned_workers`。不要從管理介面複製：
   從閘道複製來的值，等於信任了閘道。最好由 worker 的管理者透過閘道以外的管道（當面、簽章過的訊息）交給用戶端。

用戶端每次送出前都會重新計算 `key_id`，和閘道給的不一致就略過那一筆。`pinned_workers` 可以列好幾把；
同一個模型由多把已釘選的金鑰提供時，用排在前面的那一把。

---

## 6. API 金鑰與用戶端簽章金鑰

每一筆請求都要同時有 API 金鑰（`Authorization: Bearer`）與用戶端簽章。閘道與 worker 都會驗簽，
所以只偷到 API 金鑰（Cloudflare 看得到它）的人送不出請求。

1. **用戶端**執行：

   ```
   llmop client keygen
   ```

   它建立 `~/.llmop/client/signing_key.json`（0600；已存在就沿用、絕不覆寫），並印出 `public_key` 與 `sig_key_id`。
   把 `public_key` 交給閘道管理員（公鑰不是秘密，但要確保交到的是你的公鑰）。

2. **管理員**在管理介面「建立 API 金鑰」：填名稱、貼上簽章公鑰，視需要填每日請求上限與每日 token 上限（空白＝不限）。
   畫面顯示的 `llmop_sk_…` **只會出現這一次**；閘道只存它的 SHA-256。列表上的 sig_key_id 應該和用戶端印出的一致。
   用安全的管道把 API 金鑰交給用戶端。

3. **用戶端**建立 `~/.llmop/client/config.json`，權限設成 0600（`chmod 600`；權限太寬時用戶端會警告）：

   ```json
   {
     "gateway_url": "https://<名稱>.trycloudflare.com",
     "api_key": "llmop_sk_…",
     "pinned_workers": ["<worker 主控台印出的 public_key>"],
     "proxy_port": 8788,
     "default_max_tokens": 1024
   }
   ```

   `gateway_url` 必須是 `https://`；只有閘道在同一台機器時才可以用 `http://127.0.0.1:8787`。

4. 試用：

   ```
   llmop client models
   llmop client chat --model qwen3:8b "你好"
   llmop client proxy
   ```

   `llmop client proxy` 在 `127.0.0.1:8788` 提供 OpenAI 相容的 `/v1/models` 與 `/v1/chat/completions`；
   一般程式怎麼指向它見 README 的「讓 SYNOX 使用」。

配額以 UTC 日計算。閘道收下一筆請求時就把當天的請求數加一；token 上限的檢查是「今天已用的 token 數加上這筆的 `max_tokens`
不可超過上限」，已用的 token 數是 worker 回報的輸入加輸出 token。超過時回 `quota_exceeded`（HTTP 429）。

撤銷 API 金鑰：管理介面該列的「撤銷」，之後的請求一律 `unauthorized`。要換用戶端的簽章金鑰：把舊的 `signing_key.json`
移走，再執行一次 `llmop client keygen`，然後請管理員在「編輯 API 金鑰」貼上新的公鑰。如果 worker 設了 `pinned_clients`，也要更新。

---

## 7. worker 金鑰輪替

請求是用 worker 的長期公鑰加密的，沒有前向保密：錄下密文的人日後拿到私鑰，就能解開當時的請求（SECURITY.md 檢討第 1 項）。
緩解方式是定期輪替並**刪除**舊私鑰：刪掉之後，用舊金鑰加密的密文就再也解不開。

1. 在 worker 機器產生新金鑰：

   ```
   llmop worker rotate-key
   ```

   主控台印出新的 `key_id` 與 `public_key`。舊金鑰仍會被接受。

2. **重新啟動 worker**（`llmop worker run`）。worker 只在啟動時讀 `keys.json`；重新啟動後它公告兩把金鑰，新的那把是目前的金鑰。

3. 依第 5 節把新的 `public_key` 交給用戶端，請他們加進 `pinned_workers`，並放在舊的前面。這段期間，
   只釘選舊金鑰的用戶端照常可用。

4. 所有用戶端都換好之後，刪除舊私鑰並再重新啟動 worker：

   ```
   llmop worker prune-keys
   llmop worker run
   ```

   `prune-keys` 會列出要刪的 key_id 並要求輸入 `yes` 確認（`--yes` 可略過確認）。重新啟動後 worker 只公告新金鑰。
   仍然只釘選舊金鑰的用戶端會在本機直接報錯（沒有任何已釘選的 worker 提供這個模型），不會改用未釘選的金鑰；
   就算有人把請求加密給舊金鑰送來，閘道與 worker 也都會回 `unknown_key_id`。

5. 用戶端把舊的 `public_key` 從 `pinned_workers` 移除。

閘道的資料庫會保留看過的每一把公鑰與首次出現時間，方便在管理介面比對歷史；這些都是公鑰，不影響安全。

---

## 8. 時鐘：誤差必須在五分鐘以內

每一筆請求都帶有用戶端填入的時間戳。閘道與 worker 都會拒絕和自己的時鐘相差超過 300 秒的請求（`stale_timestamp`），
這是重放防禦的一部分。所以**用戶端、閘道與 worker 三方的時鐘都要對時**（啟用 NTP，例如 `systemd-timesyncd`、`chrony`
或作業系統內建的網路校時）。

另外，worker 會拒絕時間戳早於它這次啟動時刻的請求（重放防禦的開機門檻）。代價是：worker 剛重新啟動後，
時鐘比 worker 慢的用戶端會有一小段時間收到 `stale_timestamp`，慢多少秒就持續多久。對好時就不會遇到。

---

## 9. 管理介面的存取範圍（`admin_local_only`）

管理介面（`/admin/`）與管理 API（`/admin/api/*`）和用戶端 API 共用同一個連接埠，所以隧道也會把它們帶到網路上。
預設 `admin_local_only: true`：只要請求帶有 `Cf-Connecting-Ip` 標頭（經過 Cloudflare 的請求一定有），或對端不是 loopback 位址，
所有 `/admin` 路徑一律回 404。也就是說，只有在閘道那台機器上直接連 `127.0.0.1:8787` 才能管理。

要從別台電腦管理，建議保持預設、用 SSH 轉送：

```
ssh -L 8787:127.0.0.1:8787 <閘道那台機器>
```

然後在自己電腦開 `http://127.0.0.1:8787/admin/`。對閘道來說這仍然是本機連線。

把 `admin_local_only` 設成 `false` 會暴露：

- 任何知道隧道網址的人都看得到管理介面的登入頁，並且可以對管理 API 嘗試登入。保護只剩管理員密碼本身
  （scrypt 雜湊；同一個來源位址 15 分鐘內失敗 5 次就暫時鎖住登入）。
- 拿到密碼的人可以做管理員能做的一切：發 API 金鑰、改配額、建立與撤銷 worker、發註冊權杖（等於能讓自己的 worker 註冊進來）、
  看所有用量。他仍然讀不到任何對話內容，但可以讓服務停擺，或用新發的 API 金鑰消耗 worker 的運算資源。
- 經過 Cloudflare 時工作階段 cookie 會帶 `Secure`，所有改變狀態的呼叫都要 CSRF 標頭；這些保護對本機連線同樣有效。

一定要設成 `false` 時，請用很長的隨機密碼，並在不需要時改回 `true`（改設定後重新啟動閘道）。

> **注意：** 本機判斷依賴 Cloudflare 加上的 `Cf-Connecting-Ip` 標頭。如果改用其他方式把閘道帶到網路上，
> 而轉送程式就在閘道那台機器上、又不加這個標頭（例如 `ssh -R` 反向轉送、ngrok、自己架的 nginx），
> 那些外部請求在閘道看來就是 loopback 的本機連線，`admin_local_only` 擋不住，管理介面等於公開。
> 只用 `cloudflared` 對外，或確保那種轉送程式一定會加上 `Cf-Connecting-Ip`。

---

## 10. quick tunnel 的限制

- **不支援 SSE（Server-Sent Events）。** 本系統不受影響：閘道從不串流，回覆一次送完。用戶端程式要求 `stream: true` 時，
  SSE 只發生在用戶端程式與本機代理之間（`127.0.0.1:8788`），不經過隧道。
- **同時進行中的請求約 200 筆。** 每一筆等待中的請求或輪詢都佔一筆。閘道的 `max_inflight` 預設 150，留一些餘裕給輪詢與
  `/v1/models`；不要調到超過隧道的上限。
- **約 100 秒沒有回應就會被切斷（Cloudflare 錯誤 524）。** 所以閘道同步最多只等 `sync_wait_seconds`（預設 80 秒）；
  還沒好就回 `202`，用戶端用同一個 `request_id` 長輪詢 `GET /v1/requests/{request_id}`，直到完成或整體 1800 秒逾時。
  長時間的推論就是這樣完成的，用戶端 SDK 與代理會自動處理。完成的結果在閘道保留 600 秒。
- **每次啟動網址都不同**（第 3.1 節），也沒有可用性保證。長期使用請改用 named tunnel。

另外，單一請求的信封上限約 16 MiB。明文會先補齊到 1 KiB 的倍數、加密後再做 base64，所以實際可送的對話內容
（含 base64 圖片）大約是 12 MiB；回覆也有相同等級的上限。

---

## 11. 重新啟動與常見錯誤

- **重新啟動閘道**：資料庫裡的一切都保留；記憶體裡進行中的請求與尚未取回的結果會遺失，正在輪詢的用戶端會收到
  `unknown_request`，需要重送。worker 會自動重連。
- **重新啟動 worker**：掛在它身上的請求以 `worker_disconnected` 失敗；worker 重連後即可接新請求。
- **worker 在請求途中斷線**：該請求回 `worker_disconnected`（HTTP 502）。

用戶端看到的錯誤碼（完整列表見 PROTOCOL.md 第 3.5 節）：

| 錯誤碼 | 常見原因 |
|---|---|
| `unauthorized` | API 金鑰錯誤或已撤銷 |
| `bad_signature` | 這把 API 金鑰沒有登記簽章公鑰，或登記的不是這個用戶端的公鑰 |
| `untrusted_client` | worker 設了 `pinned_clients`，而這個用戶端的公鑰不在裡面 |
| `stale_timestamp` | 時鐘誤差超過五分鐘，或 worker 剛重啟而用戶端時鐘偏慢（第 8 節） |
| `quota_exceeded` | 今天的請求數或 token 數已達上限 |
| `model_unavailable` | 沒有任何**已釘選**的 worker 在線上提供這個模型（檢查 worker 是否上線、`pinned_workers` 是否正確） |
| `unknown_key_id` | 送出的請求加密給了不在線上或已刪除的 worker 金鑰 |
| `worker_busy`／`gateway_busy` | 佇列已滿，稍後再試 |
| `worker_disconnected` | worker 在請求途中斷線 |
| `timeout` | worker 在 `request_timeout_seconds` 內沒有回覆 |
| `backend_error`／`backend_timeout` | 本機 LLM 伺服器出錯或逾時（這類錯誤是加密送回的，閘道看不到細節） |
