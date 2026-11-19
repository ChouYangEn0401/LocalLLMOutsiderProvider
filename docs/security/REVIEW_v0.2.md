# llmop v0.2 獨立複查報告（T-R）

> 2026-11-19。複查者：沒寫過任何 T-M0～T-11 任務的 Opus。分支 `dev`（未 commit，交 dispatcher）。
> 依據：`docs/plan/DISPATCH.md` 第 1–3 節與 T-R 簡報、`MERGE_PLAN.md` 第 6 節（27 類攻擊）與第 10 節（決策表）、
> `TIERS_AND_USAGE_RULES.md`、`TODO.md`（T1）、`docs/HANDOFF.md`。前面執行者的報告一律當成「聲明」，逐項回到程式碼驗證。

## 1. 結論

**可以給少數信任的人試用（直連與加密兩條路都實際跑通），沒有發現高風險漏洞。** 外網唯讀（D4）、秘密路徑、`Host`
檢查、本機判斷、本文大小上限、錯誤訊息遮蔽、用戶端代理防護都經過測試與實機確認是有效的。

正式開給親友之前，建議先修以下兩項（都是「規則沒照 TIERS 生效」，不是安全漏洞，但會讓使用者體驗與文件不符）：

1. **R-04**：範例設定的 v0.1 `default_quota`（每日 20 萬 token、每月 300 萬、每分鐘 20 次、同時 2 筆、單筆 4096）仍然對每個帳號生效，
   VIP III 實際只能同時 2 筆、VIP I 每天最多 20 萬 token，與 TIERS 第 2.3 節不符。
2. **R-03**：帳號層級的 `max_concurrent` 覆寫只寫進設定物件，排程器完全沒用到。

其餘 Medium（R-08 換連結後舊 WebSocket 不斷線／遠端 worker 斷線、R-09 加密路徑不強制單一模型、R-10 外網可建立 VIP key）
屬於「設計上要決定」的事，可以先在文件註明再排進 v0.3。

## 2. 問題清單

嚴重度：High＝外人可直接造成損害；Medium＝需要 key／帳號，或與定案規則明顯不符；Low＝小瑕疵或縱深防禦；Info＝資訊。

