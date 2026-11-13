# 交接文件 — LocalLLMOutsiderProvider v0.1

> 最後更新：2026-11-13。給接手的人（包含未來的自己）。先讀這份，再看 [研究報告](research/RESEARCH_REPORT.md) 和 [開發計畫書](plan/DEVELOPMENT_PLAN.md)。

## 1. 一句話

把家裡跑的本地 LLM（Ollama / vLLM / LM Studio …），透過 Cloudflare tunnel 分享給外面的人用。前面加一層 **登入、API key、token 額度**，API 跟 OpenAI 相容。

## 2. 最重要的觀念：架設端 vs 應用端

常見的誤會是「whl 是給使用者裝的」。**不是。** 這個專案只有「架設端」；應用端不需要裝我們的任何東西。

```
架設端（你家的 Windows 主機）                  應用端（朋友、你的手機、你的其他程式）
┌────────────────────────────┐               ┌───────────────────────────────┐
│ Ollama / vLLM（本地 LLM）    │               │ 不用裝我們的任何東西            │
│ llmop  ← 本專案 / whl 就是它  │ ◀── 網路 ───  │ 用現成的 OpenAI SDK / curl /    │
│ cloudflared（對外的通道）     │   https       │ Open WebUI / Cline / Continue   │
└────────────────────────────┘               └───────────────────────────────┘
```

| | 架設端 | 應用端 |
|---|---|---|
| 是誰 | 有 GPU / Ollama 的那台電腦的主人 | 拿到帳號的朋友、自己的其他裝置 |
| 要裝什麼 | 本專案（whl 或 git clone）、cloudflared、Ollama | **什麼都不用**，用任何 OpenAI 相容工具 |
| 要做什麼 | 啟動服務、在 `config.yaml` 開帳號、設額度 | 登入網頁 → 申請 API key → 在程式裡設 `base_url` + `api_key` |
| 拿到什麼 | 一個公開網址 `https://xxx.trycloudflare.com` | 網址 + `sk-llop-...` key |

應用端的程式長這樣（`openai` 是 OpenAI 官方的套件，不是我們的）：

```python
from openai import OpenAI
client = OpenAI(base_url="https://xxx.trycloudflare.com/v1", api_key="sk-llop-...")
client.chat.completions.create(model="qwen3.5:9b", messages=[{"role": "user", "content": "你好"}])
```

這正是選擇「OpenAI 相容」的原因：應用端不需要我們另外寫 SDK，業界現有的工具全都能直接接。

## 3. whl 是什麼？為什麼需要？

- **whl（wheel）** 是 Python 套件的打包格式，可以想成 Python 版的「安裝檔」。
- 裡面就是整個 llmop：閘道、網頁 UI、`llmop` 指令。跟 `git clone` 之後跑 `Setup.bat` 裝到的是**同一套東西**，只是不需要整個 repo。
- 它的目的只有一個：讓**架設端**好裝、好更新、版本清楚（`0.1.0`、`0.2.0` …）。
- 它也是其他安裝方式的「原料」：免安裝版 zip、Docker image、uv 一行安裝，背後裝的都是這個 whl。

### 為什麼不是直接一個 exe？（常見疑問）

直覺上，架設端應該是「下載一個 exe 或安裝程式 → 雙擊 → 服務就起來」。**這個直覺是對的，最終交付就該長這樣。**

whl 只是中間產物，原因如下：

1. 這個專案是 Python 寫的。Python 程式要跑，電腦上必須有 Python。
2. whl 是 Python 程式最原始的打包成品，等於是「零件」。它假設電腦上已經有 Python。
3. 「exe / 安裝包」是在 whl 外面再包一層，**把 Python 本身也一起帶進去**，電腦上沒有 Python 也能跑。

所以順序是：原始碼 → whl（零件）→ 免安裝版 zip 或 exe（成品）。

v0.1.0 一開始只交付了 whl，後來補上了**免安裝版 zip**，它就是給架設者用的成品。

裝好之後，llmop 本身就是一個 **web-app**：雙擊啟動、瀏覽器打開，就是登入、用量、API key 管理和 admin 頁面。安裝包只負責「把它放到電腦上」。

### 架設端的安裝方式比較

