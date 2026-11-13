# 研究報告 — 把本地 LLM 安全地分享出去

> 2026-11-13。來源：(1) 業界 API 慣例（OpenAI、Anthropic、LiteLLM、OpenRouter）的網路搜尋與已知規格；
> (2) 自家 `LINEBotForChurch` 的 trycloudflare 實作；(3) 自家 `SYNOX` 的 LLM adapter 與 Ollama/vLLM 經驗。
> 標記 **[官方]** 的是透過搜尋確認過官方文件的；其餘是已知規格，請以你實際跑的版本為準。

## 0. 結論先講

1. **對外只開一個有驗證的閘道（gateway），絕不直接開 Ollama 的 11434。** Ollama 沒有任何驗證，誰連得到就能用你的 GPU，還能下載、刪除模型。
2. **API 用 OpenAI 相容格式**（`/v1/chat/completions`、`Authorization: Bearer`）。Ollama、vLLM、LM Studio、llama.cpp 都已經說這個語言，所有現成工具（OpenAI SDK、Open WebUI、Continue、Cline、LangChain）也都會說。
3. **trycloudflare 很方便，但有三個限制**：網址每次重啟都會變、官方寫明不支援 SSE 串流 **[官方]**、同時 200 個請求上限 **[官方]**。自用、朋友試用夠；要穩定就改用 named tunnel（自己的網域，免費）。
4. **額度採業界的「軟性」做法**：事前檢查、把 `max_tokens` 夾到剩餘額度、事後依實際用量記帳。這跟 LiteLLM、OpenRouter 一樣。

## 1. 業界 API 標準（OpenAI）

| 項目 | 規格 |
|---|---|
| 驗證 | `Authorization: Bearer sk-...` |
| 模型列表 | `GET /v1/models` → `{"object":"list","data":[{"id","object":"model","created","owned_by"}]}` |
| 對話 | `POST /v1/chat/completions`，回 `choices[].message` + `usage{prompt_tokens,completion_tokens,total_tokens}` |
| 串流 | `text/event-stream`；每筆 `data: {chunk}\n\n`；最後 `data: [DONE]` |
| 串流用量 | 請求帶 `stream_options:{"include_usage":true}`，最後多一個 `choices:[]` 且有 `usage` 的 chunk |
| 舊式補全 | `POST /v1/completions` |
| 向量 | `POST /v1/embeddings` |
| 錯誤 | `{"error":{"message","type","param","code"}}` |

錯誤碼慣例（本專案照用）：

| HTTP | code | 意思 |
|---|---|---|
| 400 | `invalid_request_error` | 請求格式錯 |
| 401 | `invalid_api_key` | 沒帶 key 或 key 無效 |
| 403 | `model_not_allowed` | 這個帳號不能用這個模型 |
| 404 | `model_not_found` | 沒有這個模型 |
| 429 | `rate_limit_exceeded` | 太頻繁，等 `Retry-After` 秒再試 |
| 429 | `insufficient_quota` | 額度用完（SDK 不會自動重試） |
| 502/503/504 | — | 後端（Ollama）出錯 / 沒開 / 逾時 |

回應標頭：`x-request-id`、`x-ratelimit-limit-requests`、`x-ratelimit-remaining-requests`、`x-ratelimit-reset-requests`、`x-ratelimit-*-tokens`。

串流中途出錯時 HTTP 狀態已經是 200，所以改送 `data: {"error":{...}}` 再結束。

## 2. Anthropic Messages API

- 標頭 `x-api-key`、`anthropic-version: 2023-06-01`；`POST /v1/messages`；`system` 在最外層；`max_tokens` 必填。
- 串流是具名事件：`message_start` → `content_block_start` → `content_block_delta`×N → `content_block_stop` → `message_delta`(含 `usage.output_tokens`) → `message_stop`，沒有 `[DONE]`。
- 錯誤 `{"type":"error","error":{"type","message"}}`。

**決策**：v0.1 不做 `/v1/messages`。但**接受 `x-api-key` 標頭**當作 API key（成本很低，LiteLLM 也這樣做）。Ollama 和新版 vLLM 已經有原生的 Anthropic 相容端點 **[官方]**，未來可以直接透傳，只要多解析用量即可。

## 3. API key 的業界作法