| # | 嚴重度 | 位置 | 問題 | 狀態 |
|---|---|---|---|---|
| R-01 | Low（測試基礎） | `src/llmop/tunnel.py:446` `_publish` | 先設 `self.url` 再寫資料庫與網址檔；`tests/test_tunnel.py::test_ui_and_api_tunnels_are_independent` 在 `wait_for(url)` 後立刻讀檔，偶發 `FileNotFoundError`（那次 flaky 的原因）。節點等新通道網址時也可能讀到尚未寫入的狀態 | **已修**：先寫入、再設 `url`。測試 `tests/security/test_sec_controls.py::test_tunnel_url_is_visible_only_after_it_is_persisted`；原測試連跑 20 次全過 |
| R-02 | Low | `src/llmop/worker/link.py:203` `_describe` | worker 重連失敗時，aiohttp 的錯誤訊息含完整 `gateway_url`，**秘密路徑被寫進 worker 的 log**（實機 worker2.log 出現 3 次） | **已修**：網址第一段路徑換成 `***`。測試 `test_worker_reconnect_log_does_not_contain_the_gate` |
| R-03 | Medium | `src/llmop/scheduler.py:335` `_eligible`；呼叫端 `api/proxy.py:232`、`secure/api.py:314` | `accounts.*.max_concurrent` 覆寫被解析進 `Account.limits`，`Admission.max_concurrent` 也算對了，但排程器只看 `tiers.<tier>.max_concurrent` | **未修**。建議：`Scheduler.slot(..., max_concurrent=None)` 存進 `Ticket`，`_eligible` 優先用它；兩個呼叫端傳 lease 的值（LOW 時仍用 1）。`tests/security/test_sec_known_gaps.py` 有 strict xfail |
| R-04 | Medium | `config.example.yaml:68-74`（及 `src/llmop/examples/`）；`src/llmop/quota.py:197` | v0.1 `default_quota` 仍全數生效（見結論）。`max_concurrent` 還把**排隊中**的請求算進去（lease 在排隊前取得），所以 NORMAL 帳號的第 3 筆直接 429 而不是排隊 | **未修**。建議：範例的 `default_quota` 只留 `requests_per_minute`，其餘設 0；有用量表（`tables`）時 `QuotaManager.acquire` 不再檢查 legacy `max_concurrent`（交給排程器）。strict xfail 已加 |
| R-05 | Low | `api/routes.py:107`、`web/routes.py:237`、`protocol.py:137`、`client/proxy.py:143` | 巢狀過深的 JSON 讓 `json.loads` 丟 `RecursionError`，回 500（檢查表 15 要求 400） | **已修**：當成 JSON 格式錯誤回 400。測試 `test_deep_json_is_400_not_500` |
| R-06 | Low（縱深防禦） | `src/llmop/web/console.py:232` | 本機控制台的原始設定檔 GET（含密碼雜湊、後端 key、通道 token）只要求 admin session，不要求「在控制台建立的 session」；帳號網站在 `127.0.0.1` 建立的 cookie 會被帶到 8781。外網無法觸及（已驗證），但與「寫入要本機 session」不一致 | **已修**：`origin == "remote"` 的 session 一律 403。測試 `test_raw_config_needs_a_session_made_on_the_console` |
| R-07 | Medium（接受） | `api/proxy.py`（未過濾） | `image_url` 原樣轉給後端，會自己抓網址的後端（vLLM 等）可被當成 SSRF 跳板（6.2-f） | **未修（v0.3）**；已在 `README.md`「一定要知道的限制」註明。測試 `test_readme_documents_image_url_ssrf_as_v03` |
| R-08 | Medium（營運）／Low（安全） | `src/llmop/netguard.py` `GateMiddleware`、`secure/hub.py` | 更換 API 連結後，**已連線的 WebSocket 不會被切斷**（實測：經舊 gate 連上的 worker 換連結後仍繼續服務）；而遠端 worker 的 `gateway_url` 含 gate，**下次重連就 404 無限重試**，只在 worker 端 log 看得到 | **未修**。安全面：連線仍需 worker token，舊 gate 只是門牌，風險低。建議：`WorkerConn` 記下連入時的 gate 雜湊，`rotate` 後切斷用舊 gate 連入的連線；控制台在換連結前列出遠端 worker 並提醒要改設定（或 v0.3 給 worker 一條不輪替的路徑） |
| R-09 | Medium | `src/llmop/secure/api.py` 步驟 7、`list_models` | 加密路徑無法改寫 `model`（標頭有簽章），閘道也**沒有拒絕**與 `config.model` 不同的模型；worker 公告什麼模型，加密使用者就能用什麼（`expose_models` 預設空＝全部） | **未修**。標頭閘道看得到，可以強制：`config.model` 有設時 `header.model != config.model` 回 `model_unavailable`、`/e2e/v1/models` 只列這一個；文件要求 worker 設 `expose_models: [<model>]` |
| R-10 | Medium | `src/llmop/web/routes.py:727` `api_keys_create` | 檢查表 20 計劃「VIP 沒有額度上限 → VIP 的 key 只能本機發」，目前帳號網站任何等級都能建 key。VIP 密碼經 Cloudflare 外洩時，外人可自建無上限的 key | **未修**。建議：`surface="public"` 時 vip2／vip3（或 vip1 以上）回 403「請在本機控制台建立」，或由擁有者決定接受 |
| R-11 | Low | `src/llmop/worker/config.py:50` | `llmop worker init` 產生的 `gateway_url` 是 B 的舊位址 `ws://127.0.0.1:8787/worker/ws`，v0.2 沒有東西聽 8787 | **未修**。改成 `ws://127.0.0.1:8781/worker/ws`，並印出「遠端請用 `wss://<API 連結>/worker/ws`」 |
| R-12 | Low | `config.example.yaml`（`queue.slots: 6` 對 `backends[].max_concurrency: 2`）；`api/proxy.py:236` | 全新安裝每次啟動都出現名額警告；有排程器時後端自己的 semaphore 被略過（`gate=False`），6 筆會同時打到設定為 2 的後端 | **未修**。`llmop init` 讓兩個數字一致，或有排程器時仍保留後端 semaphore |
| R-13 | Low | `src/llmop/app.py:81` `make_scheduler` | 排程器（名額、各等級同時上限、排隊上限）只在啟動時建立；設定檔熱重載（控制台說「已存檔並生效」）對這些值無效 | **未修**。設定變更時更新排程器的數值，或在控制台註明需重啟 |
| R-14 | Low | `src/llmop/scheduler.py` `Ticket.tier` | TIERS 11「排隊中帳號變成 LOW → 那筆移到 LOW 的順位」沒做；排隊的票等級固定 | **未修**（v0.3） |
| R-15 | Low | `src/llmop/api/proxy.py:759` `_StreamRelay.close` | 串流在第一個 token 前被取消時記 0 token（連提示都不記）；大提示＋馬上斷線可以不付費地佔用 GPU（只影響本機或開了 `public_stream` 的入口） | **未修**。上游已開啟（`self.upstream` 非 None）時至少記 `prompt_estimate` |
| R-16 | Low | uvicorn/h11 | 同時有 `Content-Length` 與 `Transfer-Encoding: chunked` 時以 chunked 為準（RFC 允許），不是檢查表 9 計劃的「一律 400」；重複的 `Content-Length` 會 400。前面有 cloudflared 正規化 | **接受** |
| R-17 | Info | `src/llmop/app.py:272` `/health`；登入頁的 `?v=0.2.0` | 公開入口回版本號（檢查表 23 說外網不回版本） | 未修；建議公開面的 `/health` 只回 `{"status":"ok"}` |
| R-18 | Low | `web/console.py` | D7「管理員可代發」只做了 GUEST key；沒有替一般帳號發 key 的功能 | 未修（決策落差，可接受或補一個本機路由） |
| R-19 | Low | `client/sdk.py:133` | 換連結後用戶端代理回「閘道回了無法解析的回應（HTTP 404）」，使用者看不出是 API 連結換了 | 未修；建議 404 時提示「API 連結可能已更換，請到帳號網站查看」 |
| R-20 | Info | `src/llmop/tunnel.py:243` | named tunnel 的 token 放在 cloudflared 的命令列參數，本機的行程清單看得到 | 未修；建議改用 `TUNNEL_TOKEN` 環境變數 |
| R-21 | Low | `src/llmop/api/tokens.py` `text_chars` | 約 900 層的巢狀 `content` 陣列能通過 `json.loads`，但估算 token 的遞迴會 `RecursionError` → 500（已在 `forward` 內接住，不漏名額） | 未修；改成迭代或限制深度 |

