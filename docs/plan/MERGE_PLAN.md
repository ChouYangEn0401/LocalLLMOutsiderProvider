# 合併計劃書 — 把「直連版（A）」與「端對端加密版（B）」合成一個產品

> 狀態：**草稿第 4 版**（2026-11-19）。已定案：**D1、D3、D4、D5、D7、D8、D10（簡化版）、D11（授權規劃者決定）、D12、D13（方向）、D14、D16、D19（重新界定）**。待決：**D2、D6、D9、D15、D20–D26**。最新內容在 **第 9 節**；各節不一致時以編號較大的節為準（9 > 8 > 其餘）。等級與用量的完整規格在 `docs/plan/TIERS_AND_USAGE_RULES.md`。決策板 v3：`docs/MERGE_DECISIONS.html`。**本文件只增不刪：被取代的內容保留並標示。全部決策定案後才派工，M0 尚未開始。**
>
> （上一版狀態列，保留備查） 狀態：**草稿第 3 版**。已定案：**D1、D3、D4、D5、D7、D8、D14**。待決：D2、D6、D9–D13、D15–D24。第 3 版的新需求與任務重排在 **第 8 節**；第 8 節與前面章節不一致時，**以第 8 節為準**。圖解見 `docs/MERGE_DECISIONS.html`（決策板 v2）；v1 凍結於 `docs/BU_Keep_MERGE_DECISIONS_v1_legacy.html`，不得更動。**全部決策定案後才派工，M0 尚未開始。**
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

### 2.7 外網唯讀、本機才能改（D4 定案）

**兩個監聽面**（D15 建議 A；若選 B 則同一個 port 上用下面的 `is_local()` 區分）：

| 面 | 位址 | 誰連得到 | 有哪些路由 |
|---|---|---|---|
| 公開面 | `127.0.0.1:8780`，tunnel **只**指向它 | 網路上任何人 | `/v1/*`、`/e2e/v1/*`、`/worker/ws`、登入、使用者自己的頁面（依 D16）、管理與設定的 **GET**（唯讀、已遮蔽） |
| 本機控制台 | `127.0.0.1:8781` | 只有這台電腦 | 上面全部，加上所有寫入與操作端點 |

規則（任務 S1 做成 `src/llmop/netguard.py`，全專案只有這一個判斷點）：

1. **`is_local(request)` 必須同時成立**：
   - TCP 對端是 loopback（看原始對端，不看被 `proxy_headers` 改寫過的值；本機控制台那個 uvicorn **關閉** `proxy_headers`）。
   - 沒有任何轉送標頭：`cf-connecting-ip`、`cf-ray`、`cf-visitor`、`cdn-loop`、`x-forwarded-for`、`x-forwarded-host`、`x-forwarded-proto`、`x-real-ip`、`forwarded`、`via`。
   - `Host` 標頭在白名單：`127.0.0.1:<port>`、`localhost:<port>`、`[::1]:<port>`（擋 DNS rebinding）。
   - 有 `Origin` 時必須是同一個本機來源；`Sec-Fetch-Site` 不可為 `cross-site`。
2. **公開面的路由表是白名單**：`create_app(surface="public")` 不註冊任何寫入路由。有一個測試列舉公開面全部路由，確認管理 / 設定類只有 `GET`，而且對每一條送 `POST / PUT / PATCH / DELETE` 都得到 404 或 405。
3. **公開面也檢查 `Host`**：只接受 `public_url` 的主機名、目前 tunnel 的主機名、或 loopback；其他一律 421。
4. **登入階段記錄來源**：`sessions` 加一欄 `origin = local | remote`。`remote` 的 session 即使被拿到本機控制台用，也不能寫（要在本機重新登入）；`local` 的 cookie 不會被送到 tunnel 網域（主機名不同）。
5. **唯讀也要遮蔽**：外網看到的管理頁不含任何秘密、後端的內部網址、設定檔路徑、原始 YAML、使用者的來源 IP。「進階」分頁（原始 YAML、備份）在公開面不存在。
6. **tunnel 管理器拒絕指向本機控制台的 port**，也拒絕指向任何後端 port（`tunnel.py` 啟動前檢查）。
7. **唯讀是否連看都不給**：設定 `server.remote_admin: readonly | off`（預設 `readonly`，依擁有者決定）。
8. 已知限制寫進文件：同一台電腦上的其他程式或其他 Windows 帳號算「本機」（檢查表 6b、26）。

### 2.8 優先佇列與 VIP（D5 定案）

> **〔2026-11-19 已被取代〕** 三級（vip／normal／low）的設計由 `TIERS_AND_USAGE_RULES.md` 取代。原因：擁有者改為六個等級與兩個用量表。以下保留備查。

**等級**（`accounts.<名稱>.tier`，預設 `normal`）：

| 等級 | 額度 | 出隊順序 | 用途 |
|---|---|---|---|
| `vip` | **不檢查** token / 請求次數額度（用量照記）；同時請求上限見 D19 | 最先 | 擁有者自己、SYNOX |
| `normal` | 照帳號額度 | 其次 | 朋友 |
| `low` | 照帳號額度 | 最後 | 管理員指定的帳號；或額度用完後自動降級（D17） |

**佇列在哪裡**：每個「GPU 資源」一個排程器，取代現在單純的 `asyncio.Semaphore`（`backends/base.py:161`，先到先得、無上限、無逾時）。
- 直連面：每個後端一個，名額 = `max_concurrency`。
- 加密面：閘道的 hub 對每台 worker 一個，名額 = worker 公告的 `max_concurrency`。閘道有空位才把密文送給 worker，取代 B 現在的「`8 × max_concurrency` 先塞過去」。等級只是閘道已經知道的帳號屬性，不增加任何洩漏。
- 同一台機器同時開直連與本機 worker（`custom`）時，node 讓兩者共用同一個排程器，避免兩邊各自以為 GPU 有空。

**出隊規則**：
1. 等級高的先（`vip` > `normal` > `low`）。不會中斷正在跑的請求。
2. 同等級內**按使用者輪流**：挑「目前執行中 + 最近剛被服務」最少的使用者；同一個使用者內先到先得。一個人連送 10 筆不會擋住只送 1 筆的人。
3. 防餓死：見 D18。

**上限與滿了怎麼辦**（`config.yaml` 的 `queue:` 區段，都可調）：

| 設定 | 預設 | 超過時 |
|---|---|---|
| `max_length`（每個資源排隊總數） | 32 | **503** `queue_full` + `Retry-After`（依排隊數 × 近期平均耗時估算） |
| `max_per_user`（每人排隊數；VIP 另計） | 4 | **429** `queue_limit` + `Retry-After` |
| `max_wait_seconds`：串流 / 加密面 | 600 | 503 `queue_timeout` + `Retry-After` |
| `max_wait_seconds`：直連面非串流 | **60** | 同上。原因：Cloudflare 約 100 秒沒回應就切斷（524），不能排太久 |

OpenAI SDK 對 429 與 503 都會依 `Retry-After` 自動重試，用戶端不用改。VIP 不受 `max_length` 影響（但受 D19 的同時上限）。

**串流與長請求**：
- 直連面串流：先回 SSE 標頭，排隊期間每 10 秒送一行註解（`: queued 3`）保活；排到才開始出字。排隊逾時則送 SSE 錯誤事件。
- 加密面：非串流沿用 202 + 輪詢，`pending` 回應多帶 `queue_position`；串流同樣送保活。
- 用戶端在排隊中斷線：立刻從佇列移除，不佔名額、不記用量。

**事後記帳**：請求結束（成功、失敗、取消）才寫一筆 `usage_events`，多記 `tier` 與 `queue_ms`。准入時只看「目前已記的用量是否已超過額度」（A 現行作法），不預扣。

**管理**：YAML 的 `accounts.<名稱>.tier` 與 `queue:`；設定區「帳號與額度」分頁加等級下拉選單、「概觀」加即時佇列面板（每個資源：執行中 / 各等級排隊數 / 平均等待）。改等級是寫入，只能在本機做（D4）。

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

**D1.【已定案＝B，細節見 8.1】一個閘道能不能同時開「直連」與「加密」兩種服務？**
- A：只能擇一，啟動時由 profile 決定。
- B：可以同時開；加密的 API 改走 `/e2e/v1/*`，直連維持 `/v1/*`。
- **建議 B**。後果：B 的協定文件路徑要改（簽章不包含路徑，安全性不受影響；B 尚無部署，沒有相容問題）。同時開時，走直連的那些請求閘道仍看得到明文，介面上會明確標示。

