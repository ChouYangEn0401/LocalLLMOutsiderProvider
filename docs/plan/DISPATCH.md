# 派工單（DISPATCH）— llmop v0.2 MVP

> 2026-11-19。執行者只需要讀這一份。決策依據：`MERGE_PLAN.md` 第 10 節。用量規則：`TIERS_AND_USAGE_RULES.md`（下稱 TIERS）。
> 目標：最快做出**可用**的產品。不在簡報裡的功能一律不做。

## 1. 每個執行者都要遵守

- 只在 `/home/user/LocalLLMOutsiderProvider` 的本地分支 `dev` 工作。**不碰** `main`／`master`、`docs/BU_Keep_*`、`docs/BU_Keep.sha256`、`tests/test_bu_keep.py`。不 push。
- `/home/user/SYNOX`、`/home/user/LINEBotForChurch` 唯讀。
- 只改簡報「擁有」欄列出的路徑。需要動別人的檔案時，不要動，寫進交付報告的「需整合者處理」。
- 環境：`.venv/bin/pip install -e ".[dev,secure]"`。交付前跑 `.venv/bin/python -m pytest -q`，全綠才算完成。
- A 原有的 221 個測試不得刪改斷言（只能新增）。B 的測試可隨介面調整，但檢查的性質不能變弱，改了哪些要列出。
- commit 由 dispatcher 做，或依 dispatcher 指示。
- 不加簡報以外的功能。不確定就選最簡單的做法並在報告裡說明。
- 加密相關模組（`secure/`、`worker/`、`client/`、`crypto.py`、`protocol.py`）不得把內容、key、權杖寫進 log。
- 交付報告：改了哪些檔、測試數（之前 → 之後）、沒做完的、需整合者處理的。

## 2. MVP 範圍

**要做：** A 的閘道為底，併入 B 的加密中繼、worker、用戶端外掛；帳號與每帳號的 key（含 `encryption` 旗標）；TIERS 的等級、兩個用量表、排隊；單一模型；本機控制台＋外部唯讀入口網頁（可管理自己的 key）；兩個獨立通道與「更換 API 連結」；Windows 免安裝版。

**延後到 v0.3+（這一版不做）：**
- 加密模式的真串流（D3）：MVP 沿用 B 的假串流。
- worker 改用 A 的後端轉接層（D12）：MVP 沿用 B 的 worker 後端（已是只連 loopback）。
- worker／用戶端設定搬進 `config.yaml`（D8）：MVP 沿用 `LLMOP_HOME` 下的 JSON。
- 無縫換鑰（D6-A2）、前向保密。
- 區網入口、key 放在網址路徑（`/k/<key>/`）、唯讀儀表板分享連結的到期設定。
- Docker 與 CI 的更新（保留現有檔案不動）。
- worker／用戶端的網頁控制台、playground 對外、模型別名介面。
- T1（context 超限）的完整處理：只做錯誤碼轉換與名額警告。

## 3. 共同約定（跨任務介面，不得自行更改）

- 套件佈局：A 的模組留原位；B 的 `worker/`、`client/`、`crypto.py`、`protocol.py`、`paths.py` 留原位；B 的 `gateway/` 在 T-5 移植為 `secure/` 後由 T-9 刪除。
- 入口與 port：`local` 127.0.0.1:8781（控制台＋免 key 的 `/v1`）；`public-ui` 127.0.0.1:8780（帳號網站）；`public-api` 127.0.0.1:8782（`/<gate>/v1/*`、`/<gate>/e2e/v1/*`、`/<gate>/worker/ws`）。
- Router 介面：`build_api_router(state, zone)`、`build_secure_router(state)`、`build_web_router(state, surface)`，`surface ∈ {"local","public"}`，`zone ∈ {"local","public"}`。
- `AppState` 新增：`scheduler`、`usage`、`policy`、`api_link`（由 T-9 接線；T-5/6/8 以 `getattr(state, …)` 取用並在測試中自行建立）。
- key：新發的一律 `sk-llop-`；欄位 `encryption: required|optional`；`username` 可為空（GUEST）。
- 單一模型：設定 `model:`；請求的 `model` 一律覆寫。

---

## 階段 0（1 個任務）