統計：High 0；Medium 6（R-03、R-04、R-07〔接受、已記文件〕、R-08、R-09、R-10）；Low 13（其中 R-01、R-02、R-05、R-06 已修）；Info 2。
本次修改 5 處（R-01、R-02、R-05、R-06 程式，R-07 README 一句）。

### 2.1 我修改的檔案

| 檔案 | 行 | 原因 | 測試 |
|---|---|---|---|
| `src/llmop/tunnel.py` | 446–465 | R-01：先持久化再公開 `url` | `test_tunnel_url_is_visible_only_after_it_is_persisted`；原 flaky 測試 20/20 |
| `src/llmop/worker/link.py` | 198–205 | R-02：遮蔽 log 裡網址的 gate | `test_worker_reconnect_log_does_not_contain_the_gate` |
| `src/llmop/api/routes.py` | 107 | R-05 | `test_deep_json_is_400_not_500` |
| `src/llmop/web/routes.py` | 237 | R-05 | 同上 |
| `src/llmop/protocol.py` | 137 | R-05（`/e2e` 與 worker 訊框） | 同上 |
| `src/llmop/client/proxy.py` | 143 | R-05（用戶端代理） | — （同一行為，代理已有其他測試） |
| `src/llmop/web/console.py` | 233–240 | R-06 | `test_raw_config_needs_a_session_made_on_the_console` |
| `README.md` | 「一定要知道的限制」 | R-07（6.2-f 要求 README 註明） | `test_readme_documents_image_url_ssrf_as_v03` |

## 3. 開發期間特別提出的疑慮（逐一確認）

