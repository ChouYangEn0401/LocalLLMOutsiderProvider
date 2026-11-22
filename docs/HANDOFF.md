# 交接文件 — LocalLLMOutsiderProvider v0.2

> 最後更新：2026-11-19（v0.2.0，本機分支 `dev`）。給接手的人（包含未來的自己）。
> 使用方式看 [README](../README.md)；所有設計決策的唯一依據是 [合併計畫第 10 節「決策定案表」](plan/MERGE_PLAN.md)；
> 本版範圍與分工是 [派工單](plan/DISPATCH.md)；等級與用量的規則與數字是 [TIERS_AND_USAGE_RULES.md](plan/TIERS_AND_USAGE_RULES.md)。

## 1. 一句話

把家裡跑的本地 LLM（Ollama / LM Studio / vLLM / llama.cpp）透過 Cloudflare 通道分享給少數親友。前面加上帳號、API key、
等級與兩個用量表、排隊；API 跟 OpenAI 相容；只有一個模型。可以選擇讓個別的 key **端對端加密**（閘道看不到內容）。

v0.2 是兩個原型的合併：A（v0.1 的閘道：帳號、額度、網頁、通道、Windows 打包）為底，併入 B（端對端加密中繼、worker、用戶端外掛）。

## 2. 兩樣交付物

| | 伺服器 | 用戶端外掛 |
|---|---|---|
| 是什麼 | `llmop serve`：三個入口＋兩個通道＋（可選）worker，一個程式 | `llmop client`：SDK、本機代理 `127.0.0.1:8788`、`keygen`／`chat`／`models` |
| 誰裝 | 有 GPU 的那台電腦的主人 | 只有使用**加密** key 的人 |
| 怎麼裝 | Windows 免安裝版 zip，或 `pip install "<whl>[server,secure]"`（v0.3 起要 `[server]`） | `pip install "<whl>[secure]"`，或同一個免安裝版的 `Start-Client-Proxy.bat`；給別的程式用的 SDK 只要 `[sdk]`（[SDK.md](SDK.md)） |

直連（明文）的使用者什麼都不用裝：API 連結＋`sk-llop-` key＋任何 OpenAI 相容工具。

三個入口（全部只綁 `127.0.0.1`）：本機控制台＋免 key `/v1`（8781）、帳號網站（8780，通道 `ui`）、API 連結
（8782，通道 `api`，`/<秘密路徑>/v1`、`/<秘密路徑>/e2e/v1`、`/<秘密路徑>/worker/ws`）。細節見 README 的「三個入口」。

## 3. 目前狀態（v0.2.0）

### 3.1 已經有的

| 項目 | 狀態 |
|---|---|
| 統一節點 `llmop serve`：三個 uvicorn 入口、兩個獨立 `TunnelManager`、節點內建 worker（`roles: [gateway, worker]`） | ✅ |
| 本機判斷（loopback＋無轉送標頭＋`Host`／`Origin` 檢查）、秘密路徑 gate、公開入口的 `Host` 檢查與本文大小上限 | ✅ |
| 帳號（`config.yaml`）、每帳號的 key（`sk-llop-`、`encryption: required/optional`、簽章公鑰）、GUEST key（總額、到期、重置） | ✅ |
| 等級 VIP III／II／I、NORMAL、LOW 狀態、GUEST；1.5 小時桶與每週表（持久化）；事後記帳 | ✅ |
| 排程器：全域名額、等級順序、同級輪流、每帳號同時上限、保底；429＋`Retry-After`、SSE 註解行、加密 `pending.queue`、`/v1/me` | ✅ |
| 單一模型（`model:` 覆寫，`/v1/models` 只回一筆） | ✅ |
| 加密中繼（B 的 gateway 移植為 FastAPI 的 `secure/`，掛在 `/<gate>/e2e/v1`）；B 的 worker 與用戶端（aiohttp）原樣保留 | ✅ |
| 本機控制台＋帳號網站（入口網頁：狀態、兩個用量表、自己的 key、目前的 API 連結） | ✅ |
| 「更換 API 連結」：只換路徑／連主機名一起換、寬限期、稽核紀錄（`data/api_link_audit.jsonl`）；`llmop tunnel rotate-api`／`show-api` | ✅ |
| 後端 context 超限轉成 400 `context_length_exceeded`；名額與後端平行數的啟動警告 | ✅（T1 只做這兩件） |
| Windows 免安裝版 zip：內含 Python 3.12（sha256 寫死）與 `secure` 套件；`Start`、`Start-With-Tunnel`、`Start-Worker`、`Start-Client-Proxy`、`Rotate-API-Link` 等 .bat | ✅ build 過，**沒在 Windows 上跑過** |
| wheel／sdist（`python -m build`） | ✅ |

### 3.2 延後到 v0.3（這一版刻意不做）