### T-M0 合併
- 目標：建立 `dev`，把 B 併入，784 個測試全過。
- 步驟：`MERGE_PLAN.md` 3.2 的指令與 3.3 的衝突解法（5 個檔）。分支來源 `claude/youthful-galileo-u66ybv`，B 是 `origin/claude/lm-studio-e2e-encryption-ug09c3`。
- 擁有：`.gitignore`、`pyproject.toml`、`src/llmop/__init__.py`、`src/llmop/__main__.py`、`tests/e2e/test_docs.py`、`tests/e2e/test_static_logging.py`、`docs/secure/**`（搬移 B 的 README、DEPLOY、PROTOCOL、SECURITY）。
- 不碰：其他一切原始碼。
- 重點：`__main__.py` 做成分派器（`gateway|worker|client` → B 的 CLI，其餘 → `llmop.cli.main`），必須有 `if __name__ == "__main__":`；`pyproject` 加 `secure` extra（`cryptography>=47`、`aiohttp>=3.9`、`websockets>=12`）、dev 相依（`pytest-aiohttp`、`pyhpke`）、`asyncio_mode = "auto"`、`requires-python = ">=3.11"`、entry point `llmop.__main__:main`；靜態 log 測試的掃描範圍限定 B 的模組。
- 驗收：`pytest -q` → 784 passed（含 `tests/test_bu_keep.py` 若存在）；`python -m llmop --version`、`llmop worker --help`、`llmop client --help` 正常。

**⏸ 等待 W0：dispatcher 確認後才進階段 1。**

## 階段 1 — 地基（4 個任務，可平行）

### T-1 設定
- 目標：`config.yaml` 支援 MVP 需要的全部設定，舊檔仍可載入。
- 擁有：`src/llmop/config.py`、`src/llmop/configedit.py`（新；把 `cli.py:251-510` 的保留註解編輯函式**複製**過來，不改 `cli.py`）、`config.example.yaml`、`src/llmop/examples/config.example.yaml`、`tests/test_config_v2.py`。
- 讀：TIERS 第 9 節；`MERGE_PLAN.md` 9.3、9.4。
- 內容：`roles`、`model`、`usage`、`tiers`（含預設值）、`queue`、`listeners`（三個入口的 port）、`tunnels`（`ui`、`api` 各一組，沿用 `TunnelSettings`）、`accounts.*.tier`、`accounts.*.max_api_keys`（預設 1）、`local_account`（本機免 key 對應的帳號）。沒寫的全部有預設值。
- 驗收：A 的舊 `config.example.yaml` 內容仍載入成功；每個新區段有載入與驗證錯誤測試；A 的測試全過。

### T-2 資料庫與用量
- 目標：統一的資料表與 TIERS 的用量模型。
- 擁有：`src/llmop/store.py`、`src/llmop/quota.py`、`src/llmop/usage.py`（新）、`src/llmop/security.py`、`src/llmop/auth.py`、`tests/test_usage_bucket.py`、`tests/test_low_state.py`、`tests/test_guest_keys.py`、`tests/test_store_migrate.py`。
- 讀：TIERS 第 2、3、4、8、11 節；B 的 `src/llmop/gateway/store.py`（workers 相關方法照搬）。
- 內容：`api_keys` 加 `sig_public_key`、`encryption`、`tier`、`budget_tokens`、`reset`、`label`，`username` 可空；新表 `workers`、`worker_keys`、`worker_models`、`buckets`（水位＋更新時間）、`api_link`（目前與上一個 gate 的雜湊、寬限到期）；`sessions` 加 `origin`；舊資料庫自動升級。`usage.py`：桶（回水、欠額下限、25% 回復）、每週表與週日重置、NORMAL↔LOW、GUEST 失效後仍可查。`quota.acquire` 改為依等級與兩個表判斷（VIP II／III 不擋）。`auth.py`：加 `key_allows_plaintext(key, account)`。
- 驗收：用假時鐘重現 TIERS 5.4 的三個數字（8 小時、15 分鐘、22.5 分鐘）；A v0.1 的資料庫檔升級後資料不變；A 的測試全過。

### T-3 排程器
- 目標：TIERS 第 7 節的排隊。純 asyncio 元件，不碰 HTTP。
- 擁有：`src/llmop/scheduler.py`（新）、`tests/test_scheduler.py`。
- 讀：TIERS 7.1–7.4。
- 內容：全域名額、六個順位、同級按帳號輪流、每帳號同時上限、每 6 筆保底 1 筆、`max_length`／`max_per_account`／`max_wait`、取消、`position()` 與 `eta()`、狀態快照。介面：`async with scheduler.slot(account, tier, zone) as ticket:`；等待逾時丟 `QueueTimeout(position, retry_after)`，滿了丟 `QueueFull(retry_after)`。
- 驗收：假時鐘測試涵蓋每條規則；1000 次隨機進出後計數歸零；取消等待者不漏名額。

