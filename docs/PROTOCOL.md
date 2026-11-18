# 線路協定（第 1 版）

本文件是用戶端、閘道（gateway）與 worker 之間的契約。程式中對應的型別、常數與檢查都在
`src/llmop/protocol.py`；密碼運算在 `src/llmop/crypto.py`，設計理由見 [SECURITY.md](SECURITY.md)。

角色與資料流：

```
用戶端 ──HTTP──▶ 閘道 ──WebSocket（worker 主動連入）──▶ worker ──▶ 本機 LLM 後端
```

閘道只看得到明文的中繼資料標頭（protected）與兩段不透明的 b64u 字串；對話內容只有 worker 解得開。

---

## 1. 通用規則（Common rules）

- **文字編碼**：所有 JSON 一律 UTF-8。
- **二進位編碼**：文中的 `b64u` 指 base64url（RFC 4648 第 5 節的 URL 安全字母表），**不加 `=` 補位**。
  解碼端必須拒絕：任何 `=`、`A-Za-z0-9_-` 以外的字元（包括 `+`、`/`、空白、換行），以及長度除以 4 餘 1 的字串。
  Python 的 `urlsafe_b64decode` 太寬鬆，所以 `protocol.b64u_decode` 會先明確檢查，失敗丟 `bad_envelope`。
  （實作另外拒絕最後一個字元帶多餘位元的非正規寫法，讓每段資料只有一種編碼。）
- **時間**：`ts` 是整數，UTC 的 Unix 秒數。
- **嚴格解析**：物件只要缺欄位、欄位型別不對、或出現不認得的欄位，就整個拒絕。
  這條適用於 protected 標頭、信封，以及每一種 WebSocket 訊框。
  （整數欄位不接受布林值或浮點數；實作也拒絕重複欄位與 `NaN`/`Infinity`。）
- **版本**：`v` 是整數 `1`。其他值一律回 `unsupported_version`；`v` 的型別不對則是 `bad_envelope`。

### 1.1 識別碼與秘密值

| 名稱 | 形式 | 說明 |
|---|---|---|
| worker 公鑰 | 原始 32 位元組 X25519 公鑰的 `b64u`（43 字元） | |
| `key_id` | `b64u(SHA-256("LLMOP/v1/key-id/x25519" ‖ 0x00 ‖ 原始公鑰)[:16])`，即雜湊取前 16 位元組（22 字元） | 同時是人工比對用的指紋 |
| 用戶端簽章公鑰 | 原始 32 位元組 Ed25519 公鑰的 `b64u` | |
| `sig_key_id` | `b64u(SHA-256("LLMOP/v1/key-id/ed25519" ‖ 0x00 ‖ 原始公鑰)[:16])`，即雜湊取前 16 位元組（22 字元） | |
| `request_id` | 16 個隨機位元組的 `b64u`（22 字元） | 由用戶端產生 |
| API 金鑰 | `llmop_sk_` + `b64u`(32 個隨機位元組) | 閘道只存 SHA-256 的十六進位摘要 |
| 註冊權杖 | `llmop_reg_` + `b64u`(24 個隨機位元組) | 只能用一次，1 小時後過期，雜湊後儲存 |
| worker 權杖 | `llmop_wk_` + `b64u`(32 個隨機位元組) | 閘道存雜湊；worker 存在 `state.json` |
| `worker_id` | `w_` + 12 個十六進位字元 | |
| `api_key_id` | `k_` + 12 個十六進位字元 | API 金鑰的公開代號，可以寫進紀錄 |

`‖` 表示位元組串接，`0x00` 是一個零位元組。

### 1.2 上限與預設值

以下都是 `llmop.protocol` 的模組常數，名稱相同：

| 常數 | 值 |
|---|---|
| `MAX_BODY_BYTES`（HTTP 本文與單一 WebSocket 訊息） | 16 MiB（16 × 1024 × 1024 位元組） |
| `MAX_MODEL_LEN` | 200 個字元 |
| `MAX_TOKENS_RANGE` | 1 到 1,000,000（含兩端；程式中是 tuple `(1, 1_000_000)`） |
| `CLOCK_SKEW_SECONDS` | 300 |
| `PAD_BLOCK` | 1024 位元組 |
| `SYNC_WAIT_SECONDS`（預設值） | 80 |
| `REQUEST_TIMEOUT_SECONDS`（預設值） | 1800 |
| `RESULT_TTL_SECONDS` | 完成後保留 600 秒 |
| `MAX_INFLIGHT`（預設值） | 150 |
| `WS_HEARTBEAT_SECONDS` | 25 |
| `WS_FIRST_FRAME_SECONDS` | 10 |