**D2.【待決；新需求下的重新分析見 8.6，建議仍為 A】閘道要「真的合成一支」還是「兩支並存、由 profile 決定啟動哪一支」？**
- A：合成一支——把 B 的閘道（約 1,500 行）移植到 A 的 FastAPI app，共用帳號、資料庫、網頁。
- B：並存——加密方案啟動 B 原本的 aiohttp 閘道，網頁、帳號、資料庫各一套。
- **建議 A**。後果：A 多一輪移植與測試改寫（B 的閘道測試約 900 行、e2e 測試工具約 660 行）；B 便宜很多但做不出「一個完整的設定區」，帳號與額度也無法統一。

### 乙、安全

**D3.【已定案＝A，預設值依入口，見 8.2】加密模式要不要做真串流（2.4）？**
- A：做，預設開啟，用戶端可逐筆關閉、worker 可強制關閉。
- B：不做，維持假 SSE（等整段算完才出字）。
- C：做，但預設關閉。
- **建議 A**。後果：閘道與 Cloudflare 會多看到「回覆的大致長度與出字速度」；內容仍然看不到。約一個階段的工作量。

**D4.【已定案】管理與設定頁從外網只能看，不能改。**
- 所有「寫入」與「操作」（改設定、帳號、額度、等級、後端、tunnel、發 worker 註冊權杖、輪替金鑰、測試連線、重啟角色、撤銷別人的 key、登出別人…）**只能在本機（`127.0.0.1` / `localhost`）做**。
- 從外網連進來時，管理與設定頁**嚴格唯讀**：不是「按鈕變灰」，而是那些會改變狀態的端點在外網那一面**根本不存在**。
- 設計見 **2.7**；逐項防護見 **第 6 節資安檢查表**。衍生的新決策：D15、D16。

**D5.【已定案】用量一律「事後記帳」（軟性）；搶 GPU 的問題用「優先佇列」解決；另設不受額度限制的 VIP。**
- 兩個服務面（直連、加密）都不再預扣；B 的 `reserved_tokens` 預扣機制移除。
- 請求不再互搶：進佇列排隊，依等級與公平規則出隊。
- 等級 `vip / normal / low`；VIP 不受額度限制且優先出隊。
- 設計見 **2.8**。衍生的新決策：D17、D18、D19。
- 後果（請知悉）：軟性記帳下，一個帳號最多可能超出額度「同時請求數 × 單次 `max_tokens`」；B 原本用預扣堵住的兩個繞過情境改由「同時請求上限 + 佇列」限制超出幅度，`tests/e2e/test_e2e_quota_defects.py` 要改寫成「超出幅度有上界」。

**D6.【待決；已拆成 (a) 換鑰延續性 與 (b) 前向保密，見 8.5】請求的前向保密（B 的 SECURITY 檢討 1 明寫「需擁有者決定」）要現在做嗎？**
（白話：現在若有人先錄下密文、日後又偷到 worker 私鑰，就能解開舊請求。修法是 worker 定期發「短期金鑰」。）
- A：這一版不做，維持「定期輪替金鑰 + 刪除舊私鑰」，並在設定區加上輪替提醒。
- B：這一版就做（協定再多一層，約多一個階段）。
- **建議 A**。

### 丙、帳號與金鑰

**D7.【已定案＝A】加密模式的 API key 由誰發？**
- A：使用者登入網頁後自己申請，並貼上自己的簽章公鑰；管理員也能代發。
- B：只有管理員能發（B 現在的作法）。
- **建議 A**，受帳號的 `max_api_keys` 限制。後果：使用者不必把公鑰另外傳給管理員；多一點網頁工作。

### 丁、設定、儲存、介面

**D8.【已定案＝A】設定檔以誰為準、什麼格式？**
- A：一份 `config.yaml` 管這台機器的全部角色（worker / client 的設定也搬進來，改成 YAML）；網頁寫的就是這個檔，寫入前留 `.bak`。金鑰與狀態檔仍是程式自己寫的 JSON。
- B：維持兩套——A 的 YAML 加上 B 的每角色 JSON。
- C：設定改存資料庫，YAML 只在第一次啟動時讀。
- **建議 A**。後果：網頁改寫 YAML 時，被改到的那一段的註解可能消失（會盡量用 A 現有的「保留註解」工具）；B 的 `config.json` 提供一次性的匯入指令。

**D9.【待決；擁有者下一階段再定，暫列 A】資料與金鑰放在哪裡？**
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
- **建議改為 B（待決；第 1 版建議 A）**。理由：擁有者偏好 FastAPI；G2、G3 這一版本來就要大改（串流、設定改 YAML），順便換底層比分兩次便宜。B 選 aiohttp 的原因是 worker 需要 WebSocket 用戶端（FastAPI 沒有），aiohttp 一個套件含伺服器與用戶端；改用 FastAPI + uvicorn + httpx + `websockets` 完全可行。成本：`worker/link.py`（233 行）、`client/sdk.py` + `proxy.py`（約 630 行）改寫，約 2,900 行用到 aiohttp 的測試與測試工具重寫（情境不可少），G2 / G3 / H1 工作量約多三到四成。走法：先閘道（D2）→ worker 後端改用 A 的（D12）→ 最後才換 worker 連線與用戶端。選 B 後 `secure` extra 為 `cryptography`、`websockets`。

**D12. worker 用哪一套後端轉接？**
- A：改用 A 的 `llmop.backends`（Ollama / vLLM / LM Studio / llama.cpp / OpenAI 相容，已支援串流），外加 B 的「只准 loopback」規則。
- B：留 B 自己的（Ollama 走原生 `/api/chat` 轉譯），另外幫它補串流。
- C：A 的 adapter + 預設只准 loopback + 可在本機控制台明確開啟「允許區網後端」（只准私有網段、紅色警告）。
- **建議 A（待決）**；有人需要「LLM 在區網另一台」時再加 C。只准 loopback 的理由：worker 手上是已解密的明文，用未加密的 http 送到別台會讓端對端加密在最後一段失效，而且使用者無從得知。後果：只維護一套；B 的 Ollama 原生轉譯（228 行，處理圖片與工具呼叫的格式）先不用，需要時可做成一種額外的後端類型。Ollama 的 `/v1` 有已知限制（不吃每筆請求的 `num_ctx`），A 的文件已說明。

**D13. SYNOX 怎麼接？能不能不要另外的代理？**
查證結果：SYNOX 的 `local` 後端**不送** `Authorization`（`SYNOX/src/synox/llm/local.py:306-311`），一律串流；`openai_compat` 後端會把 `api_key` 與 `base_url` 交給 `ntu_easy_llm`（`llm/factory.py:70`、`llm/easy_llm.py:129`），呼叫的是一次回整段的函式（該套件本機未安裝，標頭與是否串流未親自確認）。
- ①：SYNOX 用 `openai_compat` 直連閘道 `/v1`。llmop 不用改；多半不串流（超過約 100 秒會被 Cloudflare 切）；明文。
- ②：閘道接受 key 放在網址路徑（`/k/<key>/v1`），SYNOX 用 `local/other`。可串流；key 會進各種紀錄；明文。
- ③：本機免 key 入口——SYNOX 與閘道同一台時填 `http://127.0.0.1:8781/v1`，對應到指定帳號（例如 VIP）。可串流、享有佇列與 VIP；本機任何程式都能用。
- ④：經 `llmop client` 代理。**唯一能端對端加密的作法**；要在 SYNOX 那台多跑一個程式。
- **做不到的事**：端對端加密必須在 SYNOX 那台機器上加密，所以不是代理就是把加密碼放進 SYNOX；沒有任何閘道端的設計能讓普通 OpenAI 用戶端變成端對端加密。
- **建議改為「依情況」（待決；第 1 版建議一律走代理）**：同機 → ③；異機不加密 → ①；要加密 → ④；② 不建議。代理因此只在加密時才需要。長期解：在 SYNOX 的 `local` 後端加可選的 API key（改 SYNOX，不在本專案範圍）。

### 己、流程

**D14.【已定案】合併方法**：**採 3.1 的 A（直接 merge 無關歷史到新的本地分支 `dev`）**。其他選項見 3.1。

### 庚、D4 / D5 定案後新增的決策

**D15. 怎麼判斷「這個請求是不是本機來的」？**（cloudflared 也是從 `127.0.0.1` 連進來，所以不能只看來源 IP）
- A：**開兩個 port**。公開 port（8780，tunnel 只指向它）完全不註冊寫入端點；本機控制台 port（8781）才有寫入功能，tunnel 永遠不會指向它。另外再加標頭與 Host 檢查當第二道。
- B：只開一個 port，靠「有沒有轉送標頭 + Host 是不是本機」判斷（LINEBot 的作法）。
- **建議 A（待決）**。後果：A 是結構上隔離，就算判斷邏輯有漏洞，外網也碰不到寫入端點；代價是本機要記兩個網址（啟動器會自動開對的那個）。B 遇到不加標頭的轉送工具（`ssh -R`、ngrok、自架 nginx）會被當成本機。