### T-4 網路守門與 Windows 路徑
- 目標：判斷本機／外網的唯一實作；修 B 在 Windows 的寫檔問題。
- 擁有：`src/llmop/netguard.py`（新）、`src/llmop/paths.py`、`tests/test_netguard.py`、`tests/test_paths.py`。
- 讀：`MERGE_PLAN.md` 2.7、6.1 第 11 項；`LINEBotForChurch/src/church_bot/web/common.py:100`。
- 內容：`is_local(request)`（loopback 對端＋無轉送標頭＋`Host` 白名單＋`Origin` 檢查）；`check_public_host()`；`GateMiddleware`（路徑第一段不是目前或寬限期內的 gate → 404，並把 gate 從 `scope["path"]` 去掉）；`redact_path()`；`clean_log()`。`paths.write_json`：沒有 `os.fchmod` 時略過。
- 驗收：測試矩陣（loopback＋任一轉送標頭 → 外網；`Host: evil` → 拒絕；錯的 gate → 404；寬限期內舊 gate 可用）；monkeypatch 拿掉 `os.fchmod` 後寫檔成功。

**⏸ 等待 W1：四個任務合入、全套測試通過。**

## 階段 2 — 功能（4 個任務，可平行）

### T-5 加密中繼移植到 FastAPI
- 目標：B 的閘道功能變成 A 的 app 可掛載的 router。
- 擁有：`src/llmop/secure/**`（新）、`tests/secure/**`（新）。
- 不碰：`src/llmop/gateway/**`（留給 T-9 刪）、`app.py`、`protocol.py`、`crypto.py`。
- 讀：`src/llmop/gateway/{api,hub,worker_ws}.py`、`docs/secure/PROTOCOL.md` §3–4、`tests/gateway/test_api.py`、`test_worker_ws.py`。
- 內容：`build_secure_router(state)` 提供 `/e2e/v1/chat/completions`、`/e2e/v1/requests/{id}`、`/e2e/v1/models`、`/worker/ws`（FastAPI WebSocket）。key 與帳號用 T-2 的 store；准入用 T-2 的用量判斷；派工用 T-3 的排程器（取代 `8 × max_concurrency`）；`pending` 回應帶 `queue`；用量由 worker 回報後寫 `usage_events`（`usage_source="worker"`）。沒有 `sig_public_key` 的 key 一律拒絕。握手失敗加限流。
- 驗收：`tests/gateway/test_api.py`、`test_worker_ws.py`、`test_logging.py` 的每個情境在 `tests/secure/` 有對應並通過（交付報告附對照表）；用假 worker（FastAPI TestClient 的 websocket）跑通一筆。

### T-6 直連面
- 目標：`/v1` 接上單一模型、排隊、用量、入口政策。
- 擁有：`src/llmop/api/**`、`src/llmop/backends/**`、`tests/test_api_v2.py`、`tests/test_api.py` 與 `tests/test_backends.py`（只能新增）。
- 讀：TIERS 7.3–7.5；`src/llmop/api/proxy.py`、`api/routes.py`。
- 內容：`build_api_router(state, zone)`；`model` 一律覆寫為設定值，`/v1/models` 只回一筆；`asyncio.Semaphore` 換成排程器；`zone="public"` 預設不串流（`stream:true` 時回一次完整結果的單一 SSE chunk，或照 A 的設定可開）；`zone="local"` 免 key，對應 `local_account`；`zone="public"` 時 key 的 `encryption=required` → 403；排隊逾時回 429＋`Retry-After`＋雙語訊息；串流排隊送 `: queue …` 註解；`/v1/me` 加等級、狀態、兩個表、排隊資訊；請求本文上限 4 MiB（先看 `Content-Length`，再邊讀邊計）；後端 context 超限 → 400 `context_length_exceeded`；後端錯誤細節只進 log。
- 驗收：A 的 40＋21 個測試原樣通過；新增測試涵蓋上述每一項。