---

## 2. 請求信封（Request envelope）

用戶端送給閘道的就是這個信封，閘道原封不動轉給 worker：

```json
{
  "protected": "<標頭 JSON 位元組的 b64u>",
  "sealed":    "<enc ‖ 密文 的 b64u>",
  "sig":       "<64 位元組 Ed25519 簽章的 b64u>"
}
```

三個欄位都必須是字串，不能多也不能少。`sealed` 解碼後至少 48 位元組（32 位元組 `enc` 加 16 位元組 AEAD 標籤），
`sig` 解碼後必須剛好 64 位元組。簽章包住的是傳輸中的字串本身，而不是重新序列化的 JSON，
所以不需要任何「標準化 JSON」，也避開了跨語言簽章常見的錯誤。

### 2.1 protected 標頭

編碼前的樣子：

```json
{
  "v": 1,
  "typ": "llmop-request",
  "request_id": "Zm9vYmFyYmF6cXV1eDEyMw",
  "ts": 1795000000,
  "key_id": "<worker 的 key_id，22 字元>",
  "sig_key_id": "<用戶端的 sig_key_id，22 字元>",
  "model": "qwen3:8b",
  "max_tokens": 1024
}
```

`protected` 就是這個 JSON 的 UTF-8 位元組再做 `b64u`。`Header.encode()` 輸出緊湊 JSON（沒有多餘空白）；
收方不在乎序列化細節，因為它只解析、不重新編碼。

| 欄位 | 型別 | 為什麼閘道可以看到 |
|---|---|---|
| `v` | 整數 | 拒絕它不會說的版本 |
| `typ` | 字串，固定為 `"llmop-request"` | 讓被簽署的物件有自己的網域，不會和日後其他簽署物件混淆 |
| `request_id` | 字串 | 去除重複、輪詢用的代號、紀錄關聯 |
| `ts` | 整數 | 及早、便宜地拒絕過期封包 |
| `key_id` | 字串 | 路由到持有這把金鑰的 worker；支援金鑰輪替 |
| `sig_key_id` | 字串 | 確認簽章用的是這把 API 金鑰登記的簽章金鑰 |
| `model` | 字串 | 確認 worker 有提供這個模型；依模型計量 |
| `max_tokens` | 整數 | 配額准入（token 預算） |

範圍檢查：`v == 1`；`typ` 必須相符；`request_id`、`key_id`、`sig_key_id` 必須是解碼後 16 位元組的 `b64u`；
`ts >= 0`；`model` 是 1 到 200 個字元的字串；`max_tokens` 在 1 到 1,000,000 之間。

其他所有東西都在 `sealed` 裡面，閘道看不到：`messages`、`tools`、`tool_choice`、取樣參數、`response_format`、
圖片，以及一次性的回覆公鑰。

### 2.2 衍生值

以下的 `protected` 與 `sealed` 指的是傳輸中那兩個字串的 ASCII 位元組，一字不改：

```
signing_input = "LLMOP/v1/sig/request"   ‖ 0x00 ‖ protected ‖ "." ‖ sealed
sig           = Ed25519-Sign(用戶端簽章私鑰, signing_input)
request_hash  = SHA-256(signing_input)
info_request  = "LLMOP/v1/hpke/request"  ‖ 0x00 ‖ SHA-256(protected)
info_response = "LLMOP/v1/hpke/response" ‖ 0x00 ‖ request_hash
```

程式中：`Envelope.signing_input()`、`Envelope.request_hash()`；兩個 info 字串在 `llmop.crypto` 內部計算。

### 2.3 請求明文

JSON，然後依 [SECURITY.md](SECURITY.md) 的方式填充：

```json
{
  "reply_key": "<一把全新的一次性 X25519 公鑰的 b64u>",
  "body": { "messages": [...], "tools": [...], "temperature": 0.7, "...": "任何 OpenAI chat-completions 欄位" }
}
```

明文物件只能有 `reply_key`（解碼後 32 位元組）與 `body`（物件）兩個欄位。

`body` 的規則：

- `stream` 必須不存在或為 `false`。否則 worker 回封裝起來的錯誤 `bad_request`。
- `body` 裡的 `model` 會被忽略，worker 用 `header.model`。
- `body` 裡的 `max_tokens` 與 `max_completion_tokens` 會被忽略，worker 用 `header.max_tokens`。
  這樣閘道看到的配額數字才是真的。

