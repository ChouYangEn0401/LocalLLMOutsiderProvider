# 合併計劃書 — 把「直連版（A）」與「端對端加密版（B）」合成一個產品

> 狀態：**草稿，等擁有者回覆決策（第 4 節 D1–D14）**。日期：2026-11-19。撰寫：規劃者（Fable）。
> 本文件只規劃，不改任何原始碼。執行者是 Opus 5.5（medium effort），由 dispatcher 派工。

| 代號 | 來源 | 一句話 |
|---|---|---|
| **A** | 本 repo 分支 `claude/youthful-galileo-u66ybv`（7 commits，測試 221 個全過） | 單一 process 的 FastAPI 閘道：帳號登入、API key、額度、真串流、Cloudflare tunnel、Windows 免安裝版 |
| **B** | `origin/claude/lm-studio-e2e-encryption-ug09c3`（6 commits，測試 563 個全過） | gateway / worker / client 三個角色，HPKE 端對端加密，閘道看不到對話內容 |

兩邊套件目錄都叫 `src/llmop/`，**沒有共同的 git 祖先**。

## 0. 我實際驗證過的事

| 項目 | 結果 |
|---|---|
| A 測試（`.venv`，Python 3.13） | `221 passed` |
| B 測試（scratchpad 另建 venv：aiohttp 3.14.5、cryptography 50.0.2、pyhpke 0.6.5） | `563 passed in 19.8s` |
| 試合併（在 scratchpad 的 clone 裡做，沒動本 repo）`git merge --allow-unrelated-histories` | **只有 5 個檔案衝突**：`.gitignore`、`README.md`、`pyproject.toml`、`src/llmop/__init__.py`、`src/llmop/__main__.py`。其餘 138 個 B 的檔案直接併入，路徑不重疊 |
| 衝突用「留 A 的 4 個檔 + 用 B 的 `__main__.py`」粗暴解掉後跑全部測試 | `780 passed, 4 failed`。4 個失敗都在預期內，見 3.3 |

結論：兩邊的程式**可以先原樣並存**，再逐步整合。這決定了第 3 節的合併方法。

---

## 1. 差異清單（side by side）

### 1.1 架構與信任邊界

| | A（直連） | B（加密分離） |
|---|---|---|
| process | 1 個：閘道就在 LLM 那台機器上，直接呼叫後端（`src/llmop/app.py:65`） | 3 個角色：gateway（對外）、worker（LLM 旁邊，**主動連出** WebSocket，`worker/link.py`）、client（SDK + 本機代理 `127.0.0.1:8788`） |
| 誰看得到對話明文 | 閘道那台機器、**Cloudflare**（它終結 TLS） | 只有用戶端機器與 worker 機器。閘道、閘道紀錄、Cloudflare 都看不到（`docs/SECURITY.md` §9） |
| 用戶端要裝東西嗎 | 不用，任何 OpenAI SDK 直接用（`docs/HANDOFF.md` §2） | **要**：`llmop client`（加密與簽章在用戶端做） |
| LLM 與閘道可否分開放 | 不行（閘道要連得到後端） | 可以，worker 在 NAT 後面也行 |
| 綁定位址 | 預設 `127.0.0.1`，Docker 可設 `0.0.0.0`（`config.example.yaml`） | 閘道**只准** loopback，否則拒絕啟動（`gateway/config.py:28`）；worker 後端 URL 只准 loopback（`worker/config.py:98`） |

### 1.2 安全模型

| | A | B |
|---|---|---|
| 內容保護 | TLS 到 Cloudflare 為止 | HPKE（X25519 + HKDF-SHA256 + ChaCha20-Poly1305，`crypto.py`），每筆請求 Ed25519 簽章，回覆用一次性金鑰加密，1 KiB 填充 |
| 身分 | 帳號密碼（scrypt）→ session cookie + CSRF；API key `sk-llop-…`（只存 sha256） | API key `llmop_sk_…` **加上**簽章金鑰配對：只偷到 key 沒有私鑰送不出請求（SECURITY 檢討 11） |
| 信任錨 | 無（信任閘道） | 用戶端釘選 worker 公鑰（`pinned_workers`），worker 可釘選用戶端（`pinned_clients`） |
| 重放防護 | 無（不需要） | 時間窗 ±300 秒、開機門檻、已見 ID（`worker/replay.py`），閘道去重 |
| 管理介面 | `/ui/admin`，登入後**經 tunnel 也能用** | `/admin/`，預設**只准本機**（`admin_local_only`，看 `Cf-Connecting-Ip`） |
| 紀錄規則 | 一般 | 有靜態測試禁止把內容/秘密寫進 log（`tests/e2e/test_static_logging.py`） |
| 已知弱點 | Cloudflare 看得到明文 | 請求沒有前向保密（SECURITY 檢討 1，標明「需擁有者決定」）；中繼資料可見 |

### 1.3 協定與 API 面

| | A | B |
|---|---|---|
| 用戶端 → 閘道 | 標準 OpenAI JSON | 信封 `{protected, sealed, sig}`（`docs/PROTOCOL.md` §2） |
| 端點 | `/v1/models`、`/v1/models/{id}`、`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/me`、`/health` | `/v1/models`（多一個 `llmop.key_id/public_key`）、`/v1/chat/completions`（收信封）、`/v1/requests/{id}`（輪詢）、`/healthz`、`/worker/ws` |
| **路徑衝突** | `/v1/chat/completions` 收明文 | 同一路徑收信封 → 兩者不能在同一個 port 並存，除非改路徑（D1） |
| 長請求 | 串流；非串流超過約 100 秒會被 Cloudflare 切（524） | 同步等 80 秒 → 回 202 → 長輪詢（PROTOCOL §3.1），**不怕 524** |
| 錯誤格式 | OpenAI 樣式 `{"error":{message,type,code}}`（`api/errors.py`） | `{"status":"error","error":{code,message}}`，錯誤碼表是契約（PROTOCOL §3.5） |
| 預設 port | 8780 | 閘道 8787、用戶端代理 8788 |

### 1.4 串流

| | A | B |
|---|---|---|
| 現況 | 真的 SSE 逐段轉送（`api/proxy.py:347` `_StreamRelay`），中途取消會結算用量 | worker 拒絕 `stream:true`（`worker/handler.py:87`）；用戶端代理**假裝** SSE：等待時送註解行，最後整則回答一個 chunk（`client/proxy.py:164`） |
| 影響 | 打字機效果正常 | SYNOX 一律 `stream:true`（`SYNOX/src/synox/llm/local.py:501`），能用但要等整段算完才出字 |

### 1.5 帳號、金鑰、額度