### T-7 用戶端外掛
- 目標：外掛能連上新的路徑，並補上本機代理的防護。
- 擁有：`src/llmop/client/**`、`tests/client/**`。
- 不碰：`worker/**`、`protocol.py`、`crypto.py`。
- 讀：`src/llmop/client/{sdk,proxy,config}.py`；`MERGE_PLAN.md` 6.2 第 1 項。
- 內容：`gateway_url` 可含 gate 路徑；API 路徑改 `/e2e/v1/*`；key 前綴接受 `sk-llop-` 與 `llmop_sk_`；`pending` 的 `queue` 資訊轉成對應用程式的保活註解；代理加固：`Host` 必須是 `127.0.0.1|localhost|[::1]:<port>`、`Content-Type` 必須是 `application/json`、有 `Origin` 或 `Sec-Fetch-Site: cross-site` 就 403。
- 驗收：既有用戶端測試通過（路徑更新）；新增：錯的 `Host` → 403、`text/plain` → 415、帶 `Origin` → 403、gate 路徑正確組合。

### T-8 網頁
- 目標：本機控制台與外部入口網頁。
- 擁有：`src/llmop/web/**`、`tests/test_web_v2.py`、`tests/test_web.py`（只能新增）。
- 讀：`MERGE_PLAN.md` 9.1、9.3、2.7；TIERS 第 10 節；B 的 `src/llmop/gateway/admin.py` 與 `static/`（worker 管理的畫面邏輯）。
- 內容：`build_web_router(state, surface)`。`surface="public"`：登入、入口網頁（等級、狀態、兩個用量表、自己的 key：建立／撤銷／`encryption` 選項／貼簽章公鑰、每把 key 用量、目前 API 連結、排隊狀況）；管理資料只有 GET 且遮蔽；**不註冊任何管理寫入路由**；無 playground。`surface="local"`：上面全部＋帳號與等級、發 GUEST key、worker 清單與註冊碼、名額與後端、通道狀態與「更換 API 連結」（呼叫 `state.api_link.rotate(...)`）、補滿某帳號的桶、原始設定檔編輯（用 T-1 的 `configedit`）。所有外來字串用 `textContent`。
- 驗收：A 的 45 個 web 測試原樣通過；測試列舉 `surface="public"` 的全部路由，確認沒有管理寫入、對管理路徑送 POST／PUT／DELETE 得到 404／405；秘密欄位不出現在任何回應；無 CDN、無 CSP 錯誤。

**⏸ 等待 W2：四個任務合入、全套測試通過。**

## 階段 3 — 組裝（1 個任務）

### T-9 組裝、通道、CLI
- 目標：一個指令把整個節點跑起來。
- 擁有：`src/llmop/app.py`、`src/llmop/node.py`（新）、`src/llmop/apilink.py`（新）、`src/llmop/tunnel.py`、`src/llmop/cli.py`、`src/llmop/__main__.py`、`tests/test_node.py`、`tests/test_api_link.py`、`tests/test_cli.py` 與 `tests/test_tunnel.py`（只能新增）；刪除 `src/llmop/gateway/**` 與 `tests/gateway/**`（確認 `tests/secure/` 已涵蓋後）。
- 讀：`MERGE_PLAN.md` 9.3、9.4、2.7；第 3 節的共同約定。
- 內容：`create_app(state, surface)` 產生三個 app（`local`：web(local)＋api(local)；`public-ui`：web(public)；`public-api`：`GateMiddleware`＋api(public)＋secure）；`build_state` 建立 `scheduler`、`usage`、`api_link`。`node.py`：在同一個 event loop 起三個 uvicorn（`local` 關 `proxy_headers`）、依 `roles` 起 B 的 worker、兩個 `TunnelManager`（互不影響）、啟動時檢查名額與後端平行數並警告。`apilink.py`：產生 gate、只換路徑或連主機名一起換（先起新通道再關舊的）、寬限期、稽核紀錄。通道拒絕指向 8781 與後端 port。CLI：`llmop serve` 起整個節點；`llmop tunnel rotate-api`；`llmop init` 產生新格式設定；`llmop gateway run|init` 為別名；worker／client 子指令照舊。
- 驗收：測試「啟動 → 本機免 key 請求 → 公開帶 key 請求 → 乾淨關閉」；更換 API 連結後舊 gate 404、key 仍可用、UI 通道物件未被重啟；`public-ui` 沒有 `/v1`，`public-api` 沒有登入頁與 cookie；A 的 CLI 與 tunnel 測試原樣通過。

**⏸ 等待 W3。**

## 階段 4 — 端到端與打包（2 個任務，可平行）

