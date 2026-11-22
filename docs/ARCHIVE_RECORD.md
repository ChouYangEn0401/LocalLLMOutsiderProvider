# 歸檔紀錄：LocalLLMOutsiderProvider（llmop）v0.2.0

- 歸檔日期：2026-11-19
- 範圍：只做歸檔（還原 repo、收檔、算 sha256、寫紀錄、隱私掃描、本機 commit）。**沒有開發、沒有測試、沒有推送。**
- 歸檔時的 HEAD：`e05bbbc`（`dev`），從交付的 bundle 還原。

## 1. 前因後果（精簡）

1. 2026-11-13：v0.1（A 版）——單一程式的直連閘道：帳號、API key、token 額度、OpenAI 相容 API、cloudflared 快速通道、Windows 打包。
2. 同期另有獨立實作（B 版，分支 `claude/lm-studio-e2e-encryption-ug09c3`）：gateway／worker／client 三角色、端對端加密（HPKE）、
   worker 主動連出、簽章與防重放。與 A 版沒有共同祖先，只共用套件名 `llmop`。
3. 2026-11-18～19：比對、決策、合併成 v0.2.0（`dev`）。一個程式三個入口（本機控制台 `127.0.0.1:8781`、帳號網站 8780、
   API 入口 8782＋秘密路徑）、兩條獨立通道、每把 key 可選加密、等級與兩個用量表、優先排隊、外網唯讀。
   雲端環境回報 1282 個自動測試通過，並用假後端跑過直連與加密流程。**本次歸檔沒有重跑測試。**

決策表：`docs/plan/MERGE_PLAN.md` 第 10 節（「擁有者已決定」不改；「AI預設」為暫定，待擁有者檢視）。
凍結檔 `docs/BU_Keep_*`、`docs/BU_Keep.sha256` 未動。

## 2. 檔案收在哪裡

`dist/` 已被 `.gitignore` 排除，不進版控。

| 檔案 | 位置（相對 repo 根目錄） | 大小（bytes） | sha256 |
|---|---|---|---|
| `LocalLLMOutsiderProvider-dev.bundle`（head `e05bbbc`） | `dist/` | 778322 | `c8f2c56eabd2fd51920ebae7451a4d2d762d3e7b1bd393397433b13020dc157c` |
| `llmop-0.2.0-windows-x64-portable.zip`（未解壓、未執行） | `dist/` | 28723986 | `a63f14df3a3aa7954b54ccfdb8e4f81b80c93ac144e5095b7268c0a5f3377524` |
| `localllm_outsider_provider-0.2.0-py3-none-any.whl`（未安裝） | `dist/` | 247056 | `84d11a82fd08fb53c8e8ab82ccb94aba3ebf33543c17510051f76d7a0af4dd26` |

`docs/` 內的 `HANDOFF.md`、`AGENT_SETUP.md`、`HumanTestList.md`、`ARCHIVE_PROMPT.md` 與另外收到的同名檔內容相同（只差換行字元），沒有覆蓋。
`plan/`、`security/`、`secure/`、`BU_Keep_*`（3 個）、`BU_Keep.sha256` 都在。

## 3. GitHub 現況（依交付說明，本次未連線確認）

- `dev` 與 `claude/youthful-galileo-u66ybv`：v0.2.0（`e05bbbc`）。
- `claude/lm-studio-e2e-encryption-ug09c3`：B 版原樣。
- 沒有 `main`；預設分支仍指向 B 版那條（雲端 session 建 main 時被權限擋下）。

## 4. 沒做／沒驗證

- 沒在真的 Windows 上跑過免安裝版 zip 與 `.bat`；沒接過真的 Ollama／LM Studio／cloudflared。
- 延後到 v0.3：加密模式真串流、worker 改用統一轉接層、worker／用戶端設定併入 `config.yaml`、無縫換鑰、前向保密、區網入口、
  唯讀儀表板分享連結、`image_url` 的 SSRF 防護（R-07）、Docker 與 CI 的 v0.2 更新。
- TODO T1（後端 context 超限）只做了錯誤碼轉換與名額警告。
- 出字速度（30 token/秒）是估計值，沒有量過。

## 5. 留給別台電腦（這台不做）

1. `pip install -e ".[dev,secure]"` 後跑 `python -m pytest -q`，預期 1282 個通過。
2. Docker：`docker/` 與 `.github/` 仍是 v0.1 版本，要在有 Docker 的電腦上更新並驗證。
3. Windows 實測：照 `docs/HumanTestList.md`（A 本機、B context、C 對外公開、D 加密）。
4. 合併到 main（`git merge --no-ff dev`）、改預設分支、打 tag `v0.2.0`、建 GitHub Release：擁有者說「推」才做。

## 6. 需要外部專案配合的待辦（SYNOX 端）

- H-S1：SYNOX 的 local 後端加可選的 API key 標頭。
- H-S2：確認 SYNOX 的 OpenAI 相容後端送出的標頭與是否串流。
- H-S3（可選）：把 llmop 用戶端 SDK 做成 SYNOX 的 adapter。

已在 SYNOX 的 `docs/handoff/` 開一張交辦單記錄（只動文件）。

## 7. 隱私掃描（歸檔時）

- 所有 commit 的作者與提交者都是 `noreply` 信箱；本機 `user.email` 也是 GitHub noreply。
- 整棵樹搜尋本機路徑、個人信箱、私鑰標頭、真實 key 格式：只有測試裡的假 key（`tests/test_netguard.py`）。
- 留下的問題：`docs/ARCHIVE_PROMPT.md` 與部分計畫文件提到其他專案名稱，且已推到公開 repo；是否清理由擁有者決定。
