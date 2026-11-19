# 在新電腦接手這個專案（給你和你的 agent）

> v0.2.0 交付時寫的。你拿到三個檔案：`LocalLLMOutsiderProvider-dev.bundle`、`llmop-0.2.0-windows-x64-portable.zip`、`localllm_outsider_provider-0.2.0-py3-none-any.whl`。

## 三個檔案各是什麼

| 檔案 | 是什麼 | 什麼時候用 |
|---|---|---|
| `…-dev.bundle` | 整個專案的原始碼加完整 git 歷史（`dev` 分支）。**備份**，也能還原成可繼續開發的 repo | 要繼續開發、或 GitHub 上的東西不見時 |
| `llmop-0.2.0-windows-x64-portable.zip` | Windows 免安裝版，內含 Python。**要架設服務就用這個** | 在有 Ollama／LM Studio 的 Windows 電腦上實際跑 |
| `…whl` | Python 套件安裝檔（需要已有 Python 3.11+）。zip 裡已經有，一般不用另外裝 | 想用 `pip install` 的時候 |

GitHub 上也有同樣的東西：https://github.com/ChouYangEn0401/LocalLLMOutsiderProvider 的 `dev` 分支。有網路時直接 clone 它比較簡單；bundle 是它不見時的保險。

## 還原成可開發的 repo

```bash
git clone -b dev LocalLLMOutsiderProvider-dev.bundle LocalLLMOutsiderProvider
```

（一定要加 `-b dev`，bundle 裡沒有預設分支。）想接回 GitHub：

```bash
cd LocalLLMOutsiderProvider
```

```bash
git remote set-url origin https://github.com/ChouYangEn0401/LocalLLMOutsiderProvider
```

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -e ".[dev,secure]"
```

```bash
.venv/Scripts/python -m pytest -q
```

預期 1282 個測試通過。（Linux／macOS 把 `.venv/Scripts` 換成 `.venv/bin`。）

## 直接貼給 agent 的指示（Claude Code 等）

先把 repo 還原好、在專案資料夾裡開 agent，然後貼這一段：

```
你要接手 LocalLLMOutsiderProvider (llmop) v0.2.0。先讀 README.md，再讀 docs/HANDOFF.md
（狀態、已知缺口、待驗證項目）和 docs/security/REVIEW_v0.2.md。

規則：
- 在 dev 分支工作，不要動 main。
- docs/BU_Keep_* 和 docs/BU_Keep.sha256 是凍結檔，永遠不要修改、重新命名或刪除；
  tests/test_bu_keep.py 會檢查。要改規則請另開新檔。
- config.yaml、data/ 資料夾含密碼雜湊與私鑰，絕對不要 commit 或貼出來。
- 不確定的決策標成「AI預設」，不要替我決定；決策表在 docs/plan/MERGE_PLAN.md 第 10 節。

第一件事：幫我在這台 Windows 電腦上做首次實測，並回報結果：
1. 解壓 llmop-0.2.0-windows-x64-portable.zip，雙擊 Start.bat，確認第一次啟動流程、
   本機控制台 http://127.0.0.1:8781 可以開。
2. 接上我本機的 Ollama 或 LM Studio，用 Check.bat 或 `llmop check` 確認連得到。
3. 本機免 key 呼叫 /v1/chat/completions 一次；建一個帳號、登入帳號網站、建 key、
   用 key 經 API 入口打一次。
4. 裝好 cloudflared（winget install --id Cloudflare.cloudflared），跑
   Start-With-Tunnel.bat，用手機行動網路連公開網址；再跑一次 Rotate-API-Link.bat
   確認舊網址失效、新網址可用。
5. 量一個對話每秒出幾個 token，填進 docs/plan/TIERS_AND_USAGE_RULES.md 第 6 節的方式
   校正用量數字（先回報，不要直接改凍結檔，凍結檔在 docs/BU_Keep_TIERS_...）。
回報時列出：每一步通過或失敗、錯誤畫面或 log（遮掉 key 與秘密路徑）。失敗先不要大改，
先告訴我原因。
```

## 還沒驗證、請在第一次實測時留意

- `.bat` 啟動器在真的 Windows cmd 上的互動流程（模型名稱有空白會壞）。
- 真的 Ollama／LM Studio、真的 cloudflared（含 `TUNNEL_TOKEN` 環境變數）。
- 兩台機器之間的 wss（遠端 worker），以及換 API 連結時舊連線被切斷。
- LM Studio／llama.cpp 會把 context 平分給同時的請求，名額開 6 可能出現「Context size has been exceeded」（見 docs/plan/TODO.md T1）。