**D16. 一般使用者（不是管理員）從外網可以做什麼？**
- A：外網全部唯讀。連「申請 / 撤銷自己的 API key」都不行，key 全由管理員在本機發（D7 因此自動變成 B）。
- B：一般使用者在外網可以登入、用 playground、申請與撤銷**自己的** key；管理與設定仍然唯讀。VIP 帳號的 key 只能在本機發。
- **建議 B（待決）**。後果：A 最嚴，但朋友每次要 key 都得找你；B 之下，朋友的密碼被盜時，損害上限是他自己的額度。

**D17. 一般帳號的額度用完時怎麼辦？**

> **〔2026-11-19 已被取代〕** 由 1.5 小時桶＋LOW 規則取代（`TIERS_AND_USAGE_RULES.md` 第 3、4 節）。原因：擁有者直接給了規則。剩餘待決部分改列 D25、D26。
- A：直接拒絕（429，告知何時重置）。
- B：不拒絕，自動降到 `low` 佇列——沒人排隊時照樣能用，有人排隊就讓別人先。
- C：每個帳號可設定 `over_quota: reject | low`。
- **建議 C，預設 `low`（待決）**。後果：選 B / C-low 時，額度的意義從「用量上限」變成「優先權的額度」，GPU 閒著不浪費。

**D18. `low` 會不會永遠排不到？**

> **〔2026-11-19 已被取代〕** LOW 是暫時狀態，回水到 25% 自動回 NORMAL，不再需要升級機制；保底規則見 `TIERS_AND_USAGE_RULES.md` 7.2。
- A：嚴格優先——只要有 `normal` 在等，`low` 就不動。
- B：等超過一段時間（預設 120 秒）自動升一級，最高只升到 `normal`，永遠不會超過 VIP。
- **建議 B（待決）**。

**D19. VIP 還要不要有「同時請求數」上限？**

> **〔2026-11-19 已被取代〕** 擁有者重新界定並定案：全域名額（預設 6）＋公平排隊＋讓使用者看得到的排隊訊息；每帳號同時上限保留為公平規則。見 `TIERS_AND_USAGE_RULES.md` 第 7 節。
- A：VIP 完全沒有任何限制。
- B：VIP 不受 token 與請求次數限制，但保留同時請求上限（預設 2，可設 0 取消），免得一個 VIP 佔滿 GPU、其他 VIP 也排不到。
- **建議 B（待決）**。

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

### 階段 1 — 地基（F1–F4，加上 5.0 的 S1、F5，共 6 個任務可平行）

| ID | 目標 | 擁有的檔案 | 相依 | 卡在 | 驗收 |
|---|---|---|---|---|---|
| **F1** | 設定模型：`profile`、`gateway.planes`、`secure`、`worker`、`client` 區段與驗證；各方案的預設值；把 cli.py 裡「保留註解改 YAML」的函式搬到 `configedit.py`（cli.py 只改 import）；`settings.py` 的寫入服務（驗證 → 原子寫入 → `.bak` → 回報生效方式） | `src/llmop/config.py`、`configedit.py`、`settings.py`、`examples/config.example.yaml`、`config.example.yaml`、`tests/test_profile.py`、`tests/test_settings_service.py` | M0 | D1 D8 D9 | 沒有 `profile` 的舊 `config.yaml` 仍然載入並等同 `direct`；每個方案有載入測試；寫入失敗不留半個檔；A 的測試全過 |
| **F2** | 資料庫統一：`api_keys` 加 `sig_public_key`；新增 `workers / worker_keys / worker_models`（搬 B 的 `gateway/store.py` 對應方法）；舊資料庫自動升級；`quota.py`：VIP 免額度、`over_quota` 降級（D17）、事後記帳（D5 已定：**不做預扣**）；`usage_events` 加 `tier`、`queue_ms`；`sessions` 加 `origin` | `src/llmop/store.py`、`quota.py`、`security.py`、`tests/test_store_secure.py`、`tests/test_quota_tier.py` | M0 | D5 D7 | 用 A v0.1 的資料庫檔開啟後自動升級且資料不變；VIP 不被額度擋、用量照記；額度用完依 D17 拒絕或降級；並發下超出幅度有上界的測試 |
| **F3** | 協定 v2 與串流密碼：標頭 `stream` 欄位；`chunk` / `cancel` 訊框、`hello.features`；`crypto.py` 的分段封裝與開啟（`seq`、`final` 綁進 `info`）；路徑常數（依 D1）；更新 `PROTOCOL.md`、`SECURITY.md`（新增「串流的洩漏面」一節） | `src/llmop/protocol.py`、`crypto.py`、`docs/secure/PROTOCOL.md`、`docs/secure/SECURITY.md`、`tests/test_protocol.py`、`tests/test_crypto.py` | M0 | D1 D3 | 測試涵蓋：分段亂序、重複、缺 `final`、跨請求調換、`info` 不符，全部開啟失敗；與 `pyhpke` 的交叉驗證仍通過。D3 選 B 時，本任務只做路徑與版本，不做串流 |
| **F4** | 路徑與 Windows：`paths.py` 的 `os.fchmod` 在 Windows 上的替代作法（沒有就略過並改用建立時的權限）；`home()` 依 D9 決定預設位置；B 的 `config.json` 匯入工具 | `src/llmop/paths.py`、`src/llmop/migrate.py`、`tests/test_paths.py`、`tests/test_migrate.py` | M0 | D8 D9 | 用 monkeypatch 拿掉 `os.fchmod` 後 `write_json(secret=True)` 仍成功；`LLMOP_HOME` 仍然優先 |

**⏸ 等待點 W1**：四個任務都合進 `dev`、全套測試通過。dispatcher 把 F1–F3 產出的介面（設定 dataclass、`Store` 新方法、`protocol` 新訊框）貼進階段 2 每個任務的說明。

### 階段 2 — 功能（G1–G5，加上 5.0 的 G6，共 6 個任務可平行）

| ID | 目標 | 擁有的檔案 | 相依 | 卡在 | 驗收 |
|---|---|---|---|---|---|
| **G1** | 加密中繼移植到 FastAPI：`/e2e/v1/chat/completions`（同步 80 秒 → 202 → 輪詢；串流時回 SSE 並支援 `?after=seq` 游標輪詢）、`/e2e/v1/requests/{id}`、`/e2e/v1/models`、`/worker/ws`（握手、`chunk` 轉送、`cancel`）；接帳號、key、額度；依 `gateway.planes` 掛載或不掛載兩個面 | `src/llmop/secure/**`、`src/llmop/app.py`、`src/llmop/auth.py`、`tests/secure/**`（由 `tests/gateway/test_api.py`、`test_worker_ws.py`、`test_logging.py` 移植） | F1 F2 F3 | D1 D2 D5 | B 閘道測試的每個情境都有對應測試並通過；只開 `secure` 時 `/v1/chat/completions` 回明確錯誤；只開 `direct` 時 `/e2e/*` 與 `/worker/ws` 是 404；A 的 `tests/test_api.py` 原樣通過 |
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

**⏸ 等待點 W3**。之後先跑 5.0 的 **S9**（獨立安全複查），再進階段 4。

### 階段 4 — 整合（一個執行者）

| ID | 目標 | 擁有的檔案 | 驗收 |
|---|---|---|---|
| **I1** | 刪除已被取代的 `src/llmop/gateway/`（B 的原始閘道）與其測試；處理各任務「需要整合者處理」的項目；版本改 0.2.0；實機冒煙測試三條路徑（都接假的 Ollama）：① 直連 + 串流 ② `secure-allinone` + 串流 ③ 閘道與 worker 分兩個 process；用瀏覽器走一遍設定精靈與九個分頁；build wheel 與免安裝版；更新 `HANDOFF.md` 的「驗證過 / 沒驗證過」 | 全部（此時沒有其他執行者） | 全套測試通過（預期 800 個以上）；三條冒煙路徑的指令與輸出寫進交付報告；`git status` 乾淨 |

**⏸ 最終等待點**：交給擁有者在 Windows 主機上實測（真的 Ollama、真的 cloudflared、SYNOX）。

### 5.0 D4 / D5 定案後新增與調整的任務