### T-10 端到端測試
- 目標：真的把三個角色在 loopback 跑起來驗證。
- 擁有：`tests/e2e/**`（`test_docs.py` 除外）。
- 讀：`tests/e2e/harness.py`、`conftest.py`；T-9 的 `node.py`。
- 內容：harness 改為啟動統一後的節點（uvicorn）＋真的 B worker＋真的用戶端代理＋假後端；保留「錄下閘道所有位元組，確認沒有對話內容」；保留簽章錯、釘選不符、重放、過期的拒絕測試；`test_e2e_quota_defects.py` 改寫為「並發下超出幅度有上界」；新增：經假 cloudflared（從 loopback 連入並帶 `Cf-Connecting-Ip`）對本機控制台的每個寫入路徑發請求，全部被拒；`encryption=required` 的 key 走明文被拒；GUEST key 用完後 401 且入口頁仍顯示。
- 驗收：全部通過；交付報告列出 B 原有 e2e 情境的對照。

### T-11 打包與文件
- 目標：可安裝、可照文件架起來。
- 擁有：`pyproject.toml`、`scripts/**`、`README.md`、`docs/HANDOFF.md`、`docs/DEPLOYMENT_GUIDE.html`、`docs/secure/**`、`tests/e2e/test_docs.py`、`tests/test_cli_packaging.py`。
- 不碰：`docs/plan/**`、`docs/BU_Keep_*`、`docs/MERGE_DECISIONS.html`、`docker/**`、`.github/**`。
- 內容：版本 0.2.0；`scripts/build_portable.py` 納入 `secure` 的 Windows wheel，並**比對寫死的 Python 壓縮檔 sha256**；`.bat`：`Start.bat`（整個節點）、`Start-Worker.bat`、`Start-Client-Proxy.bat`、`Rotate-API-Link.bat`；README 一頁講清楚：架設（直連／加密）、三個入口、怎麼發 key、SYNOX 的三種接法、資料夾含私鑰的警告；`docs/secure/` 對齊 `/e2e/v1` 與 gate。
- 驗收：`python -m build` 產出 wheel；`python scripts/build_portable.py` 產出 zip 且含 `cryptography`、`aiohttp`；README 裡的每個指令都存在（沿用 B 的 `--help` 檢查）；「安全邊界」段落在 README 與 `docs/secure/SECURITY.md` 一字不差。

**⏸ 等待 W4。**

## 階段 5 — 複查（1 個任務）

### T-R 獨立複查（另一個 Opus，沒寫過上面任何任務）
- 目標：找出 bug 與安全問題，確認真的能用。
- 擁有：`tests/security/**`（新）、`docs/security/REVIEW_v0.2.md`（新）。小修可直接改並列出；大修寫進報告交 dispatcher 再派。
- 讀：`MERGE_PLAN.md` 第 6 節（資安檢查表）與第 10 節；TIERS；本檔第 2、3 節。
- 內容：
  1. 讀完整個 `src/llmop`，列出正確性問題（並發、資源外洩、錯誤處理、Windows 相容）。
  2. 逐項走資安檢查表 6.1，每一項標「已驗證／有洞／不適用」並補測試；6.2 的 8 個洞必須逐一確認已修（用戶端代理防護、本文大小上限、`Host` 檢查、本機判斷、`proxy_headers`、`image_url`、供應鏈雜湊、錯誤外流；其中 `image_url` 若未做，列為 v0.3 並在 README 註明）。
  3. 冒煙測試：`python -m build` → 全新 venv 安裝 wheel（含 `[secure]`）→ 假後端 → `llmop init` → `llmop serve` → 本機控制台建立帳號 → 帳號網站登入 → 建立 key → 經 gate 送 `/v1/chat/completions` → `/v1/me` 與入口網頁看到用量 → 更換 API 連結後舊連結 404。再跑一次加密路徑：`llmop worker init/run`、`llmop client keygen`、建立 `encryption=required` 的 key、經代理送一筆。
- 驗收：全套測試通過；報告含問題清單（嚴重度、檔案行號、是否已修）與冒煙測試的實際指令與輸出。

**⏸ 最後：交擁有者在 Windows 上實測（真的 LLM、真的 cloudflared），並照 TIERS 第 6 節量出字速度校正數字。**

## 任務總覽

| 階段 | 任務 | 可平行 |
|---|---|---|
| 0 | T-M0 | — |
| 1 | T-1、T-2、T-3、T-4 | 是 |
| 2 | T-5、T-6、T-7、T-8 | 是 |
| 3 | T-9 | — |
| 4 | T-10、T-11 | 是 |
| 5 | T-R | — |

風險最高：**T-5**（移植安全關鍵的閘道，情境不能少）、**T-9**（三個入口＋兩個通道＋worker 在同一個 process）、**T-2**（改到 A 的額度核心，221 個測試要維持）。
