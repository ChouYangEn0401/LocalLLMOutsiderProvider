# 在新電腦接手這個專案（給你和你的 agent）

> v0.2.0 交付時寫的。交付共五個檔案：三個交付物（bundle、免安裝版 zip、whl）加兩份文件（本文件與 HANDOFF.md），見下方「檔案對照」。

## 三個交付物各是什麼

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

## 先看這裡：agent 會問你的事，先回答好

接手的 agent 通常有自己的全域規則（下載來的東西不執行、對外公開要先問、合併一律 `--no-ff`…）。這份交接需要它做三類動作，**每一類你要明確授權，它才會動手**：

| 動作 | 風險 | 建議授權 |
|---|---|---|
| 從 bundle 還原 repo、在虛擬環境 `pip install` 並跑 `pytest`（預期 1282 個通過） | 低，這是你自己的專案 | 直接授權 |
| 解壓 zip、執行 `.bat`、連 Ollama／LM Studio（只開 127.0.0.1） | 中，zip 內含下載來的 Python 與未在 Windows 驗證過的 `.bat` | 在 Windows Sandbox／虛擬機做，或你明說「這是我自己建的，照跑」 |
| cloudflared 公開到網際網路、`git push`、建 Release、改預設分支 | 高，對外且難收回 | **每一步都要你當場點頭**，agent 先列出要做什麼 |

最簡單的做法：在 prompt 最前面加一行授權（擇一）：

- 保守（建議）：`授權範圍：只做還原 repo、虛擬環境跑 pytest、更新 HANDOFF.md；zip、Ollama、cloudflared 只整理成清單給我，由我手動做（見 docs/HumanTestList.md）。`
- 放行本機：`授權範圍：這個專案是我自己的，zip 與 .bat 照跑，只在 127.0.0.1 開埠、測完關掉；cloudflared 公開那段等我同意。`

## 檔案對照（避免對不上）

你會拿到的東西共五個：三個交付物＋兩份文件。

| 檔案 | 是什麼 |
|---|---|
| `LocalLLMOutsiderProvider-dev.bundle` | 原始碼＋完整 git 歷史，只有 `dev` 分支 |
| `llmop-0.2.0-windows-x64-portable.zip` | 免安裝版，內含 Python 3.12，28 MB |
| `localllm_outsider_provider-0.2.0-py3-none-any.whl` | Python 安裝檔 |
| `AGENT_SETUP.md`、`HANDOFF.md` | 本文件與交接文件（repo 內 `docs/` 也有） |

**這個專案不屬於 SYNOX。** 請還原到**新的獨立資料夾**，不要放進 SYNOX 專案裡。

## 直接貼給 agent 的指示（Claude Code 等）

先把授權那一行貼在最前面。還原 repo、在專案資料夾裡開 agent，然後貼：

```
你要接手 LocalLLMOutsiderProvider (llmop) v0.2.0。先讀 README.md、docs/HANDOFF.md
（第 3.3 節「必須在 Windows 上實測的事」就是待驗證清單）、docs/security/REVIEW_v0.2.md、
docs/HumanTestList.md。

規則：
- 在 dev 分支工作。合併到 main 一律用 `git merge --no-ff`（不要 `git push origin dev:main`
  這種直接推），不要 force push。
- docs/BU_Keep_* 和 docs/BU_Keep.sha256 是凍結檔，永遠不要修改、重新命名或刪除
  （tests/test_bu_keep.py 會檢查）。要改規則請另開新檔。
- config.yaml 和 data/ 含密碼雜湊與私鑰，絕對不要 commit 或貼出來。
- 任何 push、建 Release、改預設分支、開 cloudflared，每一步先告訴我要做什麼，等我說「做」。
  推送前先掃整棵樹與所有 commit 訊息，確認沒有 key、密碼、私人資訊，作者是 noreply 信箱。
- 決策表在 docs/plan/MERGE_PLAN.md 第 10 節：標「擁有者已決定」的不要改，標「AI預設」的
  是暫定，要改先問我。
- 需要我親手做的事（點瀏覽器、用手機行動網路連線、確認 cloudflared）請列成「請你做」清單，
  我沒回報就不要當作通過。失敗先告訴我原因，不要大改。

第一件事（依我的授權範圍）：
1. 從 bundle 還原：`git clone -b dev LocalLLMOutsiderProvider-dev.bundle <新資料夾>`，
   建虛擬環境、`pip install -e ".[dev,secure]"`、跑 `python -m pytest -q`，預期 1282 個通過。
2. 把結果寫進 docs/HANDOFF.md 第 3.3 節。
3. 把 zip、Ollama／LM Studio、cloudflared 的實測依 docs/HumanTestList.md 整理成「請你做」，
   我做完會回報結果。
```

### 實測通過後，再貼這段（發佈）

```
首次實測已通過。做收尾與發佈，每一步先說明再執行：
1. 在本機 `git switch main`（沒有就從 dev 建立），`git merge --no-ff dev`，
   commit 訊息寫清楚做了什麼、為什麼。推上去前等我同意。
2. GitHub 預設分支改成 main：有 `gh` 就 `gh repo edit --default-branch main`（等我同意），
   沒有就告訴我去 Settings → Branches 手動改。
3. 修 CI 與 Docker：ci.yml 裝 `.[dev,secure]`、Python 矩陣 3.11 以上；release.yml 要裝
   `[secure]` 並建 wheel、sdist 與免安裝版 zip；docker/ 對應 v0.2 的三個入口。只改能驗證的，
   其餘在 HANDOFF.md 註明。
4. 打 tag v0.2.0、建立 GitHub Release 並附 zip、wheel、sdist：這是對外公開，等我說「推」。
```

## 還沒驗證、請在第一次實測時留意

- `.bat` 啟動器在真的 Windows cmd 上的互動流程（模型名稱有空白會壞）。
- 真的 Ollama／LM Studio、真的 cloudflared（含 `TUNNEL_TOKEN` 環境變數）。
- 兩台機器之間的 wss（遠端 worker），以及換 API 連結時舊連線被切斷。
- LM Studio／llama.cpp 會把 context 平分給同時的請求，名額開 6 可能出現「Context size has been exceeded」（見 docs/plan/TODO.md T1）。