| 方式 | 架設者要做什麼 | 需要先裝 Python？ | 狀態 | 適合 |
|---|---|---|---|---|
| **免安裝版 zip**（`llmop-<版本>-windows-x64-portable.zip`） | 解壓縮 → 雙擊 `Start.bat`（第一次會問 admin 密碼） | **不用**，zip 內含 Python 3.12 | ✅ 已完成（**推薦**） | 一般使用者、新電腦 |
| `git clone` + `scripts\windows\Setup.bat` | clone，雙擊 | 要 | ✅ 已完成 | 開發者、想改程式的人 |
| `pip install xxx.whl` | 下載 whl，打一行指令 | 要 | ✅ 已完成 | 會用 Python 的人 |
| Docker compose | 見 [部署手冊](DEPLOYMENT_GUIDE.html) | 不用（要 Docker） | ✅ 已完成（image 還沒實際 build 過） | 想連 Ollama/vLLM 一起容器化 |
| uv 一行安裝（`install.ps1`） | PowerShell 貼一行 | 不用 | 💡 未做 | 想要 `upgrade` 一行更新的人 |
| 單一 `llmop.exe`（PyInstaller）或安裝精靈（Inno Setup） | 雙擊 | 不用 | 💡 未做 | 想要「開始功能表捷徑」那種體驗；exe 容易被防毒誤判 |

**免安裝版 zip 的內容**（由 `scripts/build_portable.py` 產生，`release.yml` 打 tag 時會自動 build）：

```
llmop-0.1.0/
  python/              自帶的 Windows Python 3.12（python-build-standalone，uv 用的同一套）
                       llmop 與相依套件已裝在 python/Lib/site-packages
  Start.bat            只給本機用，自動開瀏覽器；第一次啟動會建立 config.yaml、詢問 admin 密碼
  Start-With-Tunnel.bat  加上公開網址；沒有 cloudflared 會問要不要用 winget 安裝
  Add-User.bat         新增帳號
  Check.bat            檢查設定、後端連線、用量
  Command-Prompt.bat   開一個可以直接打 llmop 指令的視窗
  config.example.yaml, README.txt, docs/
```