| ID | 階段 | 目標 | 擁有的檔案 | 相依 | 卡在 | 驗收 |
|---|---|---|---|---|---|---|
| **S1** | 1 | 網路守門：`netguard.py`（`is_local()`、轉送標頭清單、`Host` 白名單、公開面 `Host` 檢查、log 用的字串清理函式）。純函式 + ASGI middleware，不接線 | `src/llmop/netguard.py`、`tests/test_netguard.py` | M0 | D15 | 測試矩陣：loopback 無標頭 → 本機；loopback + 任一轉送標頭 → 外網；`Host: evil.example` → 拒絕；`Host: localhost:8781` + `Origin: https://evil` → 拒絕；IPv6、大小寫、多值標頭、`Host` 帶尾點 |
| **F5** | 1 | 排程器：`scheduler.py`（等級、按使用者輪流、升級防餓死、`max_length` / `max_per_user` / `max_wait`、取消、`Retry-After` 估算、狀態快照）。與 HTTP 無關的純 asyncio 元件 | `src/llmop/scheduler.py`、`tests/test_scheduler.py` | M0 | D17 D18 D19 | 用假時鐘測：VIP 插隊但不中斷執行中的；兩個使用者 10 比 1 時輪流出隊；滿了回對應錯誤；等待者取消後名額不外洩；1000 次隨機進出後內部計數歸零 |
| **G6** | 2 | 直連面接線：`backends/base.py` 的 Semaphore 換成排程器；`api/proxy.py` 排隊期間的 SSE 保活、非串流 60 秒上限、斷線即離隊；**`/v1` 請求本文大小上限**（現在沒有，`api/routes.py:58`）；過深 JSON 回 400；`image_url` 只收 `data:`（可設定） | `src/llmop/backends/**`、`src/llmop/api/proxy.py`、`api/routes.py`、`api/sse.py`、`api/errors.py`、`tests/test_api_queue.py`、`tests/test_api.py` 與 `tests/test_backends.py`（只能新增） | F1 F2 F5 | D17–D19 | A 的 API 與後端測試原樣通過；超大本文回 413 且不讀進記憶體；排隊情境的串流收到保活行 |

既有任務的調整：

| 任務 | 增加的內容 |
|---|---|
| **F1** | 設定加 `accounts.*.tier`、`accounts.*.over_quota`、`queue:`、`server.local_port`（8781）、`server.remote_admin` |
| **G1** | hub 用 F5 的排程器（每台 worker 一個）；`pending` 回應帶 `queue_position`；`create_app(surface=…)` 產生公開面與本機面兩個 app（公開面不註冊寫入路由）；`/worker/ws` 握手失敗限流；`app.py` 不再擁有 `api/__init__.py`（改歸 G6） |
| **G2** | worker 端可設定「`image_url` 只收 `data:`」（加密面閘道看不到內容，只能在 worker 擋） |
| **G3** | **用戶端代理加固**（現在沒有任何檢查，見 6.2 第 1 項）：`Host` 白名單、`Content-Type` 必須是 `application/json`、有 `Origin` 或 `Sec-Fetch-Site: cross-site` 就拒絕、可選的本機權杖 |
| **G4** | 寫入路由只掛在本機面；唯讀遮蔽；`sessions.origin`；等級下拉選單與佇列面板；所有來自外部的字串（模型名、worker 名、使用者名）一律 `textContent`；公開面路由白名單測試 |
| **G5** | node 起兩個 uvicorn（公開面開 `proxy_headers`、本機面關）；tunnel 拒絕指向本機控制台與後端 port；直連與本機 worker 共用排程器 |
| **H1** | e2e 加：經「假 cloudflared」（從 loopback 連入並加 `Cf-Connecting-Ip`）對每個寫入端點發請求，全部被拒；佇列與 VIP 的端到端情境；`test_e2e_quota_defects.py` 改寫為「超出幅度有上界」 |
| **H2** | 免安裝版建置：Python 壓縮檔**比對寫死的 sha256**（現在只印出來，`scripts/build_portable.py:117`）；相依套件用含雜湊的鎖定檔安裝 |
| **新增 S9** | 階段 4、I1 之前：獨立的安全複查（另一個執行者，只讀程式不寫功能），照第 6 節逐項驗證並補測試；發現的問題交 I1 修。擁有：`tests/security/**`、`docs/security/web-threats.md`（把第 6 節轉成正本並更新狀態） |

### 5.1 決策 → 任務對照

| 決策 | 卡住的任務 | 若選非建議選項，計劃怎麼變 |
|---|---|---|
| D1 | F1 F3 G1 | 選 A：路徑不改，G1 改成啟動時二選一，H1 少一種矩陣 |
| D2 | G1 G4 H1 | 選 B：G1 縮成「由 node 啟動 B 的 aiohttp 閘道」；F2 不搬 worker 表；G4 的分頁 5、6 改成連到 B 的 `/admin/`；帳號與額度**不統一** |
| D3 | F3 G1 G2 G3 H1 | 選 B：四個任務的串流部分全部拿掉，約省四分之一工作量 |
| D4 | **已定案** → S1 G1 G4 G5 | — |
| D5 | **已定案** → F2 F5 G1 G4 G6 | — |
| D15 | S1 G1 G5 | 選 B：不開第二個 port，全靠 `is_local()`；測試要多涵蓋各種轉送工具 |
| D16 | G4 | 選 A：公開面連使用者的 key 端點都不註冊；D7 自動為 B |
| D17 D18 D19 | F2 F5 | 只影響排程器與額度檢查的分支與預設值 |
| D6 | — | 選 B：W3 之後另開一個階段（協定 v3） |
| D7 | F2 G4 | 選 B：G4 拿掉使用者自助綁定公鑰 |
| D8 D9 | F1 F4 G5 | 選 B：F4 不做匯入；G4 的分頁 7、8 要分別讀寫 JSON |
| D10 | G4 G5 | 選 A：G4 拿掉分頁 7、8；G5 的 node 不必在 worker 機器帶網頁 |
| D11 | G2 G3 H1 H2 | 建議已改為 B：G2、G3 各多一輪改寫（link → `websockets`，client → httpx / FastAPI），H1 的測試工具重寫；選 A 則維持 aiohttp |
| D12 | G2 | 選 B：G2 要在 B 的兩個後端各自實作串流 |
| D13 | G3 G5 G6 H3 | ③ 由 G5 / G6 在本機面提供免 key 的 `/v1`（對應指定帳號）；① 只需文件（H3）；④ 是 G3 既有範圍；選 ② 則 G6 多一條路由並要把 key 從存取紀錄遮掉。G3 的「直連上游」模式改為可不做 |

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

## 6. 資安檢查表（對照「SYNOX 網頁防護盤點」的 27 類攻擊）

狀態：**已有** = 現有程式已做（括號內是哪一邊）；**計劃** = 本計劃的任務會做；**缺口** = 現在有洞、已排任務補；**接受** = 擋不了或決定不擋，寫進文件。所有項目在 S9 逐一複查。

### 6.1 逐項對照