### 2.4 回覆明文

JSON，然後填充。只有兩種形狀：

```json
{ "ok": true,  "body": { "id": "chatcmpl-…", "object": "chat.completion", "choices": [...], "usage": {...} } }
```

```json
{ "ok": false, "error": { "code": "backend_error", "message": "…" } }
```

---

## 3. 用戶端 ↔ 閘道的 HTTP

所有 `/v1/*` 呼叫都帶 `Authorization: Bearer <API 金鑰>`。

### 3.1 `POST /v1/chat/completions`

本文就是第 2 節的信封。閘道依下列順序處理，遇到第一個失敗就停：

1. 本文大小、JSON 形狀、解析標頭。
2. 查 API 金鑰（且未被撤銷）。
3. `header.sig_key_id` 等於這把 API 金鑰登記的簽章金鑰的 `sig_key_id`，且簽章驗證通過。
4. `|now − ts| ≤ 300`。
5. `(api_key_id, request_id)` 之前沒出現過。
6. 配額：今天的請求數低於上限，且今天已用 token 數加上 `max_tokens` 不超過 token 上限。
7. 有一台已連線的 worker 公告了這個 `key_id`，而且那台 worker 提供 `model`。
8. 容量：全域進行中請求數低於 `max_inflight`，且該 worker 的待處理數低於它的上限（`8 × max_concurrency`）。
9. 把這筆請求計入配額、轉送給 worker，最多等待 `sync_wait_seconds`。

回應：

| HTTP | 本文 | 意義 |
|---|---|---|
| 200 | `{"request_id": "…", "status": "done", "sealed": "<b64u>"}` | worker 產生了封裝好的回覆。回覆本身可能是封裝起來的錯誤。 |
| 202 | `{"request_id": "…", "status": "pending"}` | 還在跑，請輪詢。 |
| 4xx / 5xx | `{"request_id": "…", "status": "error", "error": {"code": "…", "message": "…"}}` | 在產生封裝回覆之前就失敗，或根本沒有封裝回覆。如果連標頭都解析不了，就省略 `request_id`。 |

為什麼要 202 再輪詢：Cloudflare 的邊緣節點大約 100 秒沒有回應就會切斷代理中的 HTTP 請求（錯誤 524），
而本機推論常常超過這個時間。所以同步最多等 80 秒，之後改用輪詢。

### 3.2 `GET /v1/requests/{request_id}`

長輪詢，最多等 `sync_wait_seconds`。回應形狀與上面三種相同。
只有當初送出這筆請求的 API 金鑰可以取回；其他金鑰一律得到 `404 unknown_request`。
已完成的結果保留 600 秒，之後丟棄。

### 3.3 `GET /v1/models`

每個「（模型，線上 worker 金鑰）」組合一筆：

```json
{
  "object": "list",
  "data": [
    { "id": "qwen3:8b", "object": "model", "owned_by": "<worker 名稱>",
      "llmop": { "key_id": "…", "public_key": "<b64u>" } }
  ]
}
```

用戶端在使用任何一筆之前，必須先拿 `public_key` 比對自己釘選的公鑰集合。

### 3.4 `GET /healthz`

回 `{"ok": true}`，不需要驗證。

### 3.5 錯誤碼

`code` 字串是契約；`message` 是自由文字，用繁體中文。對照表在程式中是 `protocol.HTTP_STATUS`。

| code | HTTP | 由誰產生 |
|---|---|---|
| `bad_envelope` | 400 | 閘道、worker |
| `unsupported_version` | 400 | 閘道、worker |
| `payload_too_large` | 413 | 閘道 |
| `unauthorized` | 401 | 閘道 |
| `bad_signature` | 403 | 閘道、worker |
| `untrusted_client` | 403 | worker（用戶端公鑰不在 `pinned_clients` 裡） |
| `stale_timestamp` | 400 | 閘道、worker |
| `duplicate_request` | 409 | 閘道 |
| `replayed` | 409 | worker |
| `quota_exceeded` | 429 | 閘道 |
| `unknown_key_id` | 404 | 閘道、worker |
| `model_unavailable` | 404 | 閘道、worker |
| `worker_busy` | 503 | 閘道 |
| `gateway_busy` | 503 | 閘道 |
| `worker_disconnected` | 502 | 閘道 |
| `decrypt_failed` | 400 | worker |
| `timeout` | 504 | 閘道 |
| `unknown_request` | 404 | 閘道 |
| `internal_error` | 500 | 閘道、worker |

