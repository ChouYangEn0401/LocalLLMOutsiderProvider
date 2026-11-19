# 交接：LM Studio + Cloudflare 通道 MVP（2026-11-19）

給接手的人。先看「現況」，要動手看「怎麼啟動／停止／更新」。指令細節見 [docs/CLI.md](../CLI.md)，總覽見 [README](../../README.md)。
本文不寫通道網址、秘密路徑、key；要用時看本機控制台或 `llmop tunnel show-api`。

## 現況

2026-11-19 驗證可用：

- 本機 `http://127.0.0.1:8781/v1` 可回答。
- 公開 API 連結經 Cloudflare quick tunnel，轉到 LM Studio `http://localhost:1235`，可回答。
- 模型：`qwen3.5-9b@q6_k`。
- 公開測試用的是一把臨時 GUEST key，測完已撤銷。

設定（`config.yaml`，不進 git）：

- 第一個後端 `type: lmstudio`、`base_url: http://localhost:1235`、`models: ["*"]`；頂層 `model: qwen3.5-9b@q6_k`。
- `llmop check` 全綠（LM Studio 回報 6 個模型）。
- `server.public_stream` 為 false（見「決策」）。

已完成的功能：

- 本機控制台「LLM 後端」面板：改類型／網址、列模型、選模型、啟用，熱載入並保留註解（`probe.py`、`configedit.set_llm_backend`、`web/console.py`、`web/static/admin.*`）。
- 本機控制台「API 連結自動更換」面板：週期、寬限期、延期、立即更換（`apilink.py`）。
- `docs/CLI.md` 與 README 已更新。

呼叫注意：模型是思考模型（回應含 `reasoning_content`）。`max_tokens` 要 ≥ 2000，或在 LM Studio 關掉 thinking，否則 `content` 可能是空的。

## 怎麼啟動／停止／更新

啟動（脫離終端機，紀錄寫進 `data\`）：

```powershell
$env:Path += ";C:\Program Files (x86)\cloudflared"
Start-Process -FilePath .venv\Scripts\llmop.exe -ArgumentList "serve","--tunnel" -WindowStyle Hidden -RedirectStandardOutput data\serve.log -RedirectStandardError data\serve.err.log -PassThru
```

- cloudflared 是 winget 裝的，位置 `C:\Program Files (x86)\cloudflared\cloudflared.exe`；新開的終端機才會自動在 PATH 裡。
- 前景跑也可以：`llmop serve --tunnel --open`。

停止：

```powershell
Get-CimInstance Win32_Process | ? CommandLine -match llmop
Stop-Process -Id <PID>
```

這也會結束它的 cloudflared 子行程。`data\tunnel*.lock` 是 OS 檔案鎖，不要刪。

更新程式碼後（`.venv` 內的 llmop 是一般安裝，不是 editable，site-packages 是舊碼）：

1. 先停掉 serve。serve 在跑時 `.venv\Scripts\llmop.exe` 被鎖，pip 會報 `WinError 32`，失敗可能留下 `ModuleNotFoundError: No module named 'llmop'`。
2. `.venv\Scripts\python.exe -m pip install -e .`
3. 用上面的指令啟動，再看 `data\serve.log`。

目前的連結：本機控制台 `http://127.0.0.1:8781/` 的「通道與 API 連結」，或 `llmop tunnel show-api`。

## 決策

1. 不推到 GitHub，只做本機 `dev` commit。
2. 「啟用」只寫設定，靠 LM Studio 的即時載入（第一個請求較慢）；不呼叫 `lms load` 或 LM Studio REST 載入。
3. `server.public_stream` 維持 false（quick tunnel 的 SSE 不穩）；本機 `/v1` 仍可串流。
4. 模型 `qwen3.5-9b@q6_k`、後端 `http://localhost:1235`。
5. 清歷史（清私有字詞、filter-repo、force push）延後，未做。
6. 自動更換 API 連結只換秘密路徑，不換主機名。設定（週期、寬限期）與下次更換時間存在資料庫（`kv`），不在 `config.yaml`。
   儲存設定、延期、任何一次更換都會重算 `next_rotation_at`；節點每 60 秒檢查，關機錯過期限會在啟動後一分鐘內補換。
   quick tunnel 的主機名在 cloudflared 重啟時仍會變，所以週期只有具名通道（named tunnel）下才準。

## 未驗證

- 兩個新面板（LLM 後端、API 連結自動更換）**還沒在瀏覽器點過**，也沒對活的服務測過。只有單元測試與對真 LM Studio 的 `probe.list_models`（列出 6 個模型並標出已載入的）。
- 瀏覽器端 JS 沒檢查過語法（沒有 node）。
- 目前執行中的 serve 還是**舊程式碼**，要重啟並重新安裝（見上）才會有新面板。
- 加密模式、Ollama、免安裝版 zip 在真機上都沒測過。

## 已知問題

全套測試（Windows）：1126 過、14 敗、145 錯；已確認與本次改動無關的有：

- `test_raw_config_editing`：Windows CRLF。
- `test_paths`、worker 檔案權限：Windows 沒有 0600。
- `tests/client`、`tests/e2e`、`tests/worker`：缺 `aiohttp_server` fixture（沒裝 pytest-aiohttp）。
- `test_bu_keep`、`test_core` 各 1 個失敗：**還沒查**。

CLI 怪癖（完整清單見 [docs/CLI.md](../CLI.md) 第 7 節）：

- `init` 只給 `--backend-url` 時類型視為 ollama；LM Studio 要加 `--backend-type lmstudio`。
- `init` 不測試後端網址，錯了要等 `llmop check`。
- `init` 只問 admin 密碼；問後端與模型的是免安裝版 `_first_run.bat`。
- 控制台模型選單會把 embedding 模型也列出來。

## 下一步

1. 停 serve → 重新安裝 → 啟動 → 在瀏覽器逐一點兩個面板：列模型、啟用 `qwen3.5-9b@q6_k`；設定週期、延期、立即更換。
2. 考慮改用具名通道（named tunnel），讓主機名固定、自動更換週期準確。
3. `llmop init` 應該探測 LM Studio 網址並提示埠號。
4. 考慮「啟用」之後自動請 LM Studio 載入模型（`lms load`）。
5. 查 `test_bu_keep`、`test_core` 各 1 個失敗；視需要補 pytest-aiohttp。