- **加密模式的真串流（D3）**：v0.2 沿用 B 的假串流（代理先回 SSE 標頭，完成後一個 chunk 送完）。
- **worker 改用 A 的後端轉接層（D12）**：v0.2 的 worker 沿用 B 自己的後端（只連 loopback），所以 worker 有自己的
  `backends` 設定，要和 `config.yaml` 的 `backends` 分別維護。
- **worker／用戶端設定併入 `config.yaml`（D8）**：v0.2 仍是 `$LLMOP_HOME/worker/config.json`、`$LLMOP_HOME/client/config.json`。
- **無縫換鑰（D6）與前向保密**：v0.2 沿用手動的 `rotate-key` → 更新釘選 → `prune-keys`。
- 區網入口、key 放在網址路徑（`/k/<key>/`）、唯讀儀表板分享連結與其到期設定、用戶端自動取得新的 API 連結。
- **Docker 與 CI 的更新**：`docker/**` 與 `.github/**` 原封不動，是 v0.1 時代的檔案，**沒有為 v0.2 驗證**
  （compose 只開 8780 單一入口、`ci.yml` 的矩陣還有 Python 3.10 但套件要求 ≥ 3.11、`release.yml` 只裝 `[dev]` 沒裝 `[secure]`）。
- **T1（Context size has been exceeded）的完整處理**：只做了錯誤碼轉換與名額警告；截斷、摘要、依 context 自動調整都沒做（[TODO.md](plan/TODO.md)）。
- worker／用戶端的網頁控制台、playground 對外、模型別名介面。

### 3.3 必須在 Windows 上實測的事（還沒有人做過）

1. 解壓免安裝版 zip → `Start.bat`：第一次的問答（LLM 種類、模型、admin 密碼）→ 本機控制台 `http://127.0.0.1:8781/` 能登入。
2. **真的 Ollama 與真的 LM Studio**：非串流與串流、`model` 覆寫、`/v1/models`、用量有記到、LM Studio 的 context 超限訊息真的被轉成
   `context_length_exceeded`、`queue.slots` 與 LM Studio 的平行數不符時控制台有警告。
3. **真的 cloudflared**：`Start-With-Tunnel.bat` 開出兩個 trycloudflare 網址；用手機 4G 連帳號網站登入、建 key；經 API 連結送請求；
   對本機控制台的任何寫入從外網都做不到；`Rotate-API-Link.bat`（含 `--change-host`）後舊連結 404、帳號網站不受影響。
4. 加密路徑：同一台的內建 worker（`roles: [gateway, worker]`＋`llmop worker init`）、`Start-Client-Proxy.bat`、加密 key、
   經代理送一筆；另一台電腦的 `Start-Worker.bat` 經 `wss://…/<秘密路徑>/worker/ws` 註冊。
5. `tzdata`：每週重置在 Windows 上用的時區資料來自 zip 內的 `tzdata` 套件。
6. 依 [TIERS 第 6 節](plan/TIERS_AND_USAGE_RULES.md)量出滿載時的整台出字速度，校正桶與每週表的數字。
7. SYNOX 的三種接法（README「讓 SYNOX 使用」），以及 SYNOX 端的 handoff H-S1／H-S2／H-S3（[MERGE_PLAN 9.6](plan/MERGE_PLAN.md)）。

## 4. 怎麼 build 與發版

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

- `python -m build` 產生 `dist/localllm_outsider_provider-0.2.0-py3-none-any.whl` 與 `.tar.gz`。wheel 內含 `llmop/web/static/*`、
  `llmop/examples/config.example.yaml`、`llmop/secure/`、`llmop/worker/`、`llmop/client/`。
- `scripts/build_portable.py` 產生 `dist/llmop-0.2.0-windows-x64-portable.zip`。它下載（或用 `--python-archive` 指定的本機檔）
  python-build-standalone 的 Windows Python，**sha256 不符就拒絕繼續**（寫死在 `PYTHON_SHA256`；換版本時 `PYTHON_URL` 與雜湊一起改）。
  套件以 `llmop[secure]` 加上 `tzdata`、`colorama` 的 win_amd64 wheel 安裝進 zip 內的 `site-packages`，缺任何一個必要套件就失敗。
  在 Linux 上也能 build（不執行 zip 裡的 Python）。
- 版本號只在 `src/llmop/__init__.py`。發版：合併到 main、打 tag `v0.2.0`；`release.yml` 會 build 並附上檔案（但它是 v0.1 時代的
  workflow，見 3.2，第一次用前先檢查）。