只會出現在封裝回覆裡面的錯誤碼：`bad_request`、`backend_error`、`backend_timeout`、`model_unavailable`。

---

## 4. 閘道 ↔ worker 的 WebSocket

worker 開啟 `GET /worker/ws`，送出文字訊框。每個訊框是一個帶 `type` 欄位的 JSON 物件。
雙方都每 25 秒送一次 WebSocket ping。憑證只放在第一個訊框裡，絕不放在 URL。

程式中：`protocol.parse_frame(text)` 依 `type` 嚴格檢查訊框（格式錯誤丟 `bad_envelope`，版本不對丟
`unsupported_version`），並有每種訊框的建構函式：`register`、`registered`、`hello`、`ready`、`error`、
`models`、`request`、`response_ok`、`response_error`。

### 4.1 註冊握手（只在第一次連線）

```json
→ {"type": "register", "v": 1, "reg_token": "llmop_reg_…"}
← {"type": "registered", "worker_id": "w_3f9a…", "worker_token": "llmop_wk_…"}
```

（`→` 是 worker 送給閘道，`←` 是閘道送給 worker。）

閘道把註冊權杖標記為已使用，儲存 `worker_token` 的雜湊，並保持連線。worker 把 `worker_id` 與
`worker_token` 寫進 `state.json`（權限 0600），然後在同一條連線上接著送 `hello`。

### 4.2 hello（每次連線都要）

```json
→ {"type": "hello", "v": 1, "worker_id": "w_3f9a…", "worker_token": "llmop_wk_…",
   "keys": [{"key_id": "…", "public_key": "<b64u>"}],
   "models": ["qwen3:8b", "llama-3.3-70b"],
   "max_concurrency": 2}
← {"type": "ready"}
```

- `keys[0]` 是目前的金鑰；後面幾把是 worker 仍然接受的舊金鑰。`keys` 不可為空，每一項只有 `key_id` 與 `public_key`。
- `max_concurrency` 是至少為 1 的整數；`models` 是字串陣列，每個名稱最多 200 字元。
- 閘道會用 `public_key` 重新計算每個 `key_id`，對不上就拒絕。
- 如果某個 `key_id` 已經記錄在另一台 worker 名下，以 `key_conflict` 拒絕。
- 閘道把沒看過的金鑰連同 `first_seen_at` 時間存起來，管理介面會顯示。
- 閘道用 `models` 整個取代這台 worker 已存的模型清單。
- 同一個 `worker_id` 的第二條連線會取代第一條；掛在舊連線上的請求以 `worker_disconnected` 失敗。

握手的任何一步失敗，閘道送出 `{"type": "error", "code": "<code>"}` 然後關閉連線。
握手錯誤碼只有：`unauthorized`、`unsupported_version`、`bad_envelope`、`key_conflict`。
第一個訊框必須在 10 秒內到達。

### 4.3 更新模型（worker → 閘道，收到 `ready` 之後隨時可送）

```json
→ {"type": "models", "models": ["qwen3:8b"]}
```

### 4.4 轉送請求（閘道 → worker）

```json
← {"type": "request", "request_id": "…",
   "envelope": {"protected": "…", "sealed": "…", "sig": "…"},
   "client_sig_pub": "<這把 API 金鑰登記的 Ed25519 公鑰的 b64u>"}
```

`envelope` 依第 2 節嚴格檢查；`client_sig_pub` 解碼後是 32 位元組；
`request_id` 必須與信封標頭裡的 `request_id` 相同（`parse_frame` 會檢查）。

### 4.5 回覆（worker → 閘道）

```json
→ {"type": "response", "request_id": "…", "ok": true, "sealed": "<b64u>",
   "usage": {"prompt_tokens": 812, "completion_tokens": 240}}
```

```json
→ {"type": "response", "request_id": "…", "ok": false, "error": "<code>"}
```

- `ok: true` 表示 worker 解開了請求並產生封裝好的回覆；那個回覆也可能是封裝起來的錯誤，此時 `usage` 兩個數字都是 0。
- `ok: false` 只帶一個第 3.5 節表格裡的裸錯誤碼，沒有別的。worker 絕不把後端文字或請求內容放進明文訊框。
- `usage` 刻意是明文：閘道把它加進這把金鑰當天的 token 用量。兩個數字都是非負整數，不能多也不能少欄位。
- 如果 `response` 的 `request_id` 不是待處理中的請求，閘道忽略它並寫一筆紀錄。