第一次啟動後，資料夾裡會多出 `config.yaml`（帳號、額度）和 `data\`（key、用量資料庫）。

更新方式：把新版解壓到**新資料夾**，再把舊資料夾的 `config.yaml` 和 `data\` 複製過去。

自己 build：

```bash
python -m build
```

```bash
python scripts/build_portable.py
```

## 4. 目前狀態（v0.1.0）

| 項目 | 狀態 |
|---|---|
| 核心（設定、SQLite、密碼、API key、session、額度） | ✅ |
| API 層：`/v1/models`、`/v1/chat/completions`（含串流）、`/v1/completions`、`/v1/embeddings`、`/v1/me` | ✅ |
| 後端 adapter：Ollama、vLLM、LM Studio、llama.cpp、任何 OpenAI 相容伺服器 | ✅ |
| 網頁：登入、用量（額度條、30 天圖）、API key 管理、playground、admin 頁 | ✅ |
| Cloudflare quick tunnel / named tunnel | ✅ |
| CLI：`init`、`serve`、`check`、`add-user`、`set-password`、`hash-password`、`usage`、`keys`、`revoke-key`、`tunnel` | ✅ |
| Windows .bat：Setup、Start、Start-With-Tunnel、Add-User、Check | ✅ |
| Windows 免安裝版 zip（內含 Python） | ✅ build 過（23 MB），還沒在 Windows 上實跑 |
| Docker：Dockerfile + 4 個 compose 檔 | ✅（compose 檔驗證過，image 還沒實際 build） |
| CI（ubuntu + windows，py3.10/3.12）、tag 觸發的 Release workflow | ✅ 已寫好，還沒在 GitHub 上跑過 |
| 測試 | 221 個全過 |
| **推到 GitHub** | ❌ **被擋（403）**，見第 6 節 |

### 驗證過 / 沒驗證過

- ✅ 已驗證：從 whl 安裝到乾淨環境，接假的 Ollama，跑完整流程都正常（登入 → 申請 key → 列模型 → 非串流 / 串流 → 沒 key 回 401 → 用量有記到 → `llmop check` 顯示 OK）。
- ✅ 已驗證：網頁用 headless 瀏覽器測過（桌機、手機寬度、亮色、暗色），沒有 CSP 錯誤。
- ❌ 還沒驗證：
  - 真的 Ollama
  - 真的 cloudflared
  - 官方 `openai` SDK
  - 在 Windows 上實際執行 .bat 和免安裝版 zip
  - 實際 build Docker image

  第一次在 Windows 主機上跑時，這些就會一起驗證到。

## 5. 程式地圖

```
src/llmop/
  config.py     YAML 設定（帳號、額度、後端、tunnel），存檔自動重載
  store.py      SQLite：api_keys / sessions / usage_events / kv
  security.py   scrypt 密碼、sk-llop- key（只存 sha256）、session id
  quota.py      額度：每日/每月 token、每分鐘/每日請求、同時請求、單次 max_tokens
  auth.py       API key 驗證（API 層）、session cookie 驗證（網頁層）、真實 IP
  app.py        組裝 FastAPI：/v1/*（API）、/ui/*（網頁）、/health
  backends/     adapter pattern（參考 SYNOX）：base → OpenAICompatAdapter → OllamaAdapter；router 負責模型路由
  api/          OpenAI 相容端點、轉送、SSE 串流、用量記帳、錯誤格式
  web/          網頁路由 + static/（純 HTML/JS/CSS，不依賴 CDN）
  tunnel.py     cloudflared 管理（參考 LINEBot）：抓網址、自動重啟
  cli.py        llmop 指令
scripts/windows/  .bat 啟動器
docker/           Dockerfile、compose 檔、Docker 版設定範例
docs/             研究報告、開發計畫書、部署手冊、本文件
```

設計原則：

- 「前端分層」：API 層**只收 API key**，網頁層**只收 session cookie**。
- 帳號和額度放 YAML，由人改；key、session、用量放 SQLite，由程式寫。
- 永遠只綁 `127.0.0.1`，只透過 tunnel 對外；Ollama 的 11434 絕不對外。

## 6. 待辦與已知問題

**必須先處理**

1. **GitHub 權限**：push 回 403。請到 https://claude.ai/connect-github 重新連結，並把 Claude GitHub App 安裝到這個 repo。或者用交付的 `LocalLLMOutsiderProvider.bundle` 自己推：
   ```bash
   git fetch LocalLLMOutsiderProvider.bundle claude/youthful-galileo-u66ybv:claude/youthful-galileo-u66ybv
   ```
   ```bash
   git push -u origin claude/youthful-galileo-u66ybv
   ```
2. 推上去後合併到 main，打 tag `v0.1.0` → `release.yml` 會自動 build，並建立附 whl 的 Release。
3. 在 Windows 主機上實測：解壓免安裝版 zip → `Start-With-Tunnel.bat` → 用手機 4G 連公開網址。

**建議下一步（v0.2）**

- 視需要做安裝精靈（Inno Setup，有開始功能表捷徑）或 uv 一行安裝（第 3 節）。
- **換網址問題**：quick tunnel 每次重啟網址都會變。可選的解法：
  - 長期解：named tunnel 加上自己的網域，網址固定。程式已支援，設 `tunnel.mode: named`。
  - 網址變更通知：把新網址推到 LINE / Telegram / webhook。LINEBot 的 `webhook_window.py` 有現成邏輯可以搬。
  - 一個固定的「查目前網址」端點，例如放在 GitHub Pages 或 Gist。
- 視需要再做「應用端套件」（例如 `llmop-client`）：包住 OpenAI SDK，網址換了能自動去查新網址。只有在不想用 named tunnel 時才值得做。
- Anthropic `/v1/messages`：Ollama / vLLM 已經有原生端點，可以直接透傳；要做的只有解析用量。

**已知限制（設計上接受）**

- Cloudflare 官方說 quick tunnel 不支援 SSE 串流。實際上通常可以用，不穩時改用 `stream: false`。
- 非串流請求超過約 100 秒會收到 524。
- Ollama 預設 context 很小，超過會靜默截斷。請在 Ollama 那邊設 `OLLAMA_CONTEXT_LENGTH`；它的 `/v1` 不吃每個請求帶的 `num_ctx`。
- 額度是軟性的：單一請求可能稍微超過（業界作法）。
- Ollama 有 prompt cache 時，`prompt_tokens` 可能少算。

**目前的預設決定（要改再說）**

- `max_api_keys: 0`：該帳號不能申請 key，只能用網頁 playground。
- 失敗或中途取消的請求也算進「每日請求數」。
- 每日、每月額度以主機的當地時間重置。

## 7. 開發方式紀錄

- Phase 0 盤點：3 個 agent 平行調查 LINEBot 的 trycloudflare、SYNOX 的 adapter，以及業界 API 標準。
- 核心：由主 agent 先寫核心模組和介面契約（[開發計畫書](plan/DEVELOPMENT_PLAN.md) §3）。
- Phase 1：3 個 agent 依契約平行實作 —— A 是後端 + API 層，B 是網頁，C 是 tunnel / CLI / Windows / Docker / CI。
- Phase 2：主 agent 整合、修正跨模組問題、寫文件、build。