| 疑慮 | 結果 |
|---|---|
| 本機控制台的原始設定 GET 會回秘密 | 屬實（設計如此）。**外網碰不到**：public-ui、public-api（有無 gate）都 404；本機入口帶任何轉送標頭（10 種全測）、外來 `Host`、外來 `Origin`、`Sec-Fetch-Site: cross-site` 一律 403；實機用 `Cf-Connecting-Ip` 打也是 403。另補 R-06 |
| 秘密路徑出現在 log／錯誤／Referer／轉址／帳號網站 | 伺服器端：只在啟動橫幅（給擁有者看）與已登入使用者的入口網頁 JSON（D22 的設計）出現；存取紀錄與 uvicorn 的 WebSocket 紀錄都是 `/***/`（實機 serve.log 只有橫幅 1 次）；錯 gate 回固定的 `Not Found`；`/v1/...` 的錯誤訊息用的是去掉 gate 的路徑；網頁全面 `Referrer-Policy: no-referrer`；所有轉址是固定的 `/ui/...`。worker 端：R-02（已修） |
| 換 gate 後已連線的 WebSocket 不會斷 | 屬實，見 R-08。安全上 Low（仍需 worker token），營運上 Medium |
| 帳號層級 `max_concurrent` 沒進排程器 | 屬實，R-03（另發現 R-04 的 legacy 上限） |
| 遠端 worker 的 `gateway_url` 含 gate，換連結後壞掉 | 屬實，實機重現（重連 404 無限重試），R-08 |
| 公開 `/v1` 是否遵守 `encryption=required` | 是：帳號 key 與 GUEST key 在 `/v1/chat/completions`、`/v1/models` 都 403 `encryption_required`（測試＋實機） |
| GUEST key 直連與 `/e2e` 是否一致（`guest:<key_id>`） | 一致：兩條路的用量紀錄與排隊身分都用 `auth.usage_username` → `guest:<key_id>`；用量表的主體是 `key:<key_id>`（另一張表，刻意不同）。測試 `test_guest_key_has_one_identity_on_direct_and_encrypted_paths` |
| 加密路徑無法強制單一模型 | 改寫做不到，但**拒絕**做得到（標頭閘道看得到），見 R-09。目前要靠 worker 的 `expose_models` 只列一個模型 |
| `test_ui_and_api_tunnels_are_independent` 偶發 `FileNotFoundError` | 原因是 R-01 的競態（測試在背景執行緒寫檔之前就讀檔）；已修程式，連跑 20 次全過 |

## 4. 資安檢查表（MERGE_PLAN 6.1，27 類）

測試都在 `tests/security/`（`sl` = `test_sec_listeners.py`，`sc` = `test_sec_controls.py`）。

