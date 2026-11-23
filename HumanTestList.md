# HumanTestList（人要親手做的）

## 待測

- [ ] 2026-11-19 · v0.2.0 Windows 真機實測（本機、LM Studio context、對外公開、加密）· 照 [docs/HumanTestList.md](docs/HumanTestList.md) 逐項做 · 每項回報通過／失敗＋遮掉 key 的 log
- [ ] 2026-11-23 · SDK 經真的通道連線（另一台電腦，只裝 `[sdk]`）· `llmop client enroll --account-url https://<帳號網站> --user <帳號> --fingerprint <指紋> --profile home --test` → `llmop client self-check --profile home` → 伺服器 `llmop tunnel rotate-api` 後 SDK 回「API 連結已更換」、`llmop client set-link` 後恢復 · enroll 印「已釘選」與測試回覆；self-check 全 PASS；換連結前後行為如上
- [ ] 2026-11-23 · SDK 在 Windows 上收緊私鑰檔權限 · 在檔案總管對 `%USERPROFILE%\.llmop\client\home\signing_key.json` 按右鍵 → 內容 → 安全性 · 只看到自己的帳號（沒有 Users、Authenticated Users）
- [ ] 2026-11-19 · 發佈前決定「AI預設」決策（D2、D6、D9、D15、D20～D26）· 看 docs/plan/MERGE_PLAN.md 第 10 節 · 沒意見就照預設
- [ ] 2026-11-23 · v0.3 兩台電腦真機驗證（A＝有模型的伺服器、B＝用 SDK／EasyLLM 的電腦）· 照 [docs/handoff/20261123-v0.3-real-machine.md](docs/handoff/20261123-v0.3-real-machine.md) 第 2、3 節逐步做 · 每一步「成功」那行都對得上，驗證清單全勾
- [ ] 2026-11-23 · 本機控制台「載入模型」按鈕（Ollama、LM Studio 各一次）· 管理員登入 →「LLM 後端」→ 列出模型 → 選未載入的 →「載入模型」· 出現「已載入：…（N 秒）」，再列一次時標「已載入」；vLLM 顯示「啟動時就決定模型」
- [ ] 2026-11-23 · `llmop audit` 在真的通道上 · 有模型的那台跑 `llmop audit exposure --public --with-key` 與 `llmop audit canary --scan serve.log` · 最後一行都是 `OK`；測試 key 都已撤銷、serve.log 刪掉
- [ ] 2026-11-23 · `llmop serve` 啟動畫面只印遮罩版 API 連結（0.3.0a4）· 有模型的那台 `llmop serve --tunnel 2>&1 | Tee-Object serve.log`，等 API 通道出現網址；另一個視窗 `llmop tunnel show-api`，把主機名後面那段秘密路徑在 serve.log 裡搜尋（`Select-String -SimpleMatch`）· 畫面上 API link 是 `https://<主機>/xxx…/v1`、下一行 `Full link : llmop tunnel show-api`、沒有本機 `/v1` 那行；秘密路徑在 serve.log 找不到；測完刪 serve.log
- [ ] 2026-11-23 · 新手教學能照著走完 · 找一台新電腦，照 docs/TUTORIAL.md 從頭走一遍（A 段免安裝版與 Python 版各一次、B 段明文與加密各一次），記下畫面跟教學寫的不一樣的地方 · 每一步「會看到什麼」都對得上；對不上的記下實際畫面（遮掉 key、密碼、秘密路徑）交回改教學；特別看 A8 啟動畫面各行出現的順序、A5 winget 第一次問條款的樣子

## 測過了

（尚無）