- 更新已安裝的免安裝版：新版解壓到**新資料夾**，把舊資料夾的 `config.yaml` 與整個 `data\`（含 `data\llmop_home` 私鑰）複製過去。

免安裝版的內容：

```
llmop-0.2.0/
  python/                  Windows Python 3.12，llmop[secure] 與相依套件在 python/Lib/site-packages
  Start.bat                整個節點，打開本機控制台；第一次啟動會執行 llmop init
  Start-With-Tunnel.bat    同上，強制開兩個通道；沒有 cloudflared 會問要不要用 winget 安裝
  Rotate-API-Link.bat      llmop tunnel rotate-api
  Start-Worker.bat         只跑 worker 的電腦
  Start-Client-Proxy.bat   使用者那一側的加密代理
  Add-User.bat  Check.bat  Command-Prompt.bat
  config.example.yaml  README.txt  docs/
```

第一次啟動後多出 `config.yaml` 與 `data\`。**`data\` 含私鑰**（`data\llmop_home\worker\keys.json`、`data\llmop_home\client\signing_key.json`），
不要分享。

## 5. 程式地圖

```
src/llmop/
  node.py        v0.2 節點：三個入口、兩個通道、內建 worker、啟動檢查
  app.py         三個 app（local / public-ui / public-api）與 v0.1 單一入口
  cli.py         llmop 指令；__main__.py 把 worker / client 轉給各自的 CLI
  config.py      config.yaml（roles、model、tiers、usage、queue、listeners、tunnels、local_account）
  netguard.py    本機判斷、秘密路徑 gate
  apilink.py     API 連結（秘密路徑、更換、寬限、稽核）
  tunnel.py      cloudflared 管理
  store.py usage.py quota.py scheduler.py   資料庫、用量表、准入、排隊
  api/           OpenAI 相容 /v1（zone local / public）
  web/           本機控制台與帳號網站（surface local / public）
  secure/        加密中繼（/e2e/v1、worker WebSocket）
  worker/ client/ crypto.py protocol.py paths.py   B 的加密角色（aiohttp）
  backends/      A 的後端轉接層
scripts/         build_portable.py、windows/（開發用 .bat）、windows/portable/（免安裝版 .bat）
docs/secure/     加密模式的文件（README、DEPLOY、PROTOCOL、SECURITY）
```

設計原則：外網唯讀、寫入只在本機控制台（D4）；每把 key 決定准不准走明文（D1）；用量事後記帳＋排隊（D5）；
網址決定「怎麼送」（`/v1` 或 `/e2e/v1`），key 的 `encryption` 旗標決定「准不准」。

## 6. 已知限制（設計上接受）

- quick tunnel 的網址在 cloudflared 重啟時會變；帳號網站的網址變了要另外通知使用者。要固定請用 named tunnel。
- 公開 `/v1` 預設不串流；非串流超過約 100 秒會被 Cloudflare 回 524。
- Ollama 預設 context 很小且會靜默截斷；請設 `OLLAMA_CONTEXT_LENGTH`。
- 用量是事後記帳，並發時單一帳號可能超出一點（有上界）。
- 同一台電腦上的其他程式也算「本機」：可以免 key 使用本機 `/v1`。

### 6.1 獨立複查（T-R）後仍未處理的項目

完整清單與修正狀態見 [REVIEW_v0.2.md「修正後狀態」](security/REVIEW_v0.2.md)。以下是**刻意沒修**的：

- **R-07（v0.3）**：`image_url` 原樣轉給後端；會自己抓網址的後端可被當成 SSRF 跳板（README 已註明）。
- **R-14（v0.3）**：TIERS 11「排隊中的帳號變成 LOW → 那筆移到 LOW 的順位」沒做；排隊中的票等級固定在入隊時。
- **R-16（接受）**：同時有 `Content-Length` 與 `Transfer-Encoding: chunked` 時 uvicorn 以 chunked 為準（RFC 允許），
  不是檢查表 9 計劃的「一律 400」；前面有 cloudflared 正規化。
- **R-18（決策落差）**：D7「管理員可代發」目前**只有 GUEST key**；沒有替一般帳號發 key 的本機功能（帳號要自己到帳號網站建立，
  VIP 帳號則在本機控制台用自己的帳號登入建立）。
- **R-15 的殘餘**：串流在 SSE 標頭送出之前就斷線（後端已收到請求）仍記 0 token；標頭送出後、第一個 token 之前斷線才記提示的估算值。
- **R-04 的但書**：v0.1 的 `default_quota` 只有**寫在設定檔裡**的項目才生效；寫了 `max_concurrent` 的話它仍會把排隊中的請求算進去
  （範例設定已不寫）。

## 7. 歷史

- v0.1（2026-11-13）：A 的閘道，221 個測試；B 是獨立的加密中繼原型。v0.1 的原始文件保存在 `docs/BU_Keep_*`。
- v0.2（2026-11-19）：依 [DISPATCH](plan/DISPATCH.md) 分 T-M0、T-1～T-11 合併；T-R 為獨立複查。
