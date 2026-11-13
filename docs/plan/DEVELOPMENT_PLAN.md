# 開發計畫書 — LocalLLMOutsiderProvider v0.1

> 狀態：v0.1 實作中（2026-11-13）。研究依據見 `docs/research/RESEARCH_REPORT.md`。

## 1. 目標

把家裡（Windows 主機）跑的 Ollama / vLLM / LM Studio 等本地 LLM，**安全地**分享給少數線上使用者（自己的其他裝置、朋友），且：

- 使用者用 **帳號密碼登入** 網頁，拿到一段時間有效的 **session**（預設 12 小時）。
- 登入後可以 **申請 API key**（只顯示一次），之後程式用 API key 呼叫。
- 每個帳號有 **額度**（每日 / 每月 token、每分鐘請求數、同時請求數、單次 max_tokens），帳號與額度用 **YAML** 管理，使用量在後台（SQLite）記錄。
- 網頁有簡單的 **登入頁、用量頁、API key 管理、試玩（playground）**；admin 可看全部人的用量。
- API 跟業界一樣：**OpenAI 相容**（`/v1/chat/completions` 等），任何 OpenAI SDK、Open WebUI、Continue、Cline 都能直接用。
- 對外用 **Cloudflare quick tunnel（trycloudflare）**，免開 port、免網域；之後可換成 named tunnel 拿固定網址。
- 可用 **Docker** 跟 Ollama / vLLM 放在同一個 compose 裡。

非目標（v0.1 不做）：付費、多租戶隔離、帳號自助註冊、Anthropic `/v1/messages` 轉譯、高可用。

## 2. 架構

```
 使用者 / OpenAI SDK                       瀏覽器
        │  https://xxx.trycloudflare.com      │
        └──────────────┬──────────────────────┘
                 cloudflared (tunnel)
                       │ http://127.0.0.1:8780
        ┌──────────────┴───────────────────────────────┐
        │ llmop (FastAPI, 單一 process)                  │
        │  /v1/*   API 層  ── API key ── 額度 ── 轉送      │
        │  /ui/*   Web 層  ── session cookie + CSRF      │
        │  /health                                       │
        │  config.yaml (帳號/額度/後端)  data/llmop.sqlite3 │
        └──────────────┬───────────────────────────────┘
                       │ OpenAI 相容 /v1  (+ Ollama 原生 /api/tags)
           Ollama :11434   vLLM :8000   LM Studio :1234 ...
```

「前端分層」：API 層與 Web 層在同一個 process，但用路徑分開、認證方式也分開：
API **只收 API key**、Web **只收 session cookie**。這樣 API 沒有 CSRF 問題，key 也不會進到瀏覽器 cookie。

## 3. 模組與介面契約（多 agent 平行開發用）

已完成（核心，由主 agent 撰寫）：

| 模組 | 內容 |
|---|---|
| `llmop/config.py` | `Config`, `Account`, `Quota`, `Backend`, `ServerSettings`, `TunnelSettings`, `load_config`, `parse_config`, `ConfigHolder`(自動重載), `model_allowed` |
| `llmop/security.py` | scrypt 密碼、`new_api_key()`(`sk-llop-…`)、sha256、session id、CSRF token |
| `llmop/store.py` | SQLite：`api_keys`, `sessions`, `usage_events`, `kv`；`Store` 方法見原始碼 |
| `llmop/quota.py` | `QuotaManager.acquire(account) -> Lease`（raise `QuotaDenied(code, message, retry_after)`）、`usage(account)`、`headers(account)` |
| `llmop/auth.py` | `authenticate_api(request,cfg,store) -> ApiCaller`、`web_user(...) -> WebUser|None`、`client_ip`、`SESSION_COOKIE` |
| `llmop/app.py` | `AppState`、`build_state(config, store=None, transport=None)`、`create_app(config|path, state=None)` |

### Phase 1 — 三條平行工作線

**A. 後端 + API 層**（`llmop/backends/`, `llmop/api/`）

- `llmop.backends.BackendRouter(holder: ConfigHolder, transport=None)`
  - `async list_models() -> list[ModelInfo]`（`ModelInfo(id, backend, owned_by, context_length|None)`；快取 30 秒；某個後端掛掉時跳過並記錄）
  - `async resolve(model: str) -> tuple[BackendAdapter, str upstream_model]`（處理 `model_aliases`、`backend/model` 寫法、`:latest`）；找不到 raise `ModelNotFound`
  - `async aclose()`
  - `async health() -> list[dict]`（每個後端：name, type, reachable, models數, error）
- `BackendAdapter`（adapter pattern，參考 SYNOX `ChatAdapter`）：`OpenAICompatAdapter`（vllm / lmstudio / llamacpp / openai）與 `OllamaAdapter`（繼承它：模型列表用 `/api/tags`，`context_size` → `options.num_ctx`，`keep_alive` 等）。每個 backend 有 `asyncio.Semaphore(max_concurrency)`。
- `llmop.api.build_api_router(state) -> APIRouter`：
  - `GET /v1/models`, `GET /v1/models/{model:path}`
  - `POST /v1/chat/completions`, `POST /v1/completions`, `POST /v1/embeddings`
  - `GET /v1/me`（本 key 的帳號、額度、今日用量 —— 方便 CLI/腳本查詢）
