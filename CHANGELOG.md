# Changelog

## 0.3.0.dev0（開發中）

### 新增
- **用戶端 SDK `llmop.sdk`**（[docs/SDK.md](docs/SDK.md)）：同步 `Client`＋`AsyncClient`、多個設定檔
  （`$LLMOP_HOME/client/<名稱>/`）、`chat()` 回 OpenAI chat.completion、`models()`、`me()`；
  transport `plain`／`e2e`／`auto`；e2e 沿用既有的 HPKE＋Ed25519。
- 錯誤一律 `SdkError(code, message, status, retry_after, kind_hint)`，kind_hint 分 quota、auth、rate、link_changed、
  offline、timeout、trust、bad_request、server、unknown。
- `llmop client` 新子指令：`enroll`（帳號密碼建 key、本機產生金鑰對只上傳公鑰、核對並釘選 worker 指紋）、`add-plain`、
  `profiles`、`set-link`、`pin`、`unpin`、`self-check`、`revoke`、`forget`。密碼與 key 只從終端機提示輸入。
- API 連結固定、不自動探索：秘密路徑回 404 時回 `link_changed`（「API 連結已更換，請向服務提供者拿新連結」），
  `set-link` 換新連結。
- 設定檔與私鑰檔收緊成只有本人：POSIX 0600／0700；Windows 用 icacls 移除繼承、只給目前使用者的 SID。

### 變更
- **套件拆分**：伺服器的相依（fastapi、uvicorn、PyYAML、tzdata）移到 `[server]`；新增 `[sdk]`（httpx＋cryptography）。
  伺服器安裝改成 `pip install "localllm-outsider-provider[server,secure]"`；免安裝版、`Setup.bat`、Dockerfile 已跟著改。
  `[dev]` 自帶 `[server]`。沒裝 `[server]` 時 `llmop serve` 印一行安裝提示。
- `import llmop.sdk` 不載入 fastapi、uvicorn、aiohttp；`llmop client` 的 v0.2 子指令（keygen、proxy、chat、models）
  只在執行時才載入 aiohttp。

### 不變
- v0.2 的單一設定檔 `$LLMOP_HOME/client/config.json` 當成 `default` 設定檔照讀；`llmop client proxy` 照舊可用。
- 伺服器端的 API、協定、資料庫都沒有改。

## 0.2.0

v0.2 的內容見 [docs/HANDOFF.md](docs/HANDOFF.md)。