| # | 攻擊 | 結論 | 證據 |
|---|---|---|---|
| 1 | 注入 | 已驗證 | SQL 全部參數化（逐一讀過 `store.py`）；YAML `safe_load`；cloudflared 以 argv 啟動。`sc::test_cloudflared_is_started_without_a_shell`、`sl::test_login_is_throttled_and_injection_safe`。附註 R-20 |
| 2 | 路徑穿越 | 已驗證 | `sl::test_static_path_traversal` |
| 3 | CSRF | 已驗證 | 網頁：`sl::test_csrf_and_origin_on_the_account_website`；用戶端代理：`sc::test_client_proxy_guard` |
| 4 | XSS | 已驗證 | 靜態 JS 沒有任何 HTML sink、嚴格 CSP：`sc::test_web_pages_have_no_html_sinks`、`sl::test_clickjacking_and_csp_headers` |
| 5 | 繞過登入 | 已驗證 | `sl::test_public_ui_has_no_admin_write_route`、`sl::test_api_key_never_opens_the_web_ui`（另有 `tests/test_web_v2.py` 的路由列舉） |
| 6 | 偷登入 | 已驗證 | 只存雜湊：`sc::test_sessions_and_keys_are_stored_hashed`；外網 session 不能寫、（R-06 後）也不能讀原始設定 |
| 6b | 本機其他程式偷 cookie | 接受（文件已說明） | — |
| 7 | 暴力猜 | 已驗證 | 登入限流 `sl::test_login_is_throttled_and_injection_safe`；worker 握手限流 `sc::test_worker_handshake_throttle` |
| 8 | 重放 | 已驗證 | `sc::test_worker_replay_guard`；閘道去重見 `tests/secure/` |
| 9 | 請求走私 | 部分 | 重複 `Content-Length` → 400（實機）；CL＋TE 以 TE 為準（R-16，接受） |
| 10 | 塞爆 | 已驗證（有但書） | `/v1` 4 MiB、帳號網站 1 MiB、`/e2e` 與整個 public-api 16 MiB，`Content-Length` 與 chunked 都擋：`sl::test_public_ui_host_check_and_body_limit`、`sl::test_public_api_gate_host_and_body_limits`；WebSocket 16 MiB（uvicorn 預設）；佇列上限見 `tests/test_scheduler.py`。uvicorn 沒設 `limit_concurrency`（前面有 cloudflared 200 並發上限） |
| 11 | DNS rebinding | 已驗證 | `sl::test_local_console_dns_rebinding_and_cross_origin`、public 兩個入口的 421、用戶端代理 403 |
| 12 | SSRF | 有洞（接受，v0.3） | 後端網址只能本機改、tunnel 拒絕指向 8781 與後端 port（`tests/test_tunnel.py`）；`image_url` 未過濾（R-07，README 已註明） |
| 13 | 開放轉址 | 已驗證 | `sl::test_redirects_are_fixed_internal_paths`；`login.js` 只接受 `/ui/[a-z]+` |
| 14 | 點擊劫持 | 已驗證 | `sl::test_clickjacking_and_csp_headers` |
| 15 | 不安全的還原 | 已驗證（R-05 已修） | `sl::test_deep_json_is_400_not_500`；協定拒絕重複鍵與 NaN（`tests/secure/`） |
| 16 | 檔案上傳 | 不適用 | 沒有上傳；原始設定 PUT 只在本機、512 KB 上限、先驗證再寫 |
| 17 | 供應鏈 | 部分 | Python 壓縮檔 sha256 寫死且不符就停：`sc::test_portable_build_pins_and_checks_the_python_hash`；Python 套件沒有雜湊鎖定、CI 沒有 `pip-audit`（v0.3） |
| 18 | 提示注入 | 不適用 | llmop 不執行工具 |
| 19 | AI 冒充人 | 已驗證 | `sl::test_api_key_never_opens_the_web_ui` |
| 20 | 帳號被盜 | 部分 | 外網唯讀已驗證；VIP key 可在外網建立（R-10） |
| 21 | 臨時網址被濫用 | 已驗證 | gate 404、Host 421、公開面無寫入、tunnel 不能指向控制台 |
| 22 | 中間的人 | 接受 | 文件已寫明 |
| 23 | 錯誤訊息洩漏 | 已驗證 | `sl::test_backend_error_text_never_reaches_a_public_client`（實機：後端 500 帶內部網址 → 用戶端只看到固定句子）；`/health` 回版本號（R-17，Info） |
| 24 | 紀錄被塞假資料 | 已驗證 | `sc::test_redact_path_and_clean_log`、`sc::test_public_api_access_log_never_contains_the_gate`、`sc::test_websocket_handshake_log_line_is_masked` |
| 25 | 試算表公式注入 | 不適用 | 沒有匯出 |
| 26 | 同一台電腦的別人 | 部分（接受） | POSIX 0600：`sc::test_gate_file_is_private_and_only_hashes_in_the_database`；Windows 無 ACL；用戶端代理沒有本機權杖 |
| 27 | 惡意工具程式 | 不適用 | 沒有外掛；惡意 worker 由用戶端釘選公鑰防範 |

### 4.1 MERGE_PLAN 6.2 的 8 個洞

| 洞 | 狀態 | 證據 |
|---|---|---|
| (a) 用戶端本機代理沒有防護 | 已修 | Host／Origin／Sec-Fetch-Site／Content-Type 與只綁 loopback：`sc::test_client_proxy_guard`；實機帶 `Origin` → 403 |
| (b) 請求本文沒有上限 | 已修（全部公開路徑） | `/v1` 4 MiB、`/e2e` 16 MiB、帳號網站 1 MiB、WebSocket 16 MiB；實機 5 MB（CL 與 chunked）→ 413、17 MB `/e2e` → 413 |
| (c) 沒有 `Host` 檢查 | 已修 | 三個入口＋用戶端代理；實機 421／403 |
| (d) 本機判斷只看一個標頭 | 已修 | 10 種轉送標頭逐一測試 403 |
| (e) `proxy_headers` | 已修 | 本機入口關閉：`sc::test_local_listener_runs_without_proxy_headers` |
| (f) `image_url` SSRF | **未修 → v0.3** | README 已註明（R-07） |
| (g) 免安裝版供應鏈 | 已修（Python 壓縮檔）；套件雜湊鎖定未做 | `sc::test_portable_build_pins_and_checks_the_python_hash` |
| (h) 後端錯誤原樣外流 | 已修 | `sl::test_backend_error_text_never_reaches_a_public_client` |