| # | 攻擊 | llmop 的情況與控制 | 任務 | 狀態 |
|---|---|---|---|---|
| 1 | 注入 | SQL 一律參數化（A `store.py`、B `gateway/store.py`）；YAML 用 `safe_load`；cloudflared 以參數陣列啟動、不經 shell。**新風險**：設定區可改 `tunnel.cloudflared` 的執行檔路徑，等於能指定要執行的程式 → 只能本機改，且啟動前檢查檔案存在、不接受參數字串 | G4 G5 S9 | 已有 + 計劃 |
| 2 | 路徑穿越 | 靜態檔名走白名單正規式（A `web/routes.py:75`）。新增的備份下載 / 還原只用固定檔名，不收使用者給的路徑 | G4 | 已有 + 計劃 |
| 3 | CSRF | A：每個 session 一個 CSRF 碼 + `Origin` + `Sec-Fetch-Site` 檢查（`web/routes.py:148-199`）；新的設定端點全部沿用。**用戶端本機代理沒有任何防護**（6.2-1） | G4 G3 | 已有 + **缺口** |
| 4 | XSS | A：嚴格 CSP、不載入外部資源（`web/routes.py:51`）。**新風險**：設定區會顯示 worker 自己報上來的名稱與模型名、後端回的模型名 → 一律當文字顯示，並有測試塞 `<script>` 名稱 | G4 | 已有 + 計劃 |
| 5 | 繞過登入 | 公開面路由白名單（2.7-2）；有測試列舉全部路由，確認除了登入與靜態檔以外都要 session 或 API key | S1 G1 G4 H1 | 計劃 |
| 6 | 偷登入 | cookie 只存雜湊（A `sessions.id_hash`）、HttpOnly、API key 只存 sha256。新增：session 標記 `local / remote`，外網偷到的 cookie 永遠不能寫 | F2 G4 | 已有 + 計劃 |
| 6b | 本機其他程式偷登入 | 瀏覽器的 cookie 不分 port，`127.0.0.1` 上別的程式能騙到本機控制台的 cookie。與 SYNOX 相同的未解問題。緩解：本機 session 時效短、設定區不顯示任何秘密 | — | **缺口**（接受，文件說明） |
| 7 | 暴力猜 | A：依 IP 與帳號限流（`web/throttle.py`）；key 與權杖都是 192–256 位元隨機。**`/worker/ws` 握手失敗沒有限流** → 補上 | G1 | 已有 + **缺口** |
| 8 | 重放 | 加密面：時間窗 + 開機門檻 + 已見 ID（B `worker/replay.py`）、閘道去重；串流分段把序號綁進金鑰（F3）。直連面靠 TLS，不另做 | F3 | 已有 + 計劃 |
| 9 | 請求走私 | 依賴 uvicorn（h11）與 aiohttp 的嚴格解析，前面還有 cloudflared。S9 加測試：互相矛盾的長度標頭一律 400 | S9 | 計劃 |
| 10 | 塞爆 | **A 的 `/v1` 本文沒有大小上限**（`api/routes.py:58` 直接 `request.body()`）→ 補；佇列上限（2.8）；uvicorn 連線數上限；加密分段暫存有上限與 TTL；WebSocket 訊息 16 MiB（B 已有）；登入先限流再算 scrypt | G6 F5 G1 G5 | **缺口** + 計劃 |
| 11 | DNS rebinding | **兩邊現在都不檢查 `Host`**（A 的閘道、B 的閘道、B 的用戶端代理）→ 本機面、公開面、用戶端代理全部加白名單 | S1 G3 G5 | **缺口** |
| 12 | SSRF | 後端網址只能本機改（D4）；`follow_redirects=False`、`trust_env=False`（A `backends/router.py:61`）；worker 後端只准 loopback（B）；「測試連線」是操作，外網碰不到。**新發現**：使用者可在訊息裡放 `image_url: http://內網…`，vLLM 之類的後端會自己去抓 → 預設只收 `data:` | G6 G2 | 已有 + **缺口** |
| 13 | 開放轉址 | 登入後只轉到固定的內部路徑；不接受 `next=` 之類的參數。S9 確認每一個 `RedirectResponse` | S9 | 已有（待複查） |
| 14 | 點擊劫持 | `X-Frame-Options: DENY` + `frame-ancestors 'none'`（A）；新頁面走同一個 `_UIRoute` | G4 | 已有 |
| 15 | 不安全的還原 | 只讀 JSON 與 `yaml.safe_load`；B 的協定解析拒絕重複鍵與 `NaN`。直連面補：巢狀過深回 400 而不是 500 | G6 | 已有 + 計劃 |
| 16 | 檔案上傳 | 目前沒有上傳。新增的「還原設定」：只限本機、有大小上限、只解析並驗證、絕不執行 | G4 | 計劃 |
| 17 | 供應鏈 | 相依套件沒有鎖版本與雜湊；**免安裝版下載的 Python 只印出 sha256、沒有比對**（`scripts/build_portable.py:117`）；cloudflared 靠 winget。→ 鎖定檔 + 比對雜湊；CI 加 `pip-audit` | H2 | **缺口** |
| 18 | 提示注入 | llmop 只轉送，不執行任何工具（B SECURITY 檢討 13：worker 原樣交還 `tool_calls`）。playground 把模型輸出當文字顯示。llmop 自己沒有會被騙的 AI | G4 | 不適用 / 已有 |
| 19 | AI 或排程冒充人 | 分層已做：API key 只能打 `/v1`，永遠進不了網頁與管理端點（A 的設計原則）。寫入另外要求「本機 + session + CSRF」。SYNOX 拿到的只會是 API key | G4 | 已有 + 計劃 |
| 20 | 帳號被盜 | 管理員密碼在外網被盜 → 只能看、不能改（D4）。一般帳號被盜 → 損害以該帳號額度為上限。**VIP 沒有額度上限** → VIP 的 key 只能本機發（D16）、保留同時上限（D19）、佇列面板看得到異常 | G4 F5 | 計劃 |
| 21 | 臨時網址被濫用 | quick tunnel 網址任何人都敲得到：公開面全部要登入或 key；寫入端點不存在；tunnel 只准指向公開 port（2.7-6）；網址每次重啟都換 | G5 | 計劃 |
| 22 | 中間的人看得到內容 | 直連方案：Cloudflare 看得到對話明文、密碼、cookie、key → **接受**，並在方案選擇頁寫明；要避免就選加密方案（對話內容看不到；key 被看到也因為要簽章而無法單獨使用）。登入密碼經過 Cloudflare 這點在兩種方案都一樣 → 靠 D4 把損害限制在唯讀 | H3 | **接受** |
| 23 | 錯誤訊息洩漏 | 文件頁與 `Server` 標頭已關（A）。**後端的錯誤文字會原樣傳給使用者**（可能含內部網址、路徑）→ 公開面只回固定句子 + 錯誤碼，細節只進本機 log；`/health` 不回版本號給外網 | G6 S9 | 已有 + **缺口** |
| 24 | 紀錄被塞假資料 | A 的登入紀錄用 `%r`（會跳脫換行）；B 的紀錄只有固定欄位並有靜態測試。新增：所有進 log 的外來字串過 `netguard` 的清理函式 | S1 S9 | 已有 + 計劃 |
| 25 | 試算表公式注入 | 目前沒有 CSV 匯出。規則先寫下：日後做用量匯出時，`= + - @` 開頭的格子一律加前綴 | — | 不適用（規則已記） |
| 26 | 同一台電腦的別人 | 本機控制台仍要登入（不做自動登入）。**Windows 上金鑰檔的 0600 沒有效果**（F4 先讓它能跑，ACL 另議）；用戶端代理對本機任何程式開放 → 可選的本機權杖 | F4 G3 | **缺口**（部分接受） |
| 27 | 惡意的工具程式 | llmop 沒有外掛。相近的情況：惡意 worker（註冊權杖只能本機發 + 用戶端釘選公鑰）、惡意後端（只連管理員在本機設定的網址） | G1 | 不適用 / 已有 |

### 6.2 合併前最值得先修的洞（依嚴重度）

1. **B 的用戶端代理（`127.0.0.1:8788`）完全沒有防護**：不驗證、不查 `Host`、不查 `Origin`、不查 `Content-Type`（`client/proxy.py:84`、`:259` 只檢查綁定位址）。使用者只要開著代理又瀏覽到惡意網頁，那個網頁就能用 DNS rebinding 以使用者的 API key 與簽章金鑰送請求並讀到回覆。→ G3。
2. **A 的 `/v1` 沒有請求大小上限**，任何持有有效 key 的人（驗證在讀本文之前，`api/routes.py:113`）可以送超大本文吃光記憶體。→ G6。
3. **沒有任何地方檢查 `Host`**（檢查表 11）。→ S1。
4. **B 的「是否本機」只看 `Cf-Connecting-Ip` 一個標頭**（`gateway/admin.py:91`），B 自己的文件也承認 ngrok / `ssh -R` 會繞過。→ S1 的完整標頭清單 + D15 的兩個 port。
5. **A 開了 uvicorn 的 `proxy_headers`**（`cli.py:813`）：對端位址會被轉送標頭改寫，任何「看對端 IP 判斷本機」的新程式都會出錯。→ 本機面關閉 `proxy_headers`，`is_local()` 只在 `netguard` 一處實作。
6. **`image_url` 讓後端替使用者抓內網網址**（檢查表 12）。→ G6、G2。
7. **免安裝版的供應鏈沒有比對雜湊**（檢查表 17）。→ H2。
8. **後端錯誤文字原樣外流**（檢查表 23）。→ G6。

## 7. 無法驗證或尚未驗證的事

- **B 在 Windows 上的行為**：沒有 Windows 環境。`os.fchmod` 在 Windows 要 Python 3.13 才有，是依 Python 文件判斷，沒有實跑；0600 權限在 Windows 上實際沒有保護效果，私鑰檔的保護要另外想（F4 先讓它能跑，是否加 ACL 留待之後）。
- **真的 Ollama、真的 cloudflared、官方 `openai` SDK、SYNOX 實際連線**：兩邊都只用假後端測過（A 的 HANDOFF §4 也這樣寫）。
- **quick tunnel 對 SSE 與 WebSocket 的實際表現**：Cloudflare 官方不保證 quick tunnel 支援 SSE；`/worker/ws` 經 tunnel 的穩定度、加密串流是否會被緩衝，都要實測。2.4 的游標輪詢就是為這個風險準備的。
- **`cryptography` 47–49 版的 HPKE**：B 的 SECURITY.md 說只在 50.0.1 確認過；我跑測試用的是 50.0.2。
- **SYNOX 的 `openai_compat` 後端是否支援自訂 `base_url` + API key**：只確認了 `local` 後端不送 `Authorization`，沒有細讀 `openai_compat`（影響 D13 的 B 選項）。
- **uvicorn 的 WebSocket 與 aiohttp 在同一個 process 共存**：理論上沒問題（同一個 asyncio loop），尚未實作驗證（G5）。
- **第 6 節的現況判斷多數來自讀程式**，沒有實際發動攻擊驗證（例如用 DNS rebinding 打用戶端代理、用 `image_url` 讓 vLLM 抓內網）；S9 要實測。
- **GitHub**：push 仍被擋（403），本計劃全部假設只做本地 commit。
- 2.4 的工作量是估計值。


