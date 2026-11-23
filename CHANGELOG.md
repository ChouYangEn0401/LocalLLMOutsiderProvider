# Changelog

## 0.3.0a4（2026-11-23，預發布）

- `llmop serve` 的啟動畫面不再印完整的 API 連結：只印遮罩版（主機名＋秘密路徑前 3 個字＋`…`，例如
  `https://<api 主機>/k7Q…/v1`），下一行提示 `Full link : llmop tunnel show-api`。API 通道拿到網址時印的那一行也一樣。
  主控台的輸出常被存成紀錄檔（例如 `serve.log`），秘密路徑不再留在裡面。`llmop tunnel show-api`、`rotate-api` 照舊印完整連結。
- 啟動畫面不再印本機免金鑰 `/v1` 那一行（入口照樣在；節點與 v0.1 單一入口模式都是）。本機控制台、帳號網站、通道主機名、
  設定檔位置、怎麼停止照舊。
- 節點的帳號網站通道（`ui`）橫幅不再印 `API base : <網址>/v1`：帳號網站本來就沒有 `/v1`。v0.1 單一通道照舊。
- 新增 `llmop.apilink.mask_api_link()` 與 `ApiLink.masked_url()`；測試確認啟動畫面、通道回呼與紀錄都不含秘密路徑。

## 0.3.0a3（2026-11-23，預發布）

- `llmop` 指令在不能顯示中文的主控台（例如英文版 Windows 的 cp1252）印出替代字，不再當掉（`keygen` 以前會失敗）。
- 測試：client CLI 的子程序固定用 UTF-8，Windows CI 恢復全綠。

## 0.3.0a2（2026-11-23，預發布）

- SDK：API 連結結尾的 `/v1` 會自動去掉（OpenAI 相容工具習慣這樣寫；以前會變成 `/v1/v1/models`，真機實測踩到）。

## 0.3.0a1（2026-11-23，預發布）

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