## 5. 正確性複查（並發、資源、錯誤處理、Windows）

- **排程器**（`scheduler.py`）：取消等待者會出隊、取消時剛好被授予名額會釋放，計數正確；沒有發現漏名額的路徑。
- **用量結算**：直連非串流在 `_forward` 的 `except BaseException` 與 `_complete` 都走同一個冪等的 `_Call.finish`；排隊中就結束的不記用量、
  釋放 lease；串流斷線由 `_RelayResponse.__call__` 的 `finally` 收尾（冪等）。加密路徑的 `Hub.finish` 保證 `settle` 只呼叫一次。
  沒有發現重複扣款或永不結算；唯一偏差是 R-15。
- **lease 與排隊的順序**：lease 在排隊前取得，所以 legacy `max_concurrent` 會把排隊中的請求也算進去（R-04）。
- **熱重載**：帳號與等級數字跟著設定檔即時生效（用量表每次讀設定），但排程器不會（R-13）。
- **Windows**：`os.fchmod` 兩處都有防護；`fcntl`／`msvcrt` 分流；`SIGBREAK` 有處理；`SO_REUSEADDR` 在 Windows 不設；`tzdata` 有打包。
  沒有實機驗證（見第 8 節）。

## 6. 冒煙測試（實際執行）

環境：Linux、Python 3.13、全新 venv（`$S/review_venv`，`$S` = scratchpad），假的 OpenAI 相容後端在 `127.0.0.1:18900`。
三個入口改用 18781／18780／18782 避免和其他程式衝突。

```text
$ .venv/bin/python -m build --outdir $S/dist
Successfully built localllm_outsider_provider-0.2.0.tar.gz and localllm_outsider_provider-0.2.0-py3-none-any.whl
$ python3 -m venv $S/review_venv
$ $S/review_venv/bin/pip install "$S/dist/localllm_outsider_provider-0.2.0-py3-none-any.whl[secure]"
$ $S/review_venv/bin/llmop --version
llmop 0.2.0
$ LLMOP_HOME=$T/home llmop init --config $T/config.yaml --admin-password '…' \
      --backend-type openai --backend-url http://127.0.0.1:18900 --model smoke-model
[OK] created …/config.yaml
$ llmop serve --config $T/config.yaml --no-tunnel
WARNING: queue.slots is 6 but the backends accept only 2 request(s) at once …   ← R-12
  Console   : http://127.0.0.1:18781/   (this computer only)
  API link  : http://127.0.0.1:18782/fu3Z…/v1   (tunnel 'api'; keep it private)
```

直連路徑（`smoke.py`，stdlib HTTP；每行是實際的狀態碼與回應摘要）：

```text
[console login] HTTP 200: {"ok": true, "username": "admin", "role": "admin"}
[console GET raw config] HTTP 200
[console PUT raw config (add alice)] HTTP 200: {"ok": true}
[account site login] HTTP 200: {"ok": true, "username": "alice"}
[account site create key] HTTP 201: {"record": "sk-llop-...73HA", "prefix": "sk-llop-"}
[public /v1/chat/completions] HTTP 200: {"model": "smoke-model", … "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
[public backend error (sanitized?)] HTTP 502: {"error": {"message": "模型伺服器發生錯誤 (the model server returned an error).", … "code": "backend_error"}}
[public /v1/me] HTTP 200: {"tier": "normal", "status": "NORMAL", "bucket": 79982.3, "weekly_used": 18}
[portal key usage] HTTP 200: [{"all": {"requests": 2, "total": 18}, "week": {"requests": 2, "total": 18}}]
[public without gate] HTTP 404: Not Found
[local key-less /v1] HTTP 200: {"model": "smoke-model", "usage": {…"total_tokens": 18}}
[console config via forwarded header] HTTP 403: local console only
[raw config on public-ui] HTTP 404
$ llmop tunnel rotate-api --config … --reason smoke   →  [OK] new API link
[old gate after rotation] HTTP 404: Not Found
[new gate, same key] HTTP 200: {"object": "list", "data": [{"id": "smoke-model", …}]}
```