- `llmop.api.proxy.forward(state, endpoint, body, *, account, key_id, client_ip) -> starlette Response`
  - Web playground 也會呼叫這個函式（`key_id=None`），所以要獨立於 Request。
  - 流程：模型權限 → `quota.acquire` → 夾 `max_tokens` → 串流時注入 `stream_options.include_usage=true`（使用者沒要就把 usage chunk 濾掉）→ 轉送 → 在 `finally` 記帳 `store.record_usage`（後端沒給 usage 就用字數估算，`usage_source="estimate"`）→ `lease.release()`。
  - 錯誤格式一律 OpenAI：`{"error":{"message","type","param","code"}}`；401 `invalid_api_key`、403 `model_not_allowed`、404 `model_not_found`、429 `rate_limit_exceeded`/`insufficient_quota`（含 `Retry-After`）、502/503/504 後端錯誤。串流中途錯誤送 `data: {"error":...}` 後結束。
  - 回應加上 `x-request-id` 與 `x-ratelimit-*`。
  - SSE：`Cache-Control: no-cache`、`X-Accel-Buffering: no`；等第一個 token 時每 `sse_keepalive` 秒送 `: keepalive`（trycloudflare 官方不保證 SSE，盡量減少被緩衝/逾時）。

**B. Web 層**（`llmop/web/`）

- `llmop.web.build_web_router(state) -> APIRouter`
- 頁面（純 HTML + 原生 JS + 一個 CSS，放 `web/static/`，不依賴 CDN）：`/` → 未登入導向 `/ui/login`；`/ui/login`、`/ui/dashboard`（用量、額度條、每日圖、API keys、playground、API 使用範例含目前公開網址）、`/ui/admin`（admin 才能看：全部帳號用量、後端健康、公開網址、設定檔錯誤）。
- JSON：`POST /ui/api/login`、`POST /ui/api/logout`、`GET /ui/api/me`、`GET /ui/api/usage?days=30`、`GET/POST /ui/api/keys`、`DELETE /ui/api/keys/{id}`、`POST /ui/api/playground/chat`（呼叫 `forward`）、`GET /ui/api/models`、`GET /ui/api/admin/overview`、`POST /ui/api/admin/revoke-key`。
- 安全：cookie `HttpOnly; SameSite=Lax; Secure(https 時)`；所有改變狀態的請求要 `X-CSRF-Token` + Origin 檢查；登入節流（每 IP 與每帳號 15 分鐘 10 次）；未知帳號也跑一次假的 hash；安全標頭（CSP `default-src 'self'`、`X-Frame-Options: DENY`、`nosniff`）。

**C. 營運：tunnel + CLI + Windows + Docker + CI**

- `llmop/tunnel.py`：`TunnelManager(settings, local_url, store)`，背景 thread 跑 `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:PORT`，用 `https://[a-z0-9-]+\.trycloudflare\.com` 抓網址、等 `Registered tunnel connection`，網址寫進 `store.kv_set("public_url")` 與 `data/public_url.txt`，cloudflared 掛掉自動重啟（退避），`stop()` 時 terminate→kill。named 模式用 `tunnel run --token`。
- `llmop/cli.py`（`llmop` 指令 / `python -m llmop`）：`init`、`hash-password`、`add-user`、`serve [--tunnel]`、`check`（設定與後端健康）、`usage`、`keys`。
- `scripts/windows/*.bat`：Setup / Start / Start-with-Tunnel / Add-User。
- `docker/`：Dockerfile + compose（gateway+ollama、gateway+vllm、gateway 接主機上的 ollama、加 cloudflared）。
- `.github/workflows/`：CI 測試 + tag 時 build wheel 並建立 GitHub Release。

### Phase 2 — 整合（主 agent）

接起三條線、端對端測試（假後端）、README、研究報告、Docker 使用說明 artifact、build wheel、release。

## 4. 決策紀錄

| # | 決策 | 理由 |
|---|---|---|
| D1 | Python + FastAPI + httpx + PyYAML；SQLite 存用量 | Windows 好裝；SYNOX/LINEBot 同生態；用量需要查詢，YAML 不適合當記帳本 |
| D2 | 帳號/額度在 YAML，key/session/用量在 SQLite | 使用者要求「YAML 管理、後台紀錄」；YAML 是人改的，SQLite 是程式寫的 |
| D3 | 一律轉 OpenAI 相容 `/v1` | Ollama、vLLM、LM Studio、llama.cpp 都支援；業界客戶端最多 |
| D4 | API key `sk-llop-` + 32 bytes，只存 sha256 | 業界作法（OpenAI/LiteLLM）；高熵 key 不需慢雜湊 |
| D5 | 額度是軟性：事前檢查 + 夾 max_tokens + 事後記帳 | 跟 LiteLLM/OpenRouter 一樣；本地 LLM 不知道會吐幾個字 |
| D6 | 綁 127.0.0.1，只靠 tunnel 對外 | Ollama 本身沒有驗證，絕不直接開 11434 |
| D7 | 串流可用但標示「quick tunnel 不保證 SSE」；有 keepalive；建議重要用途用 named tunnel | Cloudflare 文件明載 quick tunnel 不支援 SSE |
| D8 | 換網址：v0.1 用「登入後網頁顯示目前網址」+ `public_url.txt`；長期建議 named tunnel 固定網域 | 使用者說先當作不換網址 |
| D9 | 不做 Anthropic `/v1/messages` 轉譯（v0.2 再評估直通） | Ollama/vLLM 已有原生 Anthropic 相容端點，可之後直接透傳 |

## 5. 驗收

- `pytest` 全綠（不需要真的 Ollama：用 httpx MockTransport 假後端）。
- `python -m build` 產出 wheel + sdist。
- 本機：`llmop init` → `llmop serve` → 登入 → 申請 key → `curl /v1/chat/completions` 成功並在用量頁看到。