---

## 8. 第 3 版增補（擁有者第二批回覆後）

### 8.1 D1 定案：入口 × key 的規則表

不做兩套並行機制，而是一張政策表。請求格式仍有兩種（明文、信封），但准入由「入口（listener）」與「key／帳號」兩層決定。

| 入口（zone） | 綁定 | 明文 | 加密 | 管理寫入 |
|---|---|---|---|---|
| `local`（本機 API＋本機控制台） | `127.0.0.1:8781` | 允許（預設） | 允許 | **唯一允許處** |
| `lan`（預設關閉） | 區網位址 `:8783` | 預設拒絕；本機控制台明確開啟 | 允許 | 無 |
| `public-ui`（登入網站） | `127.0.0.1:8780` ← 通道 #1 | 不適用（無 API 路由） | 不適用 | 無（唯讀） |
| `public-api` | `127.0.0.1:8782` ← 通道 #2 | **預設拒絕**；key 標記 `encryption: optional` 才允許 | 允許（預設） | 無 |

- key 欄位 `encryption: required | optional`；帳號欄位 `require_encryption: true` 時，其 key 在任何入口送明文都拒絕。
- 「worker 的本機明文 port」＝節點的 `local` 入口，不放進 `worker/link.py`。worker 的加密路徑仍無監聽 socket。B 的文件用語改為「worker 不開任何從機器外連得到的 port」。安全後果：該機器上任何程式可不經加密使用 LLM（與 Ollama 開在 loopback 同級），純指令模式可完全不開。
- **待擁有者確認的三點**：（1）公開入口是否允許逐 key 的明文例外（假設允許）；（2）上述本機明文入口的處理；（3）本機入口是否免 key、對應到指定帳號（與 D13 ③ 同一件事）。

### 8.2 D3 定案：串流預設值依入口

| 入口 | 串流預設 |
|---|---|
| `local`、`lan` | 開 |
| `public-api`（quick tunnel） | **關**（Cloudflare 官方不支援 quick tunnel 的 SSE；文件要寫明） |
| `public-api`（named tunnel） | 見 D21 |

可依入口與帳號覆寫（`listeners.<zone>.stream`、`accounts.*.stream`）。加密面另有「游標輪詢」的分段取回（非 SSE），列為可選模式 `stream_transport: off | poll | sse`，公開預設 `off`。

### 8.3 帳號、key、臨時共用 key、等級

> **〔2026-11-19 已被取代〕** 本節的等級表與「超額道」由 `TIERS_AND_USAGE_RULES.md` 取代。原因：LOW 改為 NORMAL 超過 1.5 小時尖峰保護後的狀態，訪客改稱 GUEST。帳號與臨時 key 的其餘敘述仍有效。

- 帳號與用量控制在本機與公開都生效（本機入口對應到帳號，用量照記）。
- 帳號 ⇄ key：建議預設一把、可輪替（新 key 發出後舊 key 再有效 24 小時）、管理員可逐帳號放寬（D20）。
- **臨時共用 key**：不屬於任何帳號；只能在本機控制台發；固定 `low`；必有 `expires_at` 與**總額度**（非每日）；`encryption` 可選；用完或到期即停用。`store.api_keys` 需允許 `username = NULL` 並加 `budget_tokens`、`tier`、`encryption`、`label`。
- **等級**（取代 2.8 的三級）：

| 等級 | 對象 | 每日 token | RPM | 同時上限 | 順序 | 額度用完 |
|---|---|---|---|---|---|---|
| `vip3` | 擁有者、核心成員 | 不限 | 不限 | 4 | 1 | — |
| `vip2` | 維護團隊、本地人員 | 不限 | 不限 | 3 | 2 | — |
| `vip1` | 需要大量 token 的親友 | 2,000,000 | 60 | 2 | 3 | 進超額道（D17） |
| `normal` | 親友 | 200,000 | 20 | 2 | 4 | 進超額道（D17） |
| （`overflow`，內部道） | vip1 / normal 超額後 | — | — | 1 | 5 | — |
| `low` | 公開 key 的訪客 | 整把 key 50,000 | 10 | 1 | 6 | **停用（已定）**；不升不降 |

數字為提案（D23）。D18 改為「不升級任何人，只保證同等級內久等者優先＋給 normal 以下的最低服務比例」。D19 改為每級各有同時上限。

### 8.4 兩個公開網址、秘密路徑、網址地圖

> **〔2026-11-19 已被取代〕** 「登入網址需要 named tunnel 才穩定」的說法修正：擁有者的用意是兩個通道各自獨立的生命週期，見 9.4。named tunnel 是可選項。其餘內容仍有效。

- 兩個 cloudflared（llmop 代管兩個 `TunnelManager`）：#1 → `public-ui` 入口，#2 → `public-api` 入口。兩個入口路由表互斥：UI 入口無 `/v1`、`/e2e`、`/worker/ws`；API 入口不發 cookie、無登入頁。
- **quick tunnel 的網址每次重啟都換，兩個都會換**；「登入網址很少變」需要 named tunnel（D24）。只重啟 #2 即可單獨輪替 API 網址。
- UI 入口登入後顯示「目前的 API 網址」（`AppState` 持有兩個通道的即時網址）。
- 秘密路徑（D22，建議 C）：全站門牌 `/<gate>/…`（無門牌一律 404，在任何驗證碼之前）＋ key 可放標頭或路徑 `/<gate>/k/<key>/v1/…`。存取紀錄必須遮蔽路徑中的秘密；資料庫只存雜湊。OpenAI SDK 的 `base_url` 可直接用。
- 網址地圖：`<api>/<hash>/v1/…`（API）；`<api>/<hash>/`（個人後台：用量＋撤銷本 key；申請新 key 仍需登入）；`<api>/dashboard/<hash2>`（唯讀儀表板，**獨立的 token**、可到期、可作廢、無任何寫入路由）；`<ui>`（登入）；`127.0.0.1`（管理，外網與區網皆不可達）。
- 極限：秘密路徑擋不住 Cloudflare（它看得到完整 URL）；防的是掃描與未授權探測。

### 8.5 D6 重新界定

查證（B）：`worker/keystore.py:41-84` 支援多把金鑰並存（`rotate` 保留舊鑰、`prune` 才刪）；用戶端可同時釘選多把（`client/sdk.py:184`）；閘道不存任何信封，結果只在記憶體保留 600 秒；對話歷史只存在用戶端程式（明文），每筆請求獨立加密並帶完整歷史。結論：**換鑰不會讓舊對話讀不到，也不會讓進行中的對話接不下去**。唯一中斷點：`rotate-key` 後要重啟 worker 才生效（`docs/DEPLOY.md` §7），重啟瞬間 in-flight 的那一筆失敗。

- (a) 換鑰延續性：A1 維持現狀；**A2（建議）** 熱載入金鑰（不重啟）、自動重疊期（預設 7 天）、worker 在**封裝回覆內**附「下一把公鑰」通知，用戶端自動加入釘選，控制台顯示尚未換鑰的用戶端數。
- (b) 前向保密：B1 這版不做（建議）；B2 這版做（短期預備金鑰，多一個階段）。
- A2 的自動釘選需列入 S9 安全複查（論證：通知位於只有持舊私鑰者才產生得出的封裝回覆內）。

### 8.6 D2 在新需求下的分析

佇列與等級必須是單一的記憶體排程器；帳號、key 撤銷必須即時一致。拆成多個 process 需要跨程序的佇列與同步，是最大的新風險來源；擁有者要的「分開」可由「不同入口、不同 port、不同路由表、不同公開網址」達成。**建議維持 A：一支閘道程式、多個入口**（每個入口一個 uvicorn server、共用 `AppState`）。worker、client 在別台機器時本來就是另外的 process。

### 8.7 新增決策

| # | 問題 | 建議 |
|---|---|---|
| D20 | 一個帳號幾把 key | B：預設一把＋輪替，管理員可放寬 |
| D21 | 「公開預設不串流」是否含公開明文與 named tunnel | B：quick tunnel 上都關，named tunnel 預設開 |
| D22 | 秘密路徑的語意 | C：全站門牌＋key 可放標頭或路徑；儀表板用獨立 token |
| D23 | 五個等級的預設數字 | A：採 8.3 的表，上線後調 |
| D24 | 是否用 named tunnel | B：登入網站用固定網域，API 用 quick tunnel（可先 A 後 B） |