- 格式：可辨識的前綴 + 至少 32 bytes 亂數 → 本專案 `sk-llop-` + `token_urlsafe(32)`。前綴讓 secret scanner 和 log 搜尋抓得到。
- **只在建立時顯示一次**，資料庫只存 `sha256(key)`。key 本身是高熵亂數，所以用快速雜湊就安全，不必用慢的密碼雜湊（否則每個 API 請求都會變慢）。
- 顯示時用 `sk-llop-...a1B2`（前綴 + 末 4 碼）、名稱、建立時間、最後使用時間。
- 撤銷是軟刪除（`revoked_at`）；一個帳號可以有多把 key，換 key 就是「先建新的、再撤舊的」。
- log 不能出現完整 key。

## 4. 額度與記帳（LiteLLM / OpenRouter）

- **LiteLLM**：virtual key 有 `max_budget`、`budget_duration`（如 `30d` 週期重置）、`tpm_limit`、`rpm_limit`、`max_parallel_requests`、模型白名單；分 key / user / team 多層。事前檢查、事後記帳，所以一個請求可能超過一點 **[官方]**。
- **OpenRouter**：預付 credits，每把 key 可設上限；用完回 402，太頻繁回 429。
- **串流時記帳**：優先用後端給的 usage；沒有就自己估（約 4 字元 = 1 token）並標記為估算。客戶端中途斷線也要在 `finally` 裡記到目前為止的用量。

本專案採用：

| 限制 | 存哪 | 說明 |
|---|---|---|
| 每日 / 每月 token | SQLite `usage_events` 加總 | 重啟不歸零；以主機當地時間的午夜 / 月初重置 |
| 每日請求數 | SQLite | 同上 |
| 每分鐘請求數 | 記憶體滑動視窗 | 重啟歸零無妨 |
| 同時請求數 | 記憶體計數 | 保護單張 GPU |
| 單次 max_tokens | 直接夾請求 | 也會夾到「剩餘額度」 |

## 5. Ollama / vLLM 的眉角（來自 SYNOX 實戰 + 文件）

**Ollama**

- 預設只聽 `127.0.0.1:11434`；設 `OLLAMA_HOST=0.0.0.0` 會對外，但**沒有驗證**。本專案永遠用本機連它。
- `/v1` 相容層支援 chat / completions / models / embeddings；串流時要帶 `stream_options.include_usage` 才會給用量 **[官方]**（舊版約 0.5 以前沒有）→ 閘道一律幫你加上，客戶端沒要就把那個 chunk 濾掉。
- **最大的雷：context 預設只有 4096**，超過時會**靜默砍掉最前面**（通常是 system prompt），模型突然變笨又不報錯。`/v1` 不能設 `num_ctx`，要用環境變數 `OLLAMA_CONTEXT_LENGTH` 或 Modelfile 的 `PARAMETER num_ctx`。（SYNOX 直接用原生 `/api/chat` 傳 `options.num_ctx`。）
- 原生 `/api/chat` 的最後一筆 `done:true` 有 `prompt_eval_count`、`eval_count`；**有 prompt cache 時 `prompt_eval_count` 會少算**，所以用量可能偏低。
- 模型沒 pull 會回 404「try pulling it first」→ 閘道轉成清楚的 404。
- 圖片要用 data URI，`/v1` 收到 http 圖片網址會回 400（SYNOX ADR 0021）。
- `think:false` 舊版不認得；用 `/v1` 時可用 `reasoning_effort: none`。

**vLLM**

- `--api-key` 可設後端 key（設定裡 `api_key: env:VLLM_API_KEY`）。
- `stream_options.include_usage` 支援，另有 `continuous_usage_stats`（每個 chunk 都帶累計用量）**[官方]**。
- context 由伺服器 `--max-model-len` 決定，客戶端不能改。`/v1/models` 會回 `max_model_len`。

**SYNOX 的 adapter 設計值得借鏡的地方**

- 中性型別 + 一個 `ChatAdapter` 基底類別，用「能力旗標」（`supports_tools`、`supports_vision`）讓呼叫端決定行為。
- `server` 只是第二層開關，ollama 走原生、其他都走 OpenAI 相容。
- 健康檢查**唯讀**：只查 `/api/tags`、`/api/show`、`/v1/models`，不載入模型（那台機器經不起每次測試都載模型）。
- `root_url()` 會去掉使用者多貼的 `/v1`。
- 可注入 `opener`，測試不需要開 socket。

本專案對應：`BackendAdapter` 基底 + `OpenAICompatAdapter` + `OllamaAdapter`（覆寫模型列表、context 資訊）；用 httpx 的 transport 注入做測試。因為我們是**代理**而不是客戶端，所以全部走 `/v1` 透傳最省事、最不會破壞客戶端期待的格式。

