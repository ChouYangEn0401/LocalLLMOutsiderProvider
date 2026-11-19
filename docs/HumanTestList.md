# 人工實測清單（這些要「你本人」做）

> agent 不能替你做、也不能當作你做過的事。每做完一項就打勾，把結果（通過／失敗＋畫面或 log，遮掉 key 與秘密路徑）貼給 agent，由 agent 更新 `docs/HANDOFF.md` 第 3.3 節。**沒回報的項目一律視為「未通過」。**

## 準備

- [ ] 一台 Windows 電腦，已裝 Ollama 或 LM Studio 並載好一個模型。
- [ ] zip 解壓到**沒有空白、沒有中文**的路徑，例如 `C:\llmop`。
- [ ] 想用 Windows 沙箱（Windows Sandbox）或虛擬機先試也可以；不想在主機上跑未驗證的 `.bat` 就這樣做。

## A. 只在本機（不開網路，風險最低）

1. - [ ] 雙擊 `Start.bat`。第一次會問 LLM 種類、模型名稱、admin 密碼。→ 回報：有沒有出錯、模型名稱有空白會不會壞。
2. - [ ] 瀏覽器開 `http://127.0.0.1:8781`，用 admin 登入。
3. - [ ] `Check.bat`：能連到 Ollama／LM Studio？
4. - [ ] 本機免 key 呼叫一次（PowerShell 範例在 README「快速開始」）。非串流、串流各一次。
5. - [ ] 建一個一般帳號 → 到帳號網站 `http://127.0.0.1:8780` 登入 → 建 key → 用 key 打 API 入口（`:8782`，路徑要帶秘密路徑，`llmop tunnel show-api` 看）。
6. - [ ] 量速度：一個對話每秒出幾個 token（Ollama：`ollama run <模型> --verbose`；LM Studio：聊天視窗的 tok/s）。回報數字。

## B. LM Studio 的 context 問題（TODO T1）

7. - [ ] 同時開兩個對話各送一個長一點的請求。有沒有出現 `Context size has been exceeded`？
8. - [ ] 把 `config.yaml` 的 `queue.slots` 調成 LM Studio 的並行數（通常 1～2），重試。回報結果。

## C. 對外公開（會把服務放到網際網路，**你確定要開才做**）

9. - [ ] 安裝 cloudflared：`winget install --id Cloudflare.cloudflared`。
10. - [ ] 雙擊 `Start-With-Tunnel.bat`，記下印出的兩個 trycloudflare 網址（帳號網站、API 連結）。
11. - [ ] **用手機行動網路（不要連家裡 Wi-Fi）**：開帳號網站、登入、建 key；用 key 打 API 連結一次。
12. - [ ] 從手機試著開本機控制台的任何管理頁（用帳號網站的網址加 `/ui/admin`）：應該什麼都改不了。
13. - [ ] 雙擊 `Rotate-API-Link.bat`：舊的 API 連結應該 404、新的可用、帳號網站不受影響。
14. - [ ] 完成後關掉視窗，確認通道已停止（Task Manager 沒有 cloudflared）。

## D. 加密路徑（選做）

15. - [ ] `Start-Client-Proxy.bat`、建 `encryption=required` 的 key、經代理送一筆。
16. - [ ] 第二台電腦跑 `Start-Worker.bat`（遠端 worker）註冊成功。

## 回報格式

```
項目 N：通過 / 失敗
畫面或 log：（遮掉 key 與秘密路徑）
備註：
```
