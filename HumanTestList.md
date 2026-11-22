# HumanTestList（人要親手做的）

## 待測

- [ ] 2026-11-19 · v0.2.0 Windows 真機實測（本機、LM Studio context、對外公開、加密）· 照 [docs/HumanTestList.md](docs/HumanTestList.md) 逐項做 · 每項回報通過／失敗＋遮掉 key 的 log
- [ ] 2026-11-23 · SDK 經真的通道連線（另一台電腦，只裝 `[sdk]`）· `llmop client enroll --account-url https://<帳號網站> --user <帳號> --fingerprint <指紋> --profile home --test` → `llmop client self-check --profile home` → 伺服器 `llmop tunnel rotate-api` 後 SDK 回「API 連結已更換」、`llmop client set-link` 後恢復 · enroll 印「已釘選」與測試回覆；self-check 全 PASS；換連結前後行為如上
- [ ] 2026-11-23 · SDK 在 Windows 上收緊私鑰檔權限 · 在檔案總管對 `%USERPROFILE%\.llmop\client\home\signing_key.json` 按右鍵 → 內容 → 安全性 · 只看到自己的帳號（沒有 Users、Authenticated Users）
- [ ] 2026-11-19 · 發佈前決定「AI預設」決策（D2、D6、D9、D15、D20～D26）· 看 docs/plan/MERGE_PLAN.md 第 10 節 · 沒意見就照預設

## 測過了

（尚無）
