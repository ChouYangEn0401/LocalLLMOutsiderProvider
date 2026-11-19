# 交接：MVP（LM Studio + Cloudflare 通道）與「LLM 後端」面板（2026-11-19）

## 做了什麼
- `config.yaml`（不進 git）第一個後端改成 `type: lmstudio`、`base_url: http://localhost:1235`、`models: ["*"]`；頂層 `model: qwen3.5-9b@q6_k`。`llmop check` 全綠（LM Studio 回報 6 個模型）。
- 已安裝 cloudflared（winget，位置 `C:\Program Files (x86)\cloudflared\cloudflared.exe`；新開的終端機才會在 PATH 裡）。
- 本機控制台新增「LLM 後端」區塊（只在本機控制台、管理員、本機登入的工作階段）：
  - `GET /ui/api/admin/llm-backend`：目前類型、網址、模型。
  - `GET /ui/api/admin/llm-backend/models?type=&base_url=`：列模型（OpenAI `/v1/models`；LM Studio 另查 `/api/v0/models` 標出已載入；Ollama 用 `/api/tags`）。逾時 4 秒，連不上回 502 與中文原因。網址只接受 http(s)、不可含帳密／查詢字串。
  - `POST /ui/api/admin/llm-backend`：寫入第一個後端的 type／base_url 與（可選）頂層 `model`，保留註解，驗證後存檔並熱載入。`model` 空白＝沿用目前設定。
  - 程式：`src/llmop/backends/probe.py`、`configedit.set_llm_backend`、`web/console.py`、`web/static/admin.{html,js}`。
- README 與 `llmop init` 完成提示更新（LM Studio 埠號以其顯示為準、`--backend-url`、面板用法）。

## 目前狀態
- 本機 `POST http://127.0.0.1:8781/v1/chat/completions` 已實測可回答（模型 `qwen3.5-9b@q6_k`；回應含 `reasoning_content`，max_tokens 要給夠）。
- **通道尚未開**：開工時已有一個 `llmop gateway run`（沒開通道）佔著 8780／8781／8782。要加通道必須重啟它，但這個動作被權限系統擋下（不擅自結束擁有者的行程）。需要擁有者（或主 session）停掉舊行程後照下面「啟動」開。
- **重要**：`.venv` 裡的 llmop 是**一般安裝（非 editable）**，site-packages 是舊程式碼，新面板不會出現，直到重新安裝：`.venv\Scripts\pip install -e .`（或 `pip install .`）。測試用 `src/` 跑，所以測試是綠的。

## 啟動／停止／紀錄
啟動（脫離終端機，紀錄寫進 `data\`）：
```powershell
$env:Path += ";C:\Program Files (x86)\cloudflared"
Start-Process -FilePath .venv\Scripts\llmop.exe -ArgumentList "serve","--tunnel" -WindowStyle Hidden -RedirectStandardOutput data\serve.log -RedirectStandardError data\serve.err.log -PassThru
```
停止：`Stop-Process -Id <PID>`（同時結束它的 cloudflared 子行程；用 `Get-CimInstance Win32_Process | ? CommandLine -match llmop` 找）。`data\tunnel*.lock` 是 OS 檔案鎖，檔案存在不代表有行程，不要手動刪。
API 連結與帳號網站網址（會變、含秘密路徑，**不寫在文件裡**）：看本機控制台 `http://127.0.0.1:8781/` 的「通道與 API 連結」，或 `llmop tunnel show-api`。

## 這次採用的決定（擁有者已同意預設值）
1. 不推到 GitHub，只做本機 `dev` commit。
2. 「啟用」只寫設定，靠 LM Studio 的即時載入（第一個請求較慢）；不呼叫 `lms load`／LM Studio REST 載入（之後可加）。
3. `server.public_stream` 維持 false（quick tunnel 的 SSE 不穩）；本機 `/v1` 仍可串流。
4. 模型 `qwen3.5-9b@q6_k`、後端 `http://localhost:1235`。
5. 清歷史的交接（清私有字詞／filter-repo／force push）延後，未做。
6. 自動定期更換 API 連結（本機控制台管理）：只換秘密路徑，不換主機名。quick tunnel 的主機名在 cloudflared 重啟時仍會變，所以週期只有在具名通道（named tunnel）下才準。設定（週期、寬限期）與下次更換時間都存在 DB（`kv`），儲存設定、延期、任何一次更換都會重算 `next_rotation_at`；節點每 60 秒檢查，關機錯過期限會在啟動後一分鐘內補換。

## 驗證結果
- 單元測試：`tests/test_web_v2.py`、`tests/test_config_v2.py` 新增 7 個測試（列模型、錯誤、權限／公開入口無此路由、寫入設定保留註解、configedit 設定器）全過；兩檔共 94 過、1 敗。
- 全套：1126 過、14 敗、145 錯，皆非本次改動造成：`test_raw_config_editing`（Windows CRLF）、`test_paths`／worker 檔案權限（Windows 沒有 0600）、`tests/client|e2e|worker` 缺 `aiohttp_server` fixture（沒裝 pytest-aiohttp）。
- 對真的 LM Studio（1235）：`probe.list_models` 列出 6 個模型並標出 `qwen3.5-9b@q6_k` 已載入。
- **未做**：面板 HTTP 端點對活的服務、以及透過公開連結的 e2e——因為服務還沒用新程式碼重啟、通道沒開。瀏覽器 UI 沒實際點過（沒有 node 可檢查 JS 語法）。

## 已知問題／下一步
1. 停掉舊 serve → `pip install -e .` → 用上面指令啟動 → 讀 `data\serve.log` 取得連結。
2. 建一把測試 key（`llmop` 的 key 建立路徑見 `cli.py`／控制台），用公開連結 `POST <API 連結>/v1/chat/completions` 測，之後撤銷。
3. 面板實測：列模型 → 再啟用 `qwen3.5-9b@q6_k`。
4. 模型選單目前會把嵌入模型（embedding）也列出來。
5. 可考慮：啟用後自動請 LM Studio 載入模型。