### 8.8 任務重排（取代第 5 節中相衝突的部分）

新增任務：

| ID | 階段 | 目標 | 擁有的檔案 | 相依 | 卡在 |
|---|---|---|---|---|---|
| **F6** | 1 | 入口模型：`listeners`（zone、bind、明文/加密/串流政策）、`ZonePolicy` 判斷函式（key × 帳號 × 入口 → 允許與否）；純邏輯 | `src/llmop/listeners.py`、`tests/test_listeners.py` | M0 | D1 三點確認、D15、D21 |
| **F7** | 1 | 等級與 key 模型：五級＋`overflow`、每級預設、臨時共用 key（無帳號、總額度、到期）、key 輪替重疊、`encryption` 欄位、儀表板 token | 併入 **F2**（`store.py`、`quota.py`、`security.py`）；測試 `tests/test_tiers.py`、`tests/test_shared_keys.py` | M0 | D17 D19 D20 D23 |
| **G7** | 2 | 多入口啟動與公開路由：四個入口各自的 app／路由表；秘密路徑門牌與 `/k/<key>/`；個人後台；唯讀儀表板；存取紀錄遮蔽；路由互斥測試 | `src/llmop/app.py`、`src/llmop/public/**`、`tests/test_public_routes.py` | F1 F2 F6 S1 | D2 D15 D16 D22 |
| **G8** | 2 | 雙通道：兩個 `TunnelManager`、單獨重啟、named/quick 混用、UI 顯示目前 API 網址、拒絕指向本機與後端 port | `src/llmop/tunnel.py`、`tests/test_tunnel.py`（只能新增）、`tests/test_dual_tunnel.py` | F1 | D24 |
| **G9** | 2 | 換鑰延續性（D6-A2）：金鑰熱載入、重疊期、封裝回覆內的下一把公鑰通知、用戶端自動釘選、控制台統計 | 屬 **G2**（worker）與 **G3**（client）的範圍；協定欄位在 **F3** | F3 | D6 |

既有任務的變更：
- **F1**：加 `listeners:`、`tiers:`、`tunnels:`（兩個）、`accounts.*.tier / require_encryption / stream`。
- **F2**：吸收 F7；`app.py` 的所有權由 G1 移到 **G7**。
- **F5**（排程器）：六道（五級＋overflow）、每級同時上限、最低服務比例；`low` 不參與任何升降。
- **G1**（加密中繼）：不再擁有 `app.py`；改為提供 router 給 G7 掛到 `public-api`、`lan`、`local` 入口；准入呼叫 F6 的政策。
- **G5**（node / CLI）：`tunnel.py` 移交 G8；node 啟動 3–4 個 uvicorn。
- **G6**（直連面）：准入呼叫 F6；串流預設依入口；T1 相關僅做「後端 context 超限 → 可辨識的錯誤碼」（`docs/plan/TODO.md` T1，其餘不處理）。
- **G4**（網頁）：登入網站顯示目前 API 網址；等級下拉改五級；臨時共用 key 與儀表板連結的發放頁（本機）；唯讀儀表板頁。
- **H1**：e2e 加入口矩陣（每個入口 × 明文/加密 × key 政策）、臨時 key 到期與用盡、儀表板連結不能寫入、換鑰期間請求不中斷。
- **S9**：加審秘密路徑的紀錄遮蔽、儀表板 token 與 API token 的隔離、D6-A2 的自動釘選。

階段與等待點（重排後）：
- 階段 0：M0 ⏸ W0（所有決策定案）
- 階段 1（平行）：F1、F2（含 F7）、F3、F4、F5、F6、S1 ⏸ W1
- 階段 2（平行）：G1、G2（含 G9-worker）、G3（含 G9-client）、G4、G5、G6、G7、G8 ⏸ W2
  - 檔案所有權互斥：`app.py` → G7；`tunnel.py` → G8；`cli.py`、`node.py` → G5；`secure/**` → G1；`backends/**`、`api/**` → G6；`web/**` → G4；`public/**` → G7。
- 階段 3（平行）：H1、H2、H3 ⏸ W3 → S9
- 階段 4：I1 ⏸ 交擁有者實測

決策 → 任務（新增與變更）：

| 決策 | 卡住的任務 |
|---|---|
| D1 的三點確認 | F6 G6 G7 |
| D2 | G1 G7（選 B/C 則 G7 與 F5 的共用排程器需重新設計） |
| D6 | F3 G2 G3（G9） |
| D15 | S1 F6 G7 G5 |
| D16 D20 | F2 G4 G7 |
| D17 D18 D19 D23 | F2 F5 |
| D21 | F6 G6 G1 |
| D22 | G7 S9 |
| D24 | G8 H3 |


---

## 9. 第 4 版增補（擁有者第三批回覆，2026-11-19）

擁有者的總方針：**東西變得太複雜了。這是給少數人用的小型 OpenAI 式 API 服務。保持簡單，不要加超出下面範圍的功能。**

### 9.1 範圍（縮小後）

- **只有一個模型**，使用者不能選。請求裡的 `model` 欄位一律換成設定的那一個；`/v1/models` 只回一筆。A 版的模型別名、每帳號可用模型清單不再出現在介面上（程式可保留，不暴露）。
- **真正的控制台只在本機**（`127.0.0.1`）。
- **外面的人只做三件事**：看狀態、建立／撤銷自己的 API key（可逐把選擇是否加密）、看用量。沒有 playground、沒有模型選單、沒有「部署方案」畫面。
- 本專案的交付物是兩樣（D13）：**(1) 伺服器**（閘道＋worker）；**(2) 用戶端外掛**（`llmop client`：SDK＋本機代理），只有加密模式需要。

### 9.2 本批定案

| 決策 | 定案內容 |
|---|---|
| D1 | B。三個疑點由擁有者確認：加密與否**逐把 key 決定**；**本機入口不需要 key**；worker 那一側開只限 loopback 的明文入口**可接受**（後果已知：該機器上任何程式可不經加密使用 LLM，與 Ollama 開在 loopback 同級） |
| D3 | A。公開入口預設不串流、本機入口預設串流 |
| D7、D8 | A |
| D10 | 簡化版：一個本機控制台＋一個外部入口網頁（9.3）。v2 的「每台機器的控制台頁面表」降為附錄，不在這一版實作多餘的頁面 |
| D11 | **授權規劃者決定 → 閘道用 FastAPI；worker 與用戶端維持 aiohttp**（9.5） |
| D12 | A：A 的後端轉接層＋worker 只准連 loopback |
| D13 | 方向定案：直連不需要外掛；加密一定要外掛（9.6） |
| D14 | A（不變） |
| D16 | B，但功能刻意精簡（9.1 的三件事） |
| D19 | 重新界定：全域名額預設 6＋公平排隊＋排隊訊息（`TIERS_AND_USAGE_RULES.md` 第 7 節） |

### 9.3 最簡架構（取代 2.6 與 8.x 中較複雜的畫面規劃；舊內容保留備查）

| 入口 | 誰用 | 有什麼 |
|---|---|---|
| 本機控制台 `127.0.0.1` | 只有管理員 | 帳號與等級、發 GUEST key、後端與名額、兩個通道的狀態與「更換 API 連結」按鈕、佇列即時狀況、worker 與金鑰、原始設定檔 |
| 本機 API `127.0.0.1/v1` | 這台電腦上的程式 | 明文、免 key、可串流；對應到設定指定的帳號 |
| 帳號網站（通道 #1） | 有帳號的人 | 登入 → **入口網頁**：狀態、兩個用量表、自己的 key（建立／撤銷／逐把選擇是否加密）、每把 key 的用量、**目前的 API 連結** |
| API 連結（通道 #2）`<api-host>/<秘密路徑>/…` | 程式 | `/v1/*`（明文）、`/e2e/v1/*`（加密）、`/worker/ws`；`…/dashboard/<另一串>` 唯讀儀表板 |

- 「加密是不是換個網址就好，不必有設定方案？」——是。**送到 `/v1` 就是明文、送到 `/e2e/v1` 就是加密**，由用戶端能不能加密決定，沒有「方案」要選。但 key 上的 `encryption` 旗標仍然要有：它規定這把 key **准不准**走明文。沒有旗標的話，任何拿到這把 key 的人或設定錯誤的程式都能改走明文，「這把 key 一定加密」就不成立。所以：網址決定「怎麼送」，旗標決定「准不准」。
- `profile` 設定簡化為 `roles: [gateway, worker]` 之類的清單；不做方案精靈。
- 入口網頁放在帳號網站（要密碼）還是 API 連結底下（只靠網址）：見 D22（待決）。規劃者建議：會寫入的動作（建立 key）要密碼；API 連結底下只放 API 與唯讀頁。

