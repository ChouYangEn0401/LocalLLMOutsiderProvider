# 歸檔 prompt（貼給擁有者電腦上的 agent）

> 用途：只做「歸檔收尾」，不開發、不測試。擁有者的這台電腦**不可以**跑大量測試或 Docker，那些是別台電腦的任務。

```
【任務】把 LocalLLMOutsiderProvider（簡稱 llmop）v0.2.0 這一輪工作「歸檔收尾」：把收到的檔案放到正確的地方、
確認完整、寫一份歸檔紀錄、回報。只做歸檔，不要開發、不要測試。

【這台電腦的硬性限制】
- 不要跑 pytest 或任何大量測試。不要跑 Docker。不要解壓後執行免安裝版 zip，不要執行任何 .bat。
- 不要啟動 Ollama、LM Studio、cloudflared，不要開任何埠。
- 不要 git push、不要建 Release、不要改 GitHub 預設分支。要推的話先列出來等我說「推」。
- 不要修改 SYNOX 與 LINEBotForChurch 的程式碼。
- 不要把 key、密碼、私鑰、config.yaml、data/ 放進任何 commit 或回報裡。
- 可以做的：git clone（從 bundle）、git bundle verify、複製檔案、算 sha256、讀文件、寫文件、本機 git commit。

【前因後果（請完整理解，這是你寫歸檔紀錄的依據）】
1. 2026-11-13：我在雲端 session 開了新專案 llmop（說明：Expose local LLM services to online users
   with lightweight session-based authentication for persistent access）。目標是把我家 Windows 上的
   Ollama／LM Studio／vLLM 透過 Cloudflare tunnel 分享給少數人用，前面加登入、API key、token 額度，
   API 跟 OpenAI 相容。做法參考了我另外兩個專案：LINEBotForChurch 的 trycloudflare 通道管理、
   SYNOX 的 LLM 轉接器設計。那個版本叫 v0.1（單一程式的「直連版」，簡稱 A 版）。
2. 同時，我在家裡另開了一個 session，獨立做出另一個實作（分支 claude/lm-studio-e2e-encryption-ug09c3，
   簡稱 B 版）：gateway／worker／client 三角色、端對端加密（HPKE）、worker 主動連出、簽章與防重放。
   兩邊沒有共同祖先，只是用了同一個套件名 llmop。
3. 2026-11-18～19：在雲端 session 把兩版比對、決策、合併。用多 agent 流程：Fable 規劃、Opus 5.5 實作、
   Sonnet 派工與驗收，最後再由獨立 Opus 全面複查並修正。結果是 v0.2.0，在 git 分支 dev：
   一個程式三個入口（本機控制台 127.0.0.1:8781、帳號網站 8780、API 入口 8782 加秘密路徑）、兩條獨立的
   cloudflared 通道、每把 key 可選加密、等級 VIP III/II/I／NORMAL／LOW／GUEST、1.5 小時與每週用量表、
   優先排隊、外網唯讀。1282 個自動測試通過；用假後端實際跑過完整流程（直連與加密）。
4. 沒做或沒驗證的（務必在歸檔紀錄寫清楚）：沒在真的 Windows 上跑過（.bat 與免安裝版 zip）、沒接過真的
   Ollama／LM Studio／cloudflared；加密模式真串流、worker 改用統一轉接層、設定併入 config.yaml、
   無縫換鑰、前向保密、區網入口、唯讀儀表板分享連結、image_url SSRF 防護、Docker 與 CI 的 v0.2 更新，
   都延後到 v0.3；TODO T1（LM Studio 的 Context size exceeded）只做了一半；出字速度（30 token/秒）是猜的。
5. GitHub（ChouYangEn0401/LocalLLMOutsiderProvider）現況：dev 與 claude/youthful-galileo-u66ybv 兩條
   分支是 v0.2.0（以我最新給的 bundle 為準，不會早於 829d403）；claude/lm-studio-e2e-encryption-ug09c3 是 B 版
   原樣；沒有 main；預設分支還指向 B 版那條。想建 main 的推送被雲端 session 的權限擋下，所以尚未做。
6. 決策：決策表在 docs/plan/MERGE_PLAN.md 第 10 節。標「擁有者已決定」的 13 項不要改；「AI預設」的 11 項
   是暫定（D2、D6、D9、D15、D20～D26 等），我之後會自己看。凍結檔 docs/BU_Keep_* 與
   docs/BU_Keep.sha256 永遠不要修改、重新命名、刪除。

【我給你的檔案，以及各自要收到哪裡】（通常在 Downloads，共 5～7 個）
| 檔案 | 是什麼 | 收到哪裡 |
|---|---|---|
| LocalLLMOutsiderProvider-dev.bundle | 原始碼加完整 git 歷史，只有 dev 分支。用我最新給的那份（head 不會早於 829d403） | 還原成「新的獨立 git repo」資料夾 LocalLLMOutsiderProvider（跟我其他專案同一層，**不要放進 SYNOX、不要放進 LINEBotForChurch**）；bundle 檔本身再備份一份到該 repo 的 dist/ 資料夾（dist/ 已被 .gitignore，不進版控） |
| llmop-0.2.0-windows-x64-portable.zip | Windows 免安裝版（28 MB，內含 Python） | 只複製到該 repo 的 dist/，**不解壓、不執行**；供之後在別台 Windows 電腦實測 |
| localllm_outsider_provider-0.2.0-py3-none-any.whl | Python 安裝檔 | 同上，只放 dist/，不安裝 |
| AGENT_SETUP.md、HumanTestList.md、HANDOFF.md | 接手說明、人工實測清單、交接文件 | 這三份已包含在 bundle 內的 docs/。若 bundle 裡的版本比我另外給的舊（例如沒有 HumanTestList.md），就用我給的覆蓋到 docs/ 並本機 commit 在 dev |
| （選用）ARCHIVE_PROMPT.md | 就是這份 prompt | 在 repo 的 docs/ 內，已含在 bundle 裡 |

【SYNOX 端要做的事（只做記錄，不改 SYNOX 程式碼）】
llmop 有三件事要 SYNOX 那邊配合，這是「待辦」，不是現在要做的：
- H-S1：SYNOX 的 local 後端加可選的 API key 標頭（直連最乾淨的作法）。
- H-S2：確認 SYNOX 的 openai_compat（ntu_easy_llm）送出的標頭與是否串流。
- H-S3（可選）：把 llmop 用戶端 SDK 做成 SYNOX 的 adapter，省掉另外跑本機代理。
請只在 SYNOX 專案「已經有的」交接或待辦文件裡追加一小段記錄這三項（如果沒有這種文件就不要建立，
改成寫進 llmop 的歸檔紀錄並在回報裡告訴我）。LINEBotForChurch 不需要改，它只是設計參考。

【步驟】
1. 找出檔案，列出每個的路徑、大小、sha256。
2. `git bundle verify` 檢查 bundle；`git clone -b dev <bundle> <新資料夾>`（一定要加 -b dev）；
   確認 `git log --oneline -1` 不早於 829d403，工作區乾淨。
3. 把 zip、whl、bundle 複製到新 repo 的 dist/（不要解壓、不要安裝）。
4. 確認 docs/ 內有：HANDOFF.md、AGENT_SETUP.md、HumanTestList.md、ARCHIVE_PROMPT.md、plan/、security/、
   secure/、BU_Keep_* 與 BU_Keep.sha256；缺的就用我給的補上。
5. 在新 repo 寫 docs/ARCHIVE_RECORD.md（繁體中文）：歸檔日期、上面「前因後果」的精簡版、各檔案收在哪裡
   與 sha256、GitHub 現況、沒做／沒驗證的清單、之後在別台電腦要做的事（見下）。只寫事實，不寫 key 與私人資訊。
   本機 git commit 在 dev（不要推）。
6. 若 SYNOX 有現成的交接／待辦文件，追加 H-S1～H-S3（只動文件）。
7. 掃描整個新 repo 與所有 commit 訊息：沒有 key、密碼、私鑰、個人資料；作者信箱是 noreply 或我設定的那個。

【回報格式】
- 每個檔案：收到哪裡（完整路徑）、sha256。
- 新 repo 的 HEAD、工作區是否乾淨、做了哪幾個本機 commit。
- 有沒有碰 SYNOX（碰了什麼）。
- 隱私掃描結果。
- 你遇到的疑問或你不確定的地方（不要自己猜）。

【留給「別台電腦」的任務（這台不做）】
1. `pip install -e ".[dev,secure]"` 後跑 `python -m pytest -q`，預期 1282 個通過。
2. Docker：v0.1 時期的 docker/ 與 .github/ 還沒為 v0.2 更新，要在有 Docker 的電腦上驗證。
3. Windows 實測（zip、.bat、Ollama／LM Studio、cloudflared、手機行動網路）：依 docs/HumanTestList.md。
4. 合併到 main（`git merge --no-ff dev`）、改預設分支、打 tag v0.2.0、建 GitHub Release：我說「推」才做。
```