加密路徑：

```text
[console create worker] HTTP 201 (reg_token llmop_reg_…)
$ llmop worker init                      → 金鑰 key_id LE74tiEw8JzbTg--ER0hRQ；預設 gateway_url 是 ws://127.0.0.1:8787/worker/ws（R-11）
  （把 gateway_url 改成 ws://127.0.0.1:18782/<gate>/worker/ws、backends=openai@18900、expose_models=[smoke-model]）
$ llmop worker run --reg-token llmop_reg_…
  已向閘道註冊，worker_id=w_07d5cb31172c；已連上閘道：1 把金鑰，1 個模型
  （閘道紀錄：WebSocket /***/worker/ws [accepted]）
$ llmop client keygen                    → public_key fuzf5p…，sig_key_id WXfC0J…
[account site create encryption=required key] HTTP 201: {"encryption": "required"}
[required key on plaintext /v1] HTTP 403: … "code": "encryption_required"
$ llmop client proxy                     → 本機代理已啟動：http://127.0.0.1:18788/v1
[client proxy -> /e2e/v1 -> worker] HTTP 200: {"model": "smoke-model", "choices": [… "pong:secret hello" …], "usage": {… "total_tokens": 18}}
[client proxy with Origin] HTTP 403: … "forbidden_origin"
[portal after e2e] [["required", {"requests": 1, "total": 18}], ["optional", {"requests": 2, "total": 18}]]
grep -c "secret hello" serve.log worker.log proxy.log  →  0 0 0   （內容沒有進任何 log）
```

換連結對加密路徑的影響（R-08）：

```text
$ llmop tunnel rotate-api …
[client proxy with OLD gate after rotation] HTTP 502: 閘道回了無法解析的回應（HTTP 404）        ← R-19
[client proxy NEW gate, worker connected via OLD gate] HTTP 200   ← 舊 gate 的 WebSocket 沒有被切斷
（重啟 worker）WSServerHandshakeError: 404 … url='ws://127.0.0.1:18782/AOXz…/worker/ws'  ← 無限重試，且 gate 進了 log（R-02，已修）
```

另外對執行中的節點做的探測（curl／nc）：public-ui 與 public-api 外來 `Host` → 421；本機入口 `Host: evil.example:18781`、
`X-Forwarded-For`、外來 `Origin` → 403；`/v1` 5 MB（CL 與 chunked）→ 413；帳號網站 5 MB → 413；`/e2e` 17 MB → 413；
沒有 key 的 5 MB → 401（先驗證）；重複 `Content-Length` → 400；CL＋TE → 依 TE 處理（R-16）；public-ui 沒有 `/v1`、
public-api 沒有登入頁；深度 10 萬層的 JSON → 修正前 500（R-05），修正後 400。結束時所有背景行程都已停止。

## 7. 測試數

`tests/security/` 新增 43 個（41 通過＋2 個 strict xfail 記錄 R-03、R-04）。全套：**1216 passed → 1257 passed, 2 xfailed**
（`timeout 480 .venv/bin/python -m pytest -q`）。沒有刪改任何既有測試。

## 8. 沒有驗證到的事

- **Windows**：沒有 Windows 機器。免安裝版 zip、所有 `.bat`（`Start.bat`、`Start-Worker.bat`、`Start-Client-Proxy.bat`、`Rotate-API-Link.bat` 等）、
  `SIGBREAK`／關閉視窗、`msvcrt` 鎖、Windows 上的 `os.replace` 行為、`tzdata` 時區都沒跑過。
- **真的 LLM**：只用假後端。Ollama／LM Studio 的串流、`context_length_exceeded` 的實際錯誤格式、`queue.slots` 與後端平行數的互動沒驗證。
- **真的 cloudflared**：沒有連外。cloudflared 實際送的 `Host`、`X-Forwarded-For`／`Cf-Connecting-Ip`、quick tunnel 的 SSE 與 100 秒限制、
  `--change-host` 換主機名都只在假通道下測過。uvicorn 從 `X-Forwarded-For` 取的 IP 是否正是 Cloudflare 附加的那一個（影響登入限流）沒實測。
- **兩台機器**：遠端 worker 走 `wss://` 與 TLS 沒驗證。
- 這份報告不含效能或長時間穩定性測試。