| | A | B |
|---|---|---|
| 帳號 | YAML 裡多個帳號（admin / user），每人可限制模型（`config.py:69`） | 沒有帳號，只有一把管理員密碼（存在 `gateway.db`） |
| 發 key | **使用者登入後自己申請**（`max_api_keys` 限制） | **只有管理員**在管理介面發，發的時候綁定對方的簽章公鑰 |
| 額度維度 | 每帳號：每日/每月 token、每分鐘/每日請求、同時請求、單次 `max_tokens`（`quota.py`） | 每把 key：每日請求數、每日 token |
| 額度嚴格度 | **軟性**：事後記帳，單筆可能稍微超過 | **硬性預扣**：先把 `max_tokens` 預留，回來再結算（`gateway/store.py:257` `admit`）；B 還特地修過兩個繞過漏洞（commit `e607911`） |
| 日界線 | 主機**當地時間** | **UTC** |
| 用量紀錄 | 每筆一列（模型、後端、狀態、延遲…），有 30 天圖 | 每 key 每日彙總 |

### 1.6 設定與儲存

| | A | B |
|---|---|---|
| 設定檔 | 一份 `config.yaml`（人改，註解多，**存檔自動重載**，`config.py:289`） | 每角色一份 JSON：`~/.llmop/<role>/config.json`，改了要重啟 |
| 位置 | 目前目錄（`./config.yaml`、`./data/`），適合免安裝版資料夾 | `$LLMOP_HOME`（預設 `~/.llmop`），目錄 0700、秘密檔 0600 |
| 資料庫 | `data/llmop.sqlite3`：`api_keys / sessions / usage_events / kv` | `gateway/gateway.db`：`admin / api_keys / usage / workers / worker_keys / worker_models`（兩邊都有 `api_keys` 表，**欄位不同**） |
| 秘密檔 | 無（密碼雜湊在 YAML） | worker 私鑰 `keys.json`（未加密）、`state.json`、用戶端 `signing_key.json` |

### 1.7 網頁 UI、CLI、打包、測試、文件、相依

| | A | B |
|---|---|---|
| 網頁 | 登入、用量頁（額度條、30 天圖）、API key、playground、admin 總覽；靜態檔約 1,700 行。**admin 頁只能看，改設定要編 YAML** | 單頁管理介面約 500 行：key、worker、用量。沒有使用者頁、沒有 playground |
| CLI | 平的：`llmop init / serve / check / add-user / set-password / hash-password / usage / keys / revoke-key / tunnel`（`cli.py:1071`） | 分角色：`llmop gateway init|run`、`llmop worker init|run|show-key|rotate-key|prune-keys`、`llmop client keygen|models|chat|proxy` |
| tunnel | 內建 cloudflared 管理（quick / named、自動重啟、抓網址，`tunnel.py`） | 無，手動跑 `cloudflared` |
| Windows | `.bat` 啟動器、免安裝版 zip（內含 Python 3.12） | 無。而且**現況在 Windows 會壞**：`paths.py:44` 用 `os.fchmod`，Windows 要 Python 3.13 才有（見「無法驗證」） |
| Docker / CI | Dockerfile + 4 個 compose；CI（ubuntu+windows，3.10/3.12）與 release workflow | 無 |
| 測試 | 221 個，同步風格（`TestClient`、`httpx.MockTransport`、`asyncio.run`） | 563 個，`pytest-aiohttp`、`asyncio_mode=auto`；e2e 在 loopback 跑真的三個角色並**錄下閘道收送的每個位元組確認沒有明文** |
| 文件 | README、HANDOFF、研究報告、開發計畫、部署手冊（HTML） | README、DEPLOY、PROTOCOL、SECURITY（品質高，測試會檢查 README 與 SECURITY 的段落一字不差） |
| 相依 | fastapi、uvicorn、httpx、PyYAML；Python ≥ 3.10 | aiohttp、cryptography ≥ 47；Python ≥ 3.11（用了 `asyncio.timeout`，`client/sdk.py:312`） |
| 發行名稱 | `localllm-outsider-provider`（import 名 `llmop`），MIT | `llmop`，沒有 LICENSE 檔 |
| 訊息語言 | CLI 與 log 英文，網頁繁中 | 全部繁中 |

---

## 2. 建議的統一架構

### 2.1 核心想法：一個程式、三種「角色」、用「設定檔方案（profile）」決定開哪些

一台機器上跑的 llmop 叫一個**節點（node）**。節點可以啟用下列角色，一個 process 管全部：

| 角色 | 來自 | 做什麼 |
|---|---|---|
| `gateway` | A 的 FastAPI app，加上 B 的加密中繼 | 對外服務、帳號、額度、網頁 |
| `worker` | B | 在 LLM 旁邊解密、推論 |
| `client` | B | 本機 OpenAI 相容代理，替應用程式加密 |

閘道有兩個**服務面（plane）**，各自可開關：

- **直連面 `direct`**：A 的 `/v1/*`，收明文、真串流、直接呼叫本機後端。
- **加密面 `secure`**：B 的信封 API 與 `/worker/ws`，移到 `/e2e/v1/*`（見 D1），閘道只轉送密文。

### 2.2 設定檔方案（profile）

`config.yaml` 新增最上層的 `profile`，網頁精靈與 `llmop init --profile …` 都只是在寫這一段：

```yaml
profile:
  name: direct              # direct | secure-allinone | secure-gateway | secure-worker | client | custom
  roles: [gateway]          # 這台機器啟用的角色
gateway:
  planes: [direct]          # direct | secure，可兩個都開（D1）
```

| 方案 | 這台機器跑什麼 | 適用情境 | 用戶端 |
|---|---|---|---|
| `direct`（簡單直連） | gateway(direct) + tunnel | 自己的主機自己信任，5 分鐘架好 | 任何 OpenAI SDK |
| `secure-allinone`（加密，全在自己主機） | gateway(secure) + worker + tunnel | 不想讓 Cloudflare 看到內容 | `llmop client` |
| `secure-gateway`（加密，只當閘道） | gateway(secure) + tunnel | 閘道放在不完全信任的機器（VPS、別人的電腦） | — |
| `secure-worker`（加密，只當 LLM 主機） | worker | 配合上面那台閘道 | — |
| `client`（使用者端） | client 代理 | 朋友的電腦、跑 SYNOX 的電腦 | — |
| `custom` | 自選 | 進階：例如同一個閘道同時開 direct 與 secure | 依面而定 |

方案只是「預設值的組合」，所有參數仍可在設定區個別調整。

### 2.3 套件佈局（同一個 `llmop` 套件）