## 6. Cloudflare Tunnel

| | Quick tunnel（trycloudflare） | Named tunnel | Tailscale Funnel | ngrok 免費 |
|---|---|---|---|---|
| 費用 | 免費、免帳號 | 免費，需要放在 Cloudflare 的網域 | 免費 | 免費（有額度） |
| 網址 | 隨機，**每次重啟都變** | 固定，自己的網域 | 固定 `*.ts.net` | 1 個固定網域 |
| SSE 串流 | **官方寫不支援** | 支援 | 支援 | 支援 |
| 上限 | 200 個同時請求、無 SLA | 正常 | 頻寬有限 | 請求/流量上限 |
| 適合 | 測試、自用 | **長期正式用** | 不想買網域 | 臨時展示 |

其他限制：

- `~/.cloudflared/config.yaml` 存在時 quick tunnel 起不來 **[官方]**。
- Cloudflare 約 100 秒沒有任何回應位元組會回 524。所以慢模型盡量用串流；閘道在等第一個 token 時每 15 秒送 `: keepalive`。
- 新網址要 10～60 秒才在外部解析得到（LINEBot 的實戰經驗）。

**LINEBot 的作法（直接沿用）**

- cloudflared 由使用者用 `winget install --id Cloudflare.cloudflared` 安裝；程式依序找 PATH、`Program Files (x86)`、`Program Files`。
- Python 逐行讀 cloudflared 輸出，**不要經過 PowerShell**（會整塊緩衝）。
- 用 regex `https://[a-z0-9-]+\.trycloudflare\.com` 抓網址；看到 `Registered tunnel connection` 才算可用。
- 結束時 terminate → 等 5 秒 → kill。
- 網址寫進資料庫跟檔案，使用者透過指令或網頁「拉」最新網址，不用重新貼。

**「換網址怎麼辦」**：v0.1 的作法是：

1. 登入後的網頁顯示目前網址與範例。
2. 寫 `data/public_url.txt`。
3. admin 頁顯示 tunnel 狀態。

長期解法是 named tunnel，或之後加「網址變更通知」（LINE / Telegram / webhook，LINEBot 已有現成邏輯可搬）。

## 7. 網頁登入安全

- **Cookie**：`HttpOnly; SameSite=Lax; Secure`（https 時）。內容是不透明的隨機 session id，資料庫只存它的 sha256。有到期時間，登入時換新 id，登出就刪除。
- **密碼**：stdlib `hashlib.scrypt`（n=2^15、r=8、p=1）。參數跟雜湊值存在一起（`scrypt$n$r$p$salt$hash`），比對用 `hmac.compare_digest`，計算放到 threadpool 不卡住事件迴圈。
- **CSRF**：SameSite=Lax、每個 session 一個 token（`X-CSRF-Token`），再檢查 `Origin`。GET 不改變狀態。
- **登入節流**：每個 IP、每個帳號 15 分鐘內最多 10 次失敗。錯誤訊息一律「帳號或密碼錯誤」；帳號不存在時也跑一次假的雜湊，讓回應時間一樣。
- **真實 IP**：cloudflared 從 127.0.0.1 連進來，所以只有在連線端是信任的 proxy 時，才相信 `CF-Connecting-IP` / `X-Forwarded-For`。
- **標頭**：CSP `default-src 'self'`（不用 CDN、不用 inline script）、`X-Frame-Options: DENY`、`nosniff`。
- **API 與 Web 分開**：API 只收 API key，不收 cookie，所以沒有 CSRF 問題。

## 8. 參考來源

- Cloudflare Quick Tunnels: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/
- Ollama OpenAI compatibility: https://docs.ollama.com/api/openai-compatibility
- Ollama Anthropic compatibility: https://docs.ollama.com/api/anthropic-compatibility
- LiteLLM budgets / virtual keys: https://docs.litellm.ai/docs/proxy/users
- vLLM OpenAI server / Anthropic serving: https://docs.vllm.ai/
- OpenAI API reference: https://platform.openai.com/docs/api-reference
- Anthropic Messages API: https://docs.anthropic.com/en/api/messages
- 內部：`LINEBotForChurch/src/church_bot/tunnel.py`、`SYNOX/src/synox/llm/{base,factory,local}.py`、`SYNOX/docs/adr/0007-local-llm-platform.md`