### 9.4 兩個通道、各自獨立（修正 8.4 與 D24）

擁有者的用意：**帳號網站的通道幾乎不重啟，所以網址實際上不太變；API 的通道可以隨時故意換掉**（做法與 LINEBot 的 `tunnel.py`＋`webhook_window.py` 類似）。named tunnel 不是必要條件，只是可選的加強。

- 兩個 cloudflared 行程，各由一個 `TunnelManager` 看管，生命週期互不相干。重啟 API 通道**不得**動到帳號通道。
- 誠實的但書：**機器重開或某個 cloudflared 當掉時，重啟的那個通道網址會變**。帳號網站的網址變了，使用者就要靠你另外通知（LINE 等）。
- **更換 API 連結的流程**：
  1. 管理員在本機控制台按「更換 API 連結」（或 `llmop tunnel rotate-api`）。可選：只換秘密路徑（瞬間）、或連主機名一起換（重啟通道 #2，約數秒）。
  2. 系統產生新的秘密路徑；若換主機名則啟動新的 cloudflared、拿到新網址後才關掉舊的。
  3. **舊連結預設立刻失效**；可設寬限期（預設 0，建議需要時設 10 分鐘），寬限期內新舊都通。
  4. 所有 API key **不受影響**（key 是 key，連結是連結），使用者不必重新申請。
  5. 使用者登入帳號網站，入口網頁顯示新的 API 連結；程式改一個 `base_url` 即可。用戶端外掛可設定「從帳號網站自動取得目前 API 連結」（列為之後的項目，不在這一版）。
- 紀錄：每次更換寫一筆稽核紀錄（時間、原因欄位、舊連結的雜湊）。

### 9.5 D11：授權決定的理由

決定：**閘道 FastAPI；worker 與用戶端維持 aiohttp。**

1. 限制條件是「改動最少、風險最低」。閘道本來就要移到 A 的 FastAPI 程式（D2 建議），這部分不算額外成本。
2. worker 連線（233 行）與用戶端（約 630 行）是安全關鍵且已有約 2,900 行測試保護；換底層等於重寫這些測試，風險與工作量最大的就是這一塊。
3. D12＝A 之後，worker 的後端呼叫改用 A 的 httpx 轉接層，aiohttp 在 worker 裡只剩 WebSocket 連線一個用途，範圍已經很小。
4. 兩套函式庫並存的代價只有：免安裝版多約 10 MB、`secure` 附加套件多一個相依。
5. 「之後統一」保留為不緊急的清理項目，等功能穩定、測試足夠時再做，屆時可逐檔替換。
6. 這推翻第 2 版「全部改 FastAPI」的建議：當時假設 worker／用戶端反正要大改；範圍縮小後（無 playground、單一模型、不做多餘畫面）這個假設不成立。

### 9.6 D13：兩樣交付物（確認擁有者的理解）

擁有者的理解**正確**：直連（明文）不需要外掛，消費端自己用現成的 OpenAI 相容寫法或加一個 adapter 即可；加密一定要外掛。

第 2 版 D13 的圖和這個理解的差別：圖把四條接法並列，看起來像四種產品。其實 ①②③ 都是「伺服器的直連」，只差 key 怎麼帶；只有 ④ 用到第二樣交付物。v3 的圖改成兩欄。

| 模式 | 消費端要什麼 | SYNOX 的作法 |
|---|---|---|
| 直連・同一台 | 無 | `local/other` 指向本機 API（免 key） |
| 直連・外網 | 無（帶 key） | `openai_compat`＋key；或 `local/other`＋key 放在路徑（D22） |
| 加密 | **用戶端外掛** | `local/other` 指向外掛的本機代理 |

**交給 SYNOX 的事項（SYNOX 對本專案唯讀，列為 handoff）：**
- H-S1：`local` 後端加可選的 API key 標頭（最乾淨的直連作法）。
- H-S2：確認 `openai_compat`（`ntu_easy_llm`）送出的標頭與是否串流。
- H-S3（可選）：把 llmop 用戶端 SDK 做成 SYNOX 的 adapter，省掉另外跑代理。

### 9.7 新增待決

| # | 問題 | 規劃者建議 |
|---|---|---|
| D25 | 每週用完後：LOW 的待遇直到重置，還是直接擋 | LOW 的待遇 |
| D26 | VIP I 桶見底：降到 NORMAL 順位，還是變 LOW；VIP II／III 是否完全不設表 | VIP I 降一級；II／III 不設 |

D20–D24 依擁有者指示維持待決、編號不變。備註（不代表已決）：D20——擁有者這批提到「每個帳號可建立自己的 API keys、看每把的用量」；D21——提到「公開預設不串流」；D24——依 9.4 重述為「兩個獨立通道是否另外採用固定網域」，固定網域為可選。

### 9.8 任務重排（取代 8.8 中相衝突的部分；8.8 保留）

**刪減的工作（因範圍縮小；任務編號保留、內容縮小）：**
- G4：不做九個分頁。只做：本機控制台（9.3 第一列）＋入口網頁（狀態、用量表、key 管理）＋唯讀儀表板。不做 playground 搬遷、方案精靈、worker／client 專屬頁面（附錄保留）。
- G2、G3：維持 aiohttp（D11）。G2 仍要換後端轉接層（D12）與串流；G3 仍要串流。
- F1：`profile` 簡化為 `roles`；加 `model`（單一）、`usage`、`tiers`、`queue`、`listeners`、`tunnels`。
- G6：加「`model` 一律覆寫為設定值」「`/v1/models` 只回一筆」。

**新增或改寫：**

| ID | 階段 | 目標 | 擁有的檔案 | 相依 | 卡在 |
|---|---|---|---|---|---|
| **F7′**（改寫 F7） | 1 | 用量模型：1.5 小時桶（持久化水位）、每週表與週日重置、NORMAL↔LOW 狀態機、GUEST key（總額、到期、重置旗標、失效後仍可查）、等級預設值 | 併入 F2：`store.py`、`quota.py`、`security.py`；新檔 `src/llmop/usage.py`；`tests/test_usage_bucket.py`、`tests/test_low_state.py`、`tests/test_guest_keys.py` | M0 | D25 D26 |
| **F5′**（改寫 F5） | 1 | 排程器：全域名額、六個順位、同級輪流、每帳號同時上限、每 6 筆保底 1 筆、位置與預估等待、取消 | `src/llmop/scheduler.py`、`tests/test_scheduler.py` | M0 | — |
| **G10** | 2 | 排隊訊息：429＋`Retry-After`＋雙語訊息、SSE 註解行、加密 `pending.queue`、`/v1/me` 擴充；名額與後端平行數／context 的啟動檢查與警告（T1） | `src/llmop/api/**` 中與回應有關的部分歸 G6，本任務併入 **G6**；加密面的部分併入 **G1**；`tests/test_queue_messages.py` | F5′ F7′ | — |
| **G8′**（改寫 G8） | 2 | 兩個獨立通道＋「更換 API 連結」流程（9.4）：只換路徑／連主機名一起換、寬限期、稽核紀錄、入口網頁顯示目前連結 | `src/llmop/tunnel.py`、`src/llmop/apilink.py`、`tests/test_dual_tunnel.py`、`tests/test_api_link_rotate.py` | F1 | D22 D24 |

**階段（不變的骨架）：**
- 階段 0：M0 ⏸ **W0：等擁有者把所有決策定案**
- 階段 1（平行）：F1、F2（含 F7′）、F3、F4、F5′、F6、S1 ⏸ W1
- 階段 2（平行）：G1、G2、G3、G4（精簡）、G5、G6（含 G10）、G7、G8′ ⏸ W2
- 階段 3（平行）：H1、H2、H3 ⏸ W3 → S9
- 階段 4：I1 ⏸ 交擁有者實測（含 `TIERS_AND_USAGE_RULES.md` 第 6 節的校正）

**決策 → 任務（本批變動）：**

| 決策 | 卡住的任務 |
|---|---|
| D2 | G1 G7（仍待決；建議 A） |
| D6 | F3 G2 G3 |
| D9 | F1 F4（只影響預設路徑） |
| D15 | S1 F6 G7 G5 |
| D20 | F2 G4 |
| D21 | F6 G6 |
| D22 | G7 G8′ G4 |
| D23（數字） | 不卡任務：數字是設定值，實測後調 |
| D24 | G8′ H3 |
| D25 D26 | F7′ |

### 9.9 給 SYNOX 的 handoff 與 TODO 指引

- SYNOX：H-S1、H-S2、H-S3（9.6）。
- `docs/plan/TODO.md` T1（Context size exceeded）：本計劃只做兩件相關的事——G6 把後端的 context 超限轉成可辨識的錯誤碼；G10 檢查名額與後端平行數。其餘等擁有者說開工。