```
src/llmop/
  __init__.py  __main__.py        統一：__main__ 只做分派（保留 if __name__ 守衛）
  cli.py                          A 的平指令 = 閘道/節點指令；新增 worker / client 子指令群
  node.py                    新   依 profile.roles 在同一個 event loop 啟動各角色與 tunnel
  config.py                       A；加 profile / gateway.planes / secure / worker / client 區段
  configedit.py              新   從 cli.py 搬出「保留註解改 YAML」的工具，網頁與 CLI 共用
  settings.py                新   設定服務：驗證 → 原子寫入 → .bak → 回報「立即生效 / 需重啟」
  store.py                        A；api_keys 加 sig_public_key，新增 workers / worker_keys / worker_models
  security.py auth.py quota.py    A；quota 增加「預扣」介面給加密面用
  tunnel.py                       A
  backends/                       A 的 adapter（httpx、可串流）；閘道 direct 面與 worker 共用（D10）
  api/                            A：直連面 /v1/*
  secure/                    新   B 的 gateway/{api,hub,worker_ws}.py 移植到 FastAPI：/e2e/v1/*、/worker/ws
  web/                            A 的網頁 + 新的設定區 + B 管理介面的 worker / 簽章金鑰功能
  protocol.py crypto.py paths.py  B（協定升到 v2 支援串流分段；paths 修 Windows）
  worker/                         B（link / handler / keystore / replay）；後端改用 llmop.backends
  client/                         B（sdk / proxy / config / cli）；加真串流與「直連上游」模式
  gateway/                        B 的原始閘道：整合完成後刪除（歷史保留在 git）
tests/
  test_*.py conftest.py           A 的 221 個，原樣
  test_protocol.py test_crypto.py test_paths.py   B
  secure/  worker/  client/  e2e/                 B（gateway/ 改名 secure/ 並改用 FastAPI 測試工具）
```

沒有任何模組需要改名來避開衝突：A 佔 `api/ backends/ web/` 與頂層檔，B 佔 `client/ worker/ gateway/ crypto.py protocol.py paths.py`。

`pyproject.toml`：

```toml
[project]
name = "localllm-outsider-provider"      # L1：或改成 llmop
requires-python = ">=3.11"               # L2
dependencies = ["fastapi>=0.110", "uvicorn>=0.27", "httpx>=0.27", "PyYAML>=6.0"]
[project.optional-dependencies]
secure = ["cryptography>=47", "aiohttp>=3.9", "websockets>=12"]
dev = ["pytest>=8", "respx>=0.21", "build>=1.0", "pytest-aiohttp", "pytest-asyncio", "pyhpke"]
[project.scripts]
llmop = "llmop.__main__:main"
[tool.setuptools.package-data]
llmop = ["web/static/*", "examples/*"]
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

- `pip install localllm-outsider-provider` → 只有直連版；`…[secure]` → 加密功能。**免安裝版 zip 一律全裝。**
- 沒裝 `secure` 卻選了加密方案：啟動時印出一行明確的安裝指令，不丟 traceback（`llmop.secure` / `worker` / `client` 一律延後 import）。
- CLI：`llmop serve` 依 profile 啟動本節點所有角色；`llmop gateway run` 是 `serve` 的別名、`llmop gateway init` 是 `init` 的別名；`llmop worker …`、`llmop client …` 沿用 B。

### 2.4 加密模式的真串流（取代假 SSE）

**可行，成本中等。** 設計（協定 v2，B 尚未有任何部署，所以直接升版、不保留 v1 相容）：

1. **標頭**多一個已簽署欄位 `stream: true|false`。閘道因此知道要用哪種傳輸；這是新增的一項可見中繼資料。
2. **worker** 用 `llmop.backends` 以串流呼叫後端，把收到的增量**每 150 ms 併成一批**（可設定），每批做一次獨立的單次 HPKE 封裝，收件者是用戶端的一次性 `reply_key`：
   `info = "LLMOP/v2/hpke/stream" ‖ 0x00 ‖ request_hash ‖ seq(8 位元組) ‖ final(1 位元組)`
   每批填充到 256 位元組的倍數。最後一批 `final=1`，帶完整 `usage` 與 `finish_reason`。
   - 沿用 B「每次都是全新單次 context、程式不碰 nonce」的原則（SECURITY 檢討 9），不引入自管 nonce 的對稱串流。
   - `seq` 與 `final` 綁在 `info` 裡：閘道無法調換順序、重放某一段或偷偷截斷（缺 `final` 就是不完整，用戶端報錯）。
   - 成本：每批多 48 位元組與一次 X25519，數百微秒，遠小於推論時間。
3. **worker ↔ 閘道**新增訊框：`{"type":"chunk","request_id","seq","sealed"}`；結尾仍是既有的 `response`（帶明文 usage 供計量）。新增 `{"type":"cancel","request_id"}`：用戶端斷線時閘道通知 worker 停止推論，省 GPU。`hello` 增加 `features: ["stream"]`。
4. **閘道 ↔ 用戶端**兩種取法，同一份資料：
   - 直接串：`POST /e2e/v1/chat/completions` 回 `text/event-stream`，每個 event 是一段密文，閒置時送註解行保活（避開 Cloudflare 100 秒限制）。
   - **游標輪詢（後援）**：閘道把密文分段暫存在記憶體（上限與 TTL 沿用 `RESULT_TTL_SECONDS`），`GET /e2e/v1/requests/{id}?after=<seq>` 長輪詢取回後續分段。quick tunnel 把 SSE 緩衝住時（Cloudflare 官方不保證 quick tunnel 支援 SSE）自動退到這條路，仍然是「一段一段出來」。
5. **用戶端**：SDK 新增 `chat_stream()`（非同步迭代器）；本機代理把解密後的增量轉成標準 OpenAI SSE chunk。應用程式（SYNOX）完全不用改。
6. **洩漏面要講清楚**：真串流會讓閘道與 Cloudflare 看到「大約每 150 ms 出了多少個 256 位元組區塊」，也就是回覆的**大致長度曲線與速度**；假 SSE 只洩漏總大小。所以保留開關（D3）：用戶端可逐筆選擇，worker 可強制關閉。

估計工作量：`protocol.py` +120 行、`crypto.py` +80、worker +250、secure 中繼 +200、client +200、測試 +600。拆成 1 個契約任務 + 3 個可平行任務（F3、G1、G2、G3）。

### 2.5 帳號、金鑰、額度怎麼統一

- **帳號是唯一的主體**（A 的 YAML 帳號）。B 的「單一管理員密碼」併入 A 的 `role: admin` 帳號。
- **API key 屬於帳號**。`api_keys` 表加一欄 `sig_public_key`（可為空）：
  - 空 → 只能用直連面。
  - 有值 → 可用加密面（閘道與 worker 都驗簽）。使用者在網頁申請 key 時貼上 `llmop client keygen` 印出的公鑰；管理員也可以代發（D6）。
- **模型權限**沿用帳號的 `models`；加密面再加「哪幾台 worker 有這個模型」。
- **額度**：同一個帳號、同一本帳（`usage_events`），兩個面都記進去（加密面的 `usage_source` 記為 `worker`，`backend` 記 worker 名稱）。限制項目用 A 的全套。嚴格度見 D5。
- **worker** 仍由管理員建立、發一次性註冊權杖（B 的流程不變），資料表搬進同一個 SQLite。
- **key 前綴**統一成一種，另一種在驗證時仍接受（L3）。

### 2.6 網頁 UI 的設定區

A 的網頁當底，新增 `/ui/settings`（僅 admin）。左側分頁：

| 分頁 | 內容 | 生效方式 |
|---|---|---|
| **1. 部署方案** | 2.2 的方案卡片（附「誰看得到對話」示意圖與優缺點）→ 選了之後帶出需要填的欄位；第一次啟動時就是設定精靈 | 需重啟角色（頁面上有「套用並重新啟動」） |
| **2. 網路與通道** | host / port / `public_url`、tunnel 開關與模式（quick / named）、token（只寫不顯示）、目前網址與狀態、cookie 設定 | tunnel 即時；port 需重啟 |
| **3. 後端與模型** | 後端清單增刪改（類型、URL、併發、逾時、context）、**測試連線**按鈕、模型別名、公開哪些模型 | 即時（熱重載） |
| **4. 帳號與額度** | 帳號增刪改、重設密碼、可用模型、`max_api_keys`、預設額度與個別額度 | 即時 |
| **5. 金鑰** | 所有 key、撤銷、代發、綁定/更換簽章公鑰 | 即時 |
| **6. 加密模式 · 閘道** | worker 清單（線上狀態、模型、公鑰指紋、首次看到的時間）、建立 worker / 重發註冊權杖 / 撤銷；`sync_wait`、`request_timeout`、`max_inflight`、是否允許串流 | 即時 / 部分需重啟 |
| **7. 加密模式 · 本機 worker** | 閘道網址、後端（唯讀顯示 loopback 規則）、公開模型、併發、`pinned_clients`；**顯示本機公鑰與指紋（給用戶端釘選）**、輪替金鑰、刪除舊金鑰、註冊狀態 | 需重啟 worker 角色 |
| **8. 加密模式 · 本機用戶端** | 閘道網址、API key（只寫）、簽章金鑰（產生 / 顯示公鑰）、釘選的 worker 公鑰、代理 port、預設 `max_tokens`、是否用真串流 | 需重啟 client 角色 |
| **9. 進階** | 原始 YAML 編輯器（存檔前驗證、顯示差異）、備份下載 / 還原、設定錯誤訊息、版本與診斷（等同 `llmop check`） | — |

規則：
- 所有寫入走 `settings.py`：驗證失敗不落地；成功則原子寫入並留 `.bak`；回應裡標明每個欄位是「已生效」還是「需重啟」。
- 秘密欄位（密碼雜湊、token、私鑰、API key）**永不回傳**，只能覆寫。
- 分頁依本節點啟用的角色顯示（只當 worker 的機器只看到 1、7、9）。
- 設定區經 tunnel 能不能用，見 D4。
- 加密方案下，閘道上的 playground 停用（閘道讀不到明文）；改由「本機用戶端」節點的頁面提供（D9）。

---

## 3. 合併方法

### 3.1 建議：真正的 `git merge --allow-unrelated-histories`，再在 `dev` 上逐步整合

| 方法 | 評估 |
|---|---|
| **A. 直接 merge 無關歷史（建議）** | 實測只有 5 個檔衝突，20 分鐘內可解完；兩邊 13 個 commit 的歷史與 `git blame` 完整保留；merge commit 有兩個 parent，日後看得出誰來自哪裡 |
| B. subtree / `git read-tree --prefix=theirs/` 後再搬 | 路徑本來就不衝突，放進前綴再搬出來只多一輪大規模 rename，`blame` 反而要多跳一層。沒有好處 |
| C. cherry-pick B 的 6 個 commit | 沒有共同祖先，第一個 commit 就會撞同樣 5 個檔，之後每個 commit 再解一次；commit hash 全變，得不到比 A 多的東西 |
| D. 手工移植、不留 B 的歷史 | 丟掉 B 的歷史與作者資訊，沒有理由 |

### 3.2 指令（只做本地 commit；不碰 main/master；不 push）

```bash
cd /home/user/LocalLLMOutsiderProvider
git status --short                      # 應該只有 docs/plan/MERGE_PLAN.md
git switch -c dev claude/youthful-galileo-u66ybv
git add docs/plan/MERGE_PLAN.md
git commit -m "docs: merge plan for combining direct (A) and E2E (B) implementations"

git branch theirs-b origin/claude/lm-studio-e2e-encryption-ug09c3
git tag pre-merge-a dev
git tag pre-merge-b theirs-b

git merge --allow-unrelated-histories --no-ff --no-commit theirs-b
# 預期輸出：5 個 CONFLICT (add/add)

git checkout --ours  .gitignore README.md pyproject.toml src/llmop/__init__.py
git checkout --theirs src/llmop/__main__.py
mkdir -p docs/secure
git show theirs-b:README.md > docs/secure/README.md
git mv docs/DEPLOY.md docs/PROTOCOL.md docs/SECURITY.md docs/secure/
# 然後手改（見 3.3）：.gitignore、pyproject.toml、src/llmop/__main__.py、tests/e2e/test_docs.py、tests/e2e/test_static_logging.py

.venv/bin/pip install -e ".[dev,secure]"
.venv/bin/python -m pytest -q          # 目標：784 passed
git add -A
git commit -m "merge: bring in E2E gateway/worker/client (B) alongside the direct gateway (A)"
```

之後所有整合工作都是 `dev` 上的一般 commit。回復方法：`git reset --hard pre-merge-a`。

### 3.3 那 5 個衝突與 4 個失敗測試的解法（任務 M0 的內容）

| 檔案 | 解法 |
|---|---|
| `.gitignore` | 兩邊取聯集 |
| `README.md` | 先留 A 的；B 的存成 `docs/secure/README.md`。正式的合併版 README 在 H3 寫 |
| `pyproject.toml` | 以 A 為底，照 2.3 加 `secure` extra、dev 相依、pytest 的 asyncio 設定、`llmop.gateway` 的 `static/*`、entry point 改 `llmop.__main__:main` |
| `src/llmop/__init__.py` | 留 A 的（兩邊都是 `__version__ = "0.1.0"`） |
| `src/llmop/__main__.py` | 寫成分派器：第一個參數是 `gateway` / `worker` / `client` → B 的角色 CLI；否則 → A 的 `llmop.cli.main`。**必須有 `if __name__ == "__main__":` 守衛**（A 現在沒有，B 的測試 `from llmop.__main__ import main` 會被 `sys.exit` 炸掉） |

| 失敗的測試 | 原因 | M0 的處理 |
|---|---|---|
| `tests/test_cli.py::test_python_dash_m_llmop` | `python -m llmop --version` 被 B 的分派器擋掉 | 上面的分派器修好就過 |
| `tests/e2e/test_docs.py`（2 個） | 它讀根目錄 README 找「安全邊界」段落與 `docs/DEPLOY.md` | 路徑改指向 `docs/secure/` |
| `tests/e2e/test_static_logging.py` | 它掃整個 `src/llmop`，A 的 `cli.py:81`、`tunnel.py:136`、`backends/base.py:355` 等 6 處名稱（`text`、`message`）被規則擋下 | 先把掃描範圍限定在 B 的模組（`gateway/ worker/ client/ crypto.py protocol.py paths.py`）。之後 `secure/` 也納入。A 的直連模組本來就處理明文，不適用這條規則 |

---

## 4. 衝突與決策清單（請擁有者決定）

每一項：選項 → **建議** → 後果。任務被哪些決策卡住見第 5 節。

### 甲、產品形態

**D1. 一個閘道能不能同時開「直連」與「加密」兩種服務？**
- A：只能擇一，啟動時由 profile 決定。
- B：可以同時開；加密的 API 改走 `/e2e/v1/*`，直連維持 `/v1/*`。
- **建議 B**。後果：B 的協定文件路徑要改（簽章不包含路徑，安全性不受影響；B 尚無部署，沒有相容問題）。同時開時，走直連的那些請求閘道仍看得到明文，介面上會明確標示。

**D2. 閘道要「真的合成一支」還是「兩支並存、由 profile 決定啟動哪一支」？**
- A：合成一支——把 B 的閘道（約 1,500 行）移植到 A 的 FastAPI app，共用帳號、資料庫、網頁。
- B：並存——加密方案啟動 B 原本的 aiohttp 閘道，網頁、帳號、資料庫各一套。
- **建議 A**。後果：A 多一輪移植與測試改寫（B 的閘道測試約 900 行、e2e 測試工具約 660 行）；B 便宜很多但做不出「一個完整的設定區」，帳號與額度也無法統一。

### 乙、安全

**D3. 加密模式要不要做真串流（2.4）？**
- A：做，預設開啟，用戶端可逐筆關閉、worker 可強制關閉。
- B：不做，維持假 SSE（等整段算完才出字）。
- C：做，但預設關閉。
- **建議 A**。後果：閘道與 Cloudflare 會多看到「回覆的大致長度與出字速度」；內容仍然看不到。約一個階段的工作量。

**D4. 管理與設定頁能不能從外網（經 tunnel）使用？**
- A：完全只限本機（B 現在的預設）。遠端管理要自己用 SSH 轉送。
- B：登入後可從外網**看**管理頁（用量、key、worker 狀態），但**改設定只限本機**。
- C：登入後外網什麼都能做（A 現在的行為）。
- **建議 B**，另給一個設定可切到 A 或 C。後果：設定區能改後端網址、tunnel token、發 worker 註冊權杖，一旦管理員密碼外洩，C 的損害最大。

**D5. 額度超過時要多嚴格？**
- A：全部軟性（A 的作法：事後記帳，單筆可能略超）。
- B：全部硬性預扣（B 的作法：先保留 `max_tokens` 的額度再放行）。
- C：直連維持軟性、加密面維持預扣，但記在同一本帳。
- **建議 C**。後果：不動 A 的行為與 221 個測試，也不回退 B 修過的兩個繞過漏洞；代價是兩個面的行為略有不同，文件要寫清楚。選 B 要改 A 的 `quota.py` 與串流結算，風險較高。

**D6. 請求的前向保密（B 的 SECURITY 檢討 1 明寫「需擁有者決定」）要現在做嗎？**
（白話：現在若有人先錄下密文、日後又偷到 worker 私鑰，就能解開舊請求。修法是 worker 定期發「短期金鑰」。）
- A：這一版不做，維持「定期輪替金鑰 + 刪除舊私鑰」，並在設定區加上輪替提醒。
- B：這一版就做（協定再多一層，約多一個階段）。
- **建議 A**。

### 丙、帳號與金鑰

**D7. 加密模式的 API key 由誰發？**
- A：使用者登入網頁後自己申請，並貼上自己的簽章公鑰；管理員也能代發。
- B：只有管理員能發（B 現在的作法）。
- **建議 A**，受帳號的 `max_api_keys` 限制。後果：使用者不必把公鑰另外傳給管理員；多一點網頁工作。

### 丁、設定、儲存、介面

**D8. 設定檔以誰為準、什麼格式？**
- A：一份 `config.yaml` 管這台機器的全部角色（worker / client 的設定也搬進來，改成 YAML）；網頁寫的就是這個檔，寫入前留 `.bak`。金鑰與狀態檔仍是程式自己寫的 JSON。
- B：維持兩套——A 的 YAML 加上 B 的每角色 JSON。
- C：設定改存資料庫，YAML 只在第一次啟動時讀。
- **建議 A**。後果：網頁改寫 YAML 時，被改到的那一段的註解可能消失（會盡量用 A 現有的「保留註解」工具）；B 的 `config.json` 提供一次性的匯入指令。

**D9. 資料與金鑰放在哪裡？**
- A：跟著程式資料夾（`./config.yaml`、`./data/`，worker / client 的金鑰在 `./data/worker/`、`./data/client/`）；可用 `LLMOP_HOME` 改。
- B：一律放使用者家目錄 `~/.llmop/`。
- C：免安裝版用 A，`pip` 安裝用 B。
- **建議 A**。後果：整個資料夾搬走就能換機器、好備份；但**把資料夾壓縮給別人就等於把私鑰給人**，README 與啟動器要有警告。

**D10. 設定區的範圍？**
- A：只在閘道的網頁裡；worker 與用戶端仍用設定檔與指令。
- B：每台機器都有同一套「本機控制台」（只綁 `127.0.0.1`），依角色顯示分頁（2.6 的 7、8）。
- **建議 B**。後果：worker 機器會多開一個只限本機的 port——B 原本強調「worker 不開任何 port」；保留 `llmop worker run` 純指令模式給在意這點的人。另外，加密方案下閘道的 playground 停用，改在用戶端的本機控制台提供。

### 戊、技術棧

**D11. HTTP 函式庫怎麼收斂？**
- A：閘道用 FastAPI（A）；worker 與 client 這一版先留在 aiohttp（B），都放在 `secure` extra。
- B：全部改成 FastAPI / httpx / websockets（相依最少，但要重寫 B 約 3,800 行測試的測試工具）。
- **建議 A**，B 列為之後的清理項目。後果：加密功能會多裝 aiohttp 一家（免安裝版 zip 約多 10 MB）。

**D12. worker 用哪一套後端轉接？**
- A：改用 A 的 `llmop.backends`（Ollama / vLLM / LM Studio / llama.cpp / OpenAI 相容，已支援串流），外加 B 的「只准 loopback」規則。
- B：留 B 自己的（Ollama 走原生 `/api/chat` 轉譯），另外幫它補串流。
- **建議 A**。後果：只維護一套；B 的 Ollama 原生轉譯（228 行，處理圖片與工具呼叫的格式）先不用，需要時可做成一種額外的後端類型。Ollama 的 `/v1` 有已知限制（不吃每筆請求的 `num_ctx`），A 的文件已說明。

**D13. SYNOX 怎麼接？**（查證：SYNOX 的 `local` 後端**不送** `Authorization` 標頭，`SYNOX/src/synox/llm/local.py:306`，所以連不上需要 API key 的直連閘道）
- A：一律經過 `llmop client proxy`；代理新增「直連上游」模式（不加密，只替你帶 API key）。SYNOX 永遠填 `http://127.0.0.1:8788`，換方案不用改 SYNOX。
- B：直連時 SYNOX 改用它的 `openai_compat` 後端直接帶 key；加密時才用代理。
- **建議 A**（B 仍然可行，文件一併寫）。後果：用戶端機器一定要裝 llmop（免安裝版即可）。

### 己、流程

**D14. 合併方法**：**建議 3.1 的 A（直接 merge 無關歷史到新的本地分支 `dev`）**。其他選項見 3.1。

### 低風險的不一致（附預設值；沒意見就照預設）

| # | 項目 | A | B | 預設 |
|---|---|---|---|---|
| L1 | 發行名稱 | `localllm-outsider-provider` | `llmop` | 留 A 的（指令名與 import 名都是 `llmop`，不受影響） |
| L2 | 最低 Python | 3.10 | 3.11 | **3.11**（免安裝版本來就內含 3.12；CI 改 3.11 / 3.12 / 3.13） |
| L3 | API key 前綴 | `sk-llop-` | `llmop_sk_` | 新發的一律 `sk-llop-`；用戶端設定檢查兩種都接受 |
| L4 | 閘道 port | 8780 | 8787 | 8780；用戶端代理維持 8788 |
| L5 | 每日額度的日界線 | 當地時間 | UTC | 當地時間 |
| L6 | 健康檢查路徑 | `/health` | `/healthz` | 兩個都留 |
| L7 | 管理員身分 | YAML 的 admin 帳號 | 資料庫裡一把密碼 | A；`llmop gateway init` 變成 `llmop init` 的別名 |
| L8 | 登入 session | 存 SQLite，重啟還在 | 只在記憶體 | A |
| L9 | 登入防暴力破解 | `web/throttle.py` | 同來源 15 分鐘 5 次 | A |
| L10 | 用量紀錄 | 每筆一列 | 每日彙總 | A（加密面寫進同一張表） |
| L11 | 錯誤格式 | OpenAI 樣式 | 協定自己的錯誤碼 | 各面維持自己的；用戶端代理對應用程式一律轉成 OpenAI 樣式 |
| L12 | CLI 形狀 | 平的 | 分角色 | 兩者並存（2.3） |
| L13 | 訊息語言 | CLI 英文、網頁繁中 | 全繁中 | 各模組維持現狀；新寫的網頁文字一律繁中 |
| L14 | 文件 | 根目錄 README + `docs/` | README + 3 份 | 一份合併的 README；B 的三份放 `docs/secure/` |
| L15 | 版本與授權 | 0.1.0，MIT | 0.1.0，無 LICENSE | 合併後 **0.2.0**，MIT |
| L16 | 「不可記錄內容」的靜態測試 | 無 | 掃全部原始碼 | 只適用加密相關模組 |
| L17 | 請求本文上限 | UI 1 MB | 16 MiB | 各面維持自己的 |

---

## 5. 給 Opus 執行者的任務拆分

通則（每個任務都適用）：
- 分支 `dev`，只做本地 commit，不 push、不動 main/master。只能改 `/home/user/LocalLLMOutsiderProvider`。
- **只改「擁有的檔案」欄列出的路徑**；需要別人檔案的變更，寫進交付報告的「需要整合者處理」。
- 環境：`.venv/bin/pip install -e ".[dev,secure]"`；交付前跑 `.venv/bin/python -m pytest -q`，全綠才算完成。
- A 原有的 221 個測試**不得刪改斷言**（只有任務明列的例外）。B 的測試可以因介面變更而改寫，但檢查的性質不能變弱；改了哪些要列出來。
- 寫入秘密檔一律經 `llmop.paths.write_json(secret=True)`；加密相關模組遵守 SECURITY 檢討 13（不記錄內容與秘密）。

### 階段 0 — 合併（一個執行者，依序）

| ID | 目標 | 擁有的檔案 | 相依 | 卡在 | 驗收 |
|---|---|---|---|---|---|
| **M0** | 照 3.2、3.3 建立 `dev`、合併、解 5 個衝突、修 4 個測試 | 3.3 列的檔案；`docs/secure/**`（搬移） | — | D14 | `pytest -q` → **784 passed**；`python -m llmop --version`、`llmop serve --help`、`llmop worker --help`、`llmop client --help` 都正常；`git log --graph` 看得到兩條歷史 |

**⏸ 等待點 W0**：dispatcher 確認 784 全過，且 D1–D13 已有答覆，才進階段 1。

### 階段 1 — 地基（4 個任務可平行）

| ID | 目標 | 擁有的檔案 | 相依 | 卡在 | 驗收 |
|---|---|---|---|---|---|
| **F1** | 設定模型：`profile`、`gateway.planes`、`secure`、`worker`、`client` 區段與驗證；各方案的預設值；把 cli.py 裡「保留註解改 YAML」的函式搬到 `configedit.py`（cli.py 只改 import）；`settings.py` 的寫入服務（驗證 → 原子寫入 → `.bak` → 回報生效方式） | `src/llmop/config.py`、`configedit.py`、`settings.py`、`examples/config.example.yaml`、`config.example.yaml`、`tests/test_profile.py`、`tests/test_settings_service.py` | M0 | D1 D8 D9 | 沒有 `profile` 的舊 `config.yaml` 仍然載入並等同 `direct`；每個方案有載入測試；寫入失敗不留半個檔；A 的測試全過 |
| **F2** | 資料庫統一：`api_keys` 加 `sig_public_key`；新增 `workers / worker_keys / worker_models`（搬 B 的 `gateway/store.py` 對應方法）；舊資料庫自動升級；`quota.py` 新增預扣 / 結算 / 釋放介面（依 D5） | `src/llmop/store.py`、`quota.py`、`security.py`、`tests/test_store_secure.py`、`tests/test_quota_reserve.py` | M0 | D5 D7 | 用 A v0.1 的資料庫檔開啟後自動升級且資料不變；B 的「兩個額度繞過」情境（`tests/e2e/test_e2e_quota_defects.py` 的邏輯）在新介面上有對應測試 |
| **F3** | 協定 v2 與串流密碼：標頭 `stream` 欄位；`chunk` / `cancel` 訊框、`hello.features`；`crypto.py` 的分段封裝與開啟（`seq`、`final` 綁進 `info`）；路徑常數（依 D1）；更新 `PROTOCOL.md`、`SECURITY.md`（新增「串流的洩漏面」一節） | `src/llmop/protocol.py`、`crypto.py`、`docs/secure/PROTOCOL.md`、`docs/secure/SECURITY.md`、`tests/test_protocol.py`、`tests/test_crypto.py` | M0 | D1 D3 | 測試涵蓋：分段亂序、重複、缺 `final`、跨請求調換、`info` 不符，全部開啟失敗；與 `pyhpke` 的交叉驗證仍通過。D3 選 B 時，本任務只做路徑與版本，不做串流 |
| **F4** | 路徑與 Windows：`paths.py` 的 `os.fchmod` 在 Windows 上的替代作法（沒有就略過並改用建立時的權限）；`home()` 依 D9 決定預設位置；B 的 `config.json` 匯入工具 | `src/llmop/paths.py`、`src/llmop/migrate.py`、`tests/test_paths.py`、`tests/test_migrate.py` | M0 | D8 D9 | 用 monkeypatch 拿掉 `os.fchmod` 後 `write_json(secret=True)` 仍成功；`LLMOP_HOME` 仍然優先 |

**⏸ 等待點 W1**：四個任務都合進 `dev`、全套測試通過。dispatcher 把 F1–F3 產出的介面（設定 dataclass、`Store` 新方法、`protocol` 新訊框）貼進階段 2 每個任務的說明。

### 階段 2 — 功能（5 個任務可平行）

| ID | 目標 | 擁有的檔案 | 相依 | 卡在 | 驗收 |
|---|---|---|---|---|---|
| **G1** | 加密中繼移植到 FastAPI：`/e2e/v1/chat/completions`（同步 80 秒 → 202 → 輪詢；串流時回 SSE 並支援 `?after=seq` 游標輪詢）、`/e2e/v1/requests/{id}`、`/e2e/v1/models`、`/worker/ws`（握手、`chunk` 轉送、`cancel`）；接帳號、key、額度；依 `gateway.planes` 掛載或不掛載兩個面 | `src/llmop/secure/**`、`src/llmop/app.py`、`src/llmop/auth.py`、`src/llmop/api/__init__.py`、`tests/secure/**`（由 `tests/gateway/test_api.py`、`test_worker_ws.py`、`test_logging.py` 移植） | F1 F2 F3 | D1 D2 D5 | B 閘道測試的每個情境都有對應測試並通過；只開 `secure` 時 `/v1/chat/completions` 回明確錯誤；只開 `direct` 時 `/e2e/*` 與 `/worker/ws` 是 404；A 的 `tests/test_api.py` 原樣通過 |
| **G2** | worker：後端改用 `llmop.backends`（依 D12）並保留 loopback 規則；串流分批封裝（150 ms、256 位元組填充、可設定）；處理 `cancel`；設定從 `config.yaml` 的 `worker` 區段讀；`features` 公告 | `src/llmop/worker/**`、`tests/worker/**` | F1 F3 F4 | D3 D11 D12 | 既有 worker 測試（重放、金鑰、連線重試、握手）通過；新增串流測試：增量順序正確、`final` 帶 usage、後端中途失敗時送出**封裝過的**錯誤、收到 `cancel` 後後端連線被關閉 |
| **G3** | 用戶端：SDK `chat_stream()`；代理輸出真的 OpenAI SSE；SSE 不通時自動退到游標輪詢；「直連上游」模式（依 D13）；設定從 `config.yaml` 的 `client` 區段讀；key 前綴兩種都接受 | `src/llmop/client/**`、`tests/client/**` | F1 F3 F4 | D3 D13 | 既有用戶端測試通過；新增：串流逐段輸出、亂序或缺 `final` 時報錯而不是靜默截斷、退回輪詢、直連上游會帶 `Authorization` |
| **G4** | 網頁：`/ui/settings` 九個分頁（2.6）與對應的 `/ui/api/settings/*`；把 B 管理介面的 worker 管理與簽章公鑰綁定搬進來；申請 key 時可貼簽章公鑰；外網存取規則（D4）；加密方案下停用閘道 playground | `src/llmop/web/**`、`tests/test_web.py`（只能新增）、`tests/test_settings_web.py` | F1 F2 | D4 D7 D10 | 非 admin 與未登入一律被拒；所有寫入要 CSRF；秘密欄位不出現在任何回應（有測試逐一檢查）；外網請求依 D4 被擋；A 的 45 個 web 測試原樣通過；頁面不依賴 CDN、沒有 CSP 錯誤 |
| **G5** | 節點與 CLI：`node.py` 依 `profile.roles` 在一個 process 啟動 gateway / worker / client / tunnel，可個別重啟；`llmop init --profile`、`llmop serve`、`llmop gateway …` 別名、`llmop worker …`、`llmop client …`、`llmop check` 涵蓋三個角色；沒裝 `secure` 時的提示 | `src/llmop/cli.py`、`src/llmop/__main__.py`、`src/llmop/node.py`、`src/llmop/tunnel.py`、`tests/test_cli.py`（只能新增）、`tests/test_cli_packaging.py`、`tests/test_node.py`、`tests/test_tunnel.py` | F1 F4 | D8 D9 D10 | A 的 CLI 測試原樣通過；B 的 `tests/worker/test_worker_cli.py`、`tests/client/test_client_cli.py`、`tests/gateway/test_config_cli.py` 的情境仍成立（由 G2 / G3 配合調整）；`llmop serve` 在 `secure-allinone` 方案下一個指令就把閘道與 worker 都帶起來 |

G4 需要「重啟某個角色」的動作時，呼叫 `AppState` 上的一個回呼；回呼由 G5 的 `node.py` 提供，沒有 node 時（測試）回報「需手動重啟」。這個介面由 dispatcher 在 W1 時定下來。

**⏸ 等待點 W2**：五個任務都合進 `dev`、全套測試通過。

### 階段 3 — 端到端、打包、文件（3 個任務可平行）

| ID | 目標 | 擁有的檔案 | 相依 | 卡在 | 驗收 |
|---|---|---|---|---|---|
| **H1** | e2e：把 `tests/e2e/harness.py` 改成啟動統一後的閘道（uvicorn 跑在 loopback）+ 真的 worker + 真的代理 + 假後端；保留並擴充「錄下閘道所有位元組、確認沒有對話內容」的測試到**串流**情境；新增方案矩陣（`direct`、`secure-allinone`、`secure-gateway` + `secure-worker`、兩面同開） | `tests/e2e/**`（`test_docs.py` 除外） | G1 G2 G3 G5 | — | B 原有的 e2e 情境（流程、安全、額度漏洞）全部通過；串流情境下閘道錄到的位元組不含任何提示或回覆文字；直連方案的串流仍是真串流 |
| **H2** | 打包與營運：`pyproject.toml` 定稿；免安裝版納入 `secure` 的 Windows wheel；新增 `.bat`（worker、用戶端代理、方案選擇）；Docker compose 的加密方案範例；CI 矩陣（ubuntu + windows，3.11 / 3.12 / 3.13，含 `secure`）；release workflow | `pyproject.toml`、`scripts/**`、`docker/**`、`.github/**`、`.gitignore`、`.dockerignore` | G5 | D11 | `python -m build` 產出 wheel；乾淨 venv 裝 wheel（不含 extra）可跑直連方案；裝 `[secure]` 可跑加密方案；`scripts/build_portable.py` 產出 zip 且內含 `cryptography`、`aiohttp` |
| **H3** | 文件：合併的 `README.md`（先講怎麼選方案）；更新 `HANDOFF.md`、`DEPLOYMENT_GUIDE.html`；`docs/secure/` 三份對齊新路徑與串流；SYNOX 接法（D13）；`tests/e2e/test_docs.py` 對齊 | `README.md`、`docs/**`（`docs/plan/` 除外）、`tests/e2e/test_docs.py` | G1–G5 | 全部 | 「安全邊界」段落在 README 與 `SECURITY.md` 一字不差（沿用 B 的測試）；文件中的每個指令都存在（沿用 B 的 `--help` 檢查） |

**⏸ 等待點 W3**。

### 階段 4 — 整合（一個執行者）

| ID | 目標 | 擁有的檔案 | 驗收 |
|---|---|---|---|
| **I1** | 刪除已被取代的 `src/llmop/gateway/`（B 的原始閘道）與其測試；處理各任務「需要整合者處理」的項目；版本改 0.2.0；實機冒煙測試三條路徑（都接假的 Ollama）：① 直連 + 串流 ② `secure-allinone` + 串流 ③ 閘道與 worker 分兩個 process；用瀏覽器走一遍設定精靈與九個分頁；build wheel 與免安裝版；更新 `HANDOFF.md` 的「驗證過 / 沒驗證過」 | 全部（此時沒有其他執行者） | 全套測試通過（預期 800 個以上）；三條冒煙路徑的指令與輸出寫進交付報告；`git status` 乾淨 |

**⏸ 最終等待點**：交給擁有者在 Windows 主機上實測（真的 Ollama、真的 cloudflared、SYNOX）。

### 5.1 決策 → 任務對照

| 決策 | 卡住的任務 | 若選非建議選項，計劃怎麼變 |
|---|---|---|
| D1 | F1 F3 G1 | 選 A：路徑不改，G1 改成啟動時二選一，H1 少一種矩陣 |
| D2 | G1 G4 H1 | 選 B：G1 縮成「由 node 啟動 B 的 aiohttp 閘道」；F2 不搬 worker 表；G4 的分頁 5、6 改成連到 B 的 `/admin/`；帳號與額度**不統一** |
| D3 | F3 G1 G2 G3 H1 | 選 B：四個任務的串流部分全部拿掉，約省四分之一工作量 |
| D4 | G4 | 只影響一個存取檢查與預設值 |
| D5 | F2 G1 | 選 B：F2 要改 A 的結算流程，A 的額度測試需擁有者同意後調整 |
| D6 | — | 選 B：W3 之後另開一個階段（協定 v3） |
| D7 | F2 G4 | 選 B：G4 拿掉使用者自助綁定公鑰 |
| D8 D9 | F1 F4 G5 | 選 B：F4 不做匯入；G4 的分頁 7、8 要分別讀寫 JSON |
| D10 | G4 G5 | 選 A：G4 拿掉分頁 7、8；G5 的 node 不必在 worker 機器帶網頁 |
| D11 | G2 G3 H2 | 選 B：G2、G3 各多一輪改寫，H1 的測試工具重寫 |
| D12 | G2 | 選 B：G2 要在 B 的兩個後端各自實作串流 |
| D13 | G3 H3 | 選 B：G3 不做直連上游 |

### 5.2 測試策略

1. **基準線**：M0 之後 784 個（A 221 + B 563）。之後每個任務的交付報告都要寫「之前 N 個、之後 M 個、改寫了哪幾個、為什麼」。
2. **A 的 221 個是不可動的回歸網**：直連方案的行為由它們定義。只允許新增。
3. **B 的測試分三類處理**：
   - 原樣保留：`test_protocol.py`、`test_crypto.py`、`test_paths.py`、worker 的重放與金鑰、用戶端設定與 SDK（只隨 F3 擴充）。
   - 移植：`tests/gateway/*` → `tests/secure/*`（aiohttp 測試客戶端換成 FastAPI 的）。**逐一對照，不可少情境**；G1 的交付要附對照表。
   - 改寫測試工具：`tests/e2e/harness.py`（H1）。
4. **安全不變量**（H1 負責，任何階段都不能弱化）：閘道收送的位元組不含對話內容；簽章錯、釘選不符、重放、過期，一律拒絕；額度不能靠並發或取消繞過。
5. **跨平台**：CI 加 windows；`os.fchmod`、檔案權限、路徑分隔、主控台編碼（A 已有 `_safe_console`）各有測試。
6. **同一個 event loop 的共存**：`node.py` 把 uvicorn、aiohttp（worker / client）與 tunnel 的子行程放在一起，G5 要有「啟動 → 處理一筆請求 → 乾淨關閉」的測試。

---

## 6. 無法驗證或尚未驗證的事

- **B 在 Windows 上的行為**：沒有 Windows 環境。`os.fchmod` 在 Windows 要 Python 3.13 才有，是依 Python 文件判斷，沒有實跑；0600 權限在 Windows 上實際沒有保護效果，私鑰檔的保護要另外想（F4 先讓它能跑，是否加 ACL 留待之後）。
- **真的 Ollama、真的 cloudflared、官方 `openai` SDK、SYNOX 實際連線**：兩邊都只用假後端測過（A 的 HANDOFF §4 也這樣寫）。
- **quick tunnel 對 SSE 與 WebSocket 的實際表現**：Cloudflare 官方不保證 quick tunnel 支援 SSE；`/worker/ws` 經 tunnel 的穩定度、加密串流是否會被緩衝，都要實測。2.4 的游標輪詢就是為這個風險準備的。
- **`cryptography` 47–49 版的 HPKE**：B 的 SECURITY.md 說只在 50.0.1 確認過；我跑測試用的是 50.0.2。
- **SYNOX 的 `openai_compat` 後端是否支援自訂 `base_url` + API key**：只確認了 `local` 後端不送 `Authorization`，沒有細讀 `openai_compat`（影響 D13 的 B 選項）。
- **uvicorn 的 WebSocket 與 aiohttp 在同一個 process 共存**：理論上沒問題（同一個 asyncio loop），尚未實作驗證（G5）。
- **GitHub**：push 仍被擋（403），本計劃全部假設只做本地 commit。
- 2.4 的工作量是估計值。
