# 新手教學：把自己電腦上的模型分享給朋友，或使用朋友分享的模型

這份教學寫給**沒什麼電腦概念的人**。照著一步一步做，每一步都寫了「要打什麼」「會看到什麼」「不是這樣怎麼辦」。
以 **0.3.0a4**（預發布版）為準。每個指令的完整說明在 [docs/CLI.md](CLI.md)，SDK 在 [docs/SDK.md](SDK.md)，
加密模式在 [docs/secure/DEPLOY.md](secure/DEPLOY.md)。

---

## 這是什麼

LocalLLMOutsiderProvider（指令叫 `llmop`）有兩種人會用：

| 你是 | 你要做的事 | 看哪一段 |
|---|---|---|
| **A：有模型的人** | 你電腦上有 LM Studio 或 Ollama 跑著模型，想讓幾個朋友經網路用它 | [A：我有模型，要分享給別人](#a我有模型要分享給別人) |
| **B：拿到連結的人** | 朋友給了你一個連結和一把 key（或帳號），你想在自己電腦上用 | [B：別人給了我連結和 key，我要用](#b別人給了我連結和-key我要用) |

先認識幾個詞（後面會一直用到）：

- **終端機**＝打指令的視窗（黑色或藍色底）。Windows 內建的叫 **PowerShell**。
- **指令**＝在終端機裡打的一行字，打完按 Enter 執行。
- **通道**＝Cloudflare 提供的免費服務，讓外面的人可以連到你電腦上的程式，**不用改路由器、不用開防火牆**。這裡會開兩條：
  - **帳號網站**：朋友登入、看自己的用量、建立 key 的網頁。
  - **API 連結**：程式用的網址，長得像 `https://example.trycloudflare.com/xxxxxxxx/v1`。中間那段 `xxxxxxxx` 是**秘密路徑**，跟密碼一樣不能外流。
- **API key**＝`sk-llop-` 開頭的一串字，程式用它證明「我是誰」。跟密碼一樣不能外流。
- **本機控制台**＝只有 A 自己的電腦打得開的管理網頁（`http://127.0.0.1:8781/`）。
- **加密模式**＝對話內容在 B 的電腦上先加密，只有 A 跑模型的那台解得開；中間的 Cloudflare 與紀錄檔都看不到內容。
  不加密的叫**明文**（直連）模式，中間看得到內容。
- **指紋**＝A 的加密金鑰的「身分證號碼」（22 個字元），B 用它確認自己連到的真的是 A，不是冒牌貨。

> 這是給少數人用的小型服務，不是商業級服務。這一版還沒在真的模型、真的通道、兩台電腦上完整實測過（見 [交辦單](handoff/20261123-v0.3-real-machine.md)）。

---

## A：我有模型，要分享給別人

### 你需要準備什麼

| 要準備的 | 說明 |
|---|---|
| 一台 Windows 電腦，有足夠的顯示卡或記憶體跑模型 | 這台要一直開著，朋友才用得到 |
| LM Studio 或 Ollama，並下載好一個模型 | 這份教學兩種都寫 |
| 網路 | |
| （Python 版才要）Python 3.11 或更新 | 免安裝版已經內含 Python |

### 步驟 A1：安裝（兩種擇一）

#### 甲、免安裝版（推薦，不用裝 Python）

1. **做什麼**：下載並解壓縮免安裝版。
2. **打什麼**：不用打指令。用瀏覽器打開
   <https://github.com/ChouYangEn0401/LocalLLMOutsiderProvider/releases/tag/v0.3.0a4>，
   下載 `llmop-0.3.0a4-windows-x64-portable.zip`。在下載的檔案上按右鍵 →「解壓縮全部」，解到一個你找得到的地方（例如「文件」）。
   打開解出來的 `llmop-0.3.0a4` 資料夾，**雙擊 `Command-Prompt.bat`**。之後 A 段所有 `llmop` 指令都在這個視窗裡打。
3. **會看到什麼**：一個黑色視窗，上面寫：

   ```
   Type commands like:  llmop usage    llmop keys    llmop tunnel show-api    llmop --help
   ```

   在裡面打 `llmop --version`，會看到：

   ```
   llmop 0.3.0a4
   ```

4. **不是這樣怎麼辦**：
   - 出現 `[X] python\python.exe is missing. Unzip the whole folder again.`：沒有整個解壓縮。重新「解壓縮全部」，不要直接在 zip 裡面點兩下。
   - **注意**：這個資料夾裡之後會出現 `data` 資料夾，裡面有資料庫和**私鑰**。不要分享、不要上傳、不要放進雲端同步資料夾。

#### 乙、Python 版（已經有 Python 3.11 以上）

1. **做什麼**：在 PowerShell 裡建一個資料夾與虛擬環境（**虛擬環境**＝獨立的 Python 小房間），安裝伺服器。
2. **打什麼**：按 Windows 鍵，打 `PowerShell`，按 Enter。然後一次一行：

   ```powershell
   python --version
   ```

   ```powershell
   mkdir $HOME\llmop-server
   ```

   ```powershell
   cd $HOME\llmop-server
   ```

   ```powershell
   python -m venv .venv
   ```

   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```

   ```powershell
   python -m pip install "localllm-outsider-provider[server,secure] @ https://github.com/ChouYangEn0401/LocalLLMOutsiderProvider/releases/download/v0.3.0a4/localllm_outsider_provider-0.3.0a4-py3-none-any.whl"
   ```

   ```powershell
   llmop --version
   ```

3. **會看到什麼**：第一行印出 `Python 3.11` 以上的版本；啟用虛擬環境後提示字最前面多了 `(.venv)`；安裝最後一行是 `Successfully installed` 開頭；最後一行印出：

   ```
   llmop 0.3.0a4
   ```

4. **不是這樣怎麼辦**：
   - 啟用虛擬環境時出現紅字「**因為這個系統上已停用指令碼執行**」：先打 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`（只對這個視窗有效），再打一次 `.\.venv\Scripts\Activate.ps1`。
   - 想從原始碼裝（要先裝 git）：`git clone https://github.com/ChouYangEn0401/LocalLLMOutsiderProvider.git`，`cd LocalLLMOutsiderProvider`，
     建好虛擬環境後 `python -m pip install -e ".[server,secure]"`。
   - `llmop` 說 `needs the server extra`：安裝那行的 `[server,secure]` 漏了，重打一次。
   - **以後每次重開 PowerShell**，都要先 `cd $HOME\llmop-server`，再打 `.\.venv\Scripts\Activate.ps1`。
   - `config.yaml` 與 `data` 資料夾會建在這個資料夾；加密用的私鑰在 `$HOME\.llmop`。都不要分享。

### 步驟 A2：打開模型

1. **做什麼**：讓 LM Studio 或 Ollama 在這台電腦上跑起模型。
2. **打什麼**：
   - **LM Studio**：打開 LM Studio，載入一個模型，到開發者（Developer）分頁打開本機伺服器。**記下畫面上顯示的網址與埠號**（預設 `1234`，但可能是別的數字），以及模型名稱。
     **不要**打開「在區網提供服務」那類選項。
   - **Ollama**：在 PowerShell 打（模型名稱可以換成你要的）：

     ```powershell
     ollama pull qwen3.5:9b
     ```

     ```powershell
     ollama list
     ```

3. **會看到什麼**：LM Studio 顯示伺服器正在執行；Ollama 的 `ollama list` 列出你剛下載的模型名稱。
4. **不是這樣怎麼辦**：模型跑不起來是 LM Studio／Ollama 本身的問題，請照它們自己的說明處理，這一步通了再往下。

### 步驟 A3：建立設定檔與管理員密碼

1. **做什麼**：建立 `config.yaml`（設定檔），設定管理員 `admin` 的密碼，並告訴它模型在哪。
2. **打什麼**（照你的情況挑一行；`1234` 換成 LM Studio 顯示的埠號，`example-model` 換成模型名稱）：

   LM Studio：

   ```powershell
   llmop init --backend-type lmstudio --backend-url http://127.0.0.1:1234 --model example-model
   ```

   Ollama：

   ```powershell
   llmop init --backend-type ollama --model qwen3.5:9b
   ```

   它會問兩次密碼（打的時候畫面**不會顯示任何字**，這是正常的），至少 8 個字：

   ```
   New password for the web login 'admin' (input is hidden):
   Type it again:
   ```

3. **會看到什麼**：

   ```
   Creating config.yaml
   [OK] created config.yaml
        web login: admin / the password you just typed
        LLM backend: lmstudio at http://127.0.0.1:1234

   Next steps:
     ...
   ```

4. **不是這樣怎麼辦**：
   - `too short: use at least 8 characters`：密碼太短，重打。
   - `the two passwords are different; try again`：兩次打的不一樣，重打。
   - `[X] config.yaml already exists; nothing was changed.`：已經建過了。要重來就加 `--force`（舊檔會留成 `.bak`）；只是要改密碼用 `llmop set-password admin`。

### 步驟 A4：檢查設定和模型連不連得上

1. **做什麼**：確認 `llmop` 找得到你的模型。這一步只讀，不改任何東西。
2. **打什麼**：

   ```powershell
   llmop check
   ```

3. **會看到什麼**：前面幾段是設定檔位置、帳號表、通道狀態，最後是：

   ```
   Backends:
     [OK] lmstudio (lmstudio) http://127.0.0.1:1234  models: 2  15 ms
          example-model, other-model

   All good.
   ```

   （模型數量、毫秒數每台不同。）
4. **不是這樣怎麼辦**：
   - 看到 `[X]  lmstudio (lmstudio) http://127.0.0.1:1234  NOT reachable: Backend 'lmstudio' is not reachable (is the LM Studio server running?).`：
     LM Studio 的伺服器沒開，或埠號不對。對照 LM Studio 畫面上的埠號，重新 `llmop init --force --backend-type lmstudio --backend-url http://127.0.0.1:<埠號> --model <名稱>`。
   - `has no models yet (Ollama: ollama pull <model>)`：Ollama 還沒有模型，回到步驟 A2。

### 步驟 A5：安裝 cloudflared（開通道用）

1. **做什麼**：裝 Cloudflare 的 `cloudflared`，它負責開兩條通道。用免費的「快速通道」不需要 Cloudflare 帳號。
2. **打什麼**：

   ```powershell
   winget install --id Cloudflare.cloudflared
   ```

3. **會看到什麼**：winget 安裝完成。**關掉視窗重開**（Python 版要重新 `cd` 和啟用虛擬環境；免安裝版重新雙擊 `Command-Prompt.bat`），再打 `llmop check`，
   中間那一行 `Tunnel: … cloudflared:` 後面會是 cloudflared 的位置，而不是 `NOT FOUND`。
4. **不是這樣怎麼辦**：`winget` 無法辨識：免安裝版可以直接雙擊 `Start-With-Tunnel.bat`，它會問你要不要裝；或到 Cloudflare 官網下載 `cloudflared.exe`，放在免安裝版資料夾裡（跟 `Start.bat` 同一層）。

### 步驟 A6：幫朋友建帳號，並限制他能用的模型

1. **做什麼**：替朋友 `alice` 建帳號，讓程式產生一個隨機密碼，並限制她只能用 `example-model`。
2. **打什麼**：

   ```powershell
   llmop add-user alice --generate --models example-model
   ```

3. **會看到什麼**：

   ```
   [OK] added account 'alice' (role user) to config.yaml
        password: <一串隨機密碼>
        (shown only once - send it to them privately)
        tokens/day unlimited, tokens/month unlimited, requests/minute 20
        A running server picks this up automatically (no restart needed).
   ```

   **密碼只顯示這一次**，抄下來，等一下私下給 alice。`llmop serve` 已經在跑也不用重開。
4. **不是這樣怎麼辦**：
   - `account 'alice' already exists.`：帳號已經有了；要改密碼用 `llmop set-password alice --generate`。
   - **限制模型的規則**：`--models` 可以寫好幾個，用逗號隔開（`*`＝全部）。`config.yaml` 裡有一行 `model:`，有填的話**所有人都用那一個模型**；
     這時帳號的 `--models` 要包含它，不然這個帳號會被拒（`model_not_allowed`）。想讓不同帳號用不同模型，把 `model:` 那行改成 `model: ""`。
   - 等級、用量上限也寫在 `config.yaml`（`accounts.alice.tier: normal`），存檔就生效，規則見 README 的「等級與用量」。

### 步驟 A7：開加密（建議）

如果只給朋友明文的 key，可以跳過這一步。

1. **做什麼**：在這台電腦建立 worker（**worker**＝負責把加密內容解開、交給模型的小程式）與它的金鑰，並記下**指紋**。
2. **打什麼**：
   - 先打開設定檔：

     ```powershell
     notepad config.yaml
     ```

     找到 `roles: [gateway]` 這一行，改成 `roles: [gateway, worker]`，存檔，關掉記事本。
   - 建立 worker 金鑰：

     ```powershell
     llmop worker init
     ```

   - **用 LM Studio 的人**還要改 worker 的設定，讓它找到 LM Studio。打開設定檔（Python 版打第一行，免安裝版打第二行）：

     ```powershell
     notepad $HOME\.llmop\worker\config.json
     ```

     ```bat
     notepad data\llmop_home\worker\config.json
     ```

     把 `"backends"` 那一行改成下面這樣（`1234` 換成 LM Studio 的埠號），存檔：

     ```json
     "backends": [{"kind": "openai", "base_url": "http://127.0.0.1:1234"}],
     ```

   - 看指紋：

     ```powershell
     llmop worker show-key
     ```

3. **會看到什麼**：`llmop worker init` 印出：

   ```
   已建立金鑰檔 …keys.json（權限 0600）。
   目前的金鑰（請把 key_id 與 public_key 釘選到用戶端設定）：
     key_id     : <22 個字元>
     public_key : <43 個字元>
     建立時間   : <日期時間> UTC
   已建立設定檔 …config.json；請確認 gateway_url 與 backends 後再執行 llmop worker run。
     gateway_url 預設是同一台電腦的閘道（…）。
     在別台電腦上請改成 wss://<API 連結>/worker/ws（…）。
   ```

   （worker 跟伺服器在同一台電腦時，`gateway_url` 不用改，也不用自己執行 `llmop worker run`：`llmop serve` 會一起帶起來。）

   `llmop worker show-key` 印出 `目前的金鑰：` 和同樣的 `key_id`。**`key_id` 那 22 個字元就是指紋。**
   用**別的管道**（當面、電話、另一個通訊軟體）把指紋交給朋友，**不要**只放在帳號網站上。
4. **不是這樣怎麼辦**：
   - `llmop worker needs the secure extra`：Python 版安裝時漏了 `secure`，重裝 `[server,secure]`。
   - `找不到 …keys.json；請先執行 llmop worker init`：先做 `llmop worker init`。
   - worker 的 `base_url` 只能是本機（`127.0.0.1`、`localhost`），不然 worker 拒絕啟動。

### 步驟 A8：啟動伺服器並打開通道

1. **做什麼**：啟動整個服務，並開兩條通道（帳號網站、API 連結）。**這個視窗要一直開著**，關掉就停止分享。
2. **打什麼**：

   ```powershell
   llmop serve --tunnel --open
   ```

   （免安裝版也可以直接雙擊 `Start-With-Tunnel.bat`，效果一樣。）
3. **會看到什麼**：瀏覽器自動打開本機控制台。視窗裡先出現一個框：

   ```
   ==================================================================
     llmop 0.3.0a4 - Local LLM Outsider Provider (node)

     Console   : http://127.0.0.1:8781/   (this computer only)
     Accounts  : http://127.0.0.1:8780/   (tunnel 'ui')
     API link  : http://127.0.0.1:8782/k7Q…/v1   (tunnel 'api'; keep it private)
     Full link : llmop tunnel show-api
     ...
     Stop      : press Ctrl+C or close this window
   ==================================================================
   ```

   過幾秒到一分鐘，通道接通後還會出現：
   - `PUBLIC URL (Cloudflare quick tunnel):` 下面一行 `https://….trycloudflare.com`，以及 `Web login : https://….trycloudflare.com/ui/login`——**這是帳號網站，給朋友登入用**。
   - `API link  : https://….trycloudflare.com/k7Q…/v1   (and /e2e/v1)`——API 連結，**秘密路徑故意只印前 3 個字**。
   - 開了加密的話，還有一行 `Worker    : in-process, key_id <指紋> (clients pin this key)`。
4. **不是這樣怎麼辦**：
   - `cannot listen on 127.0.0.1:8781 … Is llmop already running in another window?`：已經有一個 `llmop serve` 在跑，關掉另一個視窗再試。
   - `WARNING: the ui tunnel did not start; continuing without it`：cloudflared 沒裝好，回到步驟 A5。
   - 新網址要 10–60 秒才會到處都連得到，稍等一下。**快速通道的網址每次重開都會變**，要重新告訴朋友。

### 步驟 A9：看完整的 API 連結

啟動畫面只印遮住的連結（因為畫面常被存成紀錄檔）。要完整的，用另一個視窗看。

1. **做什麼**：開**第二個視窗**（Python 版：新開 PowerShell、`cd $HOME\llmop-server`、啟用虛擬環境；免安裝版：再雙擊一次 `Command-Prompt.bat`），印出完整連結。
2. **打什麼**：

   ```powershell
   llmop tunnel show-api
   ```

3. **會看到什麼**：

   ```
   API link : https://example.trycloudflare.com/xxxxxxxx/v1
   Encrypted: https://example.trycloudflare.com/xxxxxxxx/e2e/v1
   Keep it private: anyone with the link can try API keys against it.
   ```

   第一行就是要給朋友的 **API 連結**（明文模式用）。
4. **不是這樣怎麼辦**：網址是 `http://127.0.0.1:8782/…` 開頭：API 通道還沒接通，等一下再打一次；或確認步驟 A8 有加 `--tunnel`。

### 步驟 A10：把東西交給朋友

1. **做什麼**：依朋友要用的方式，私下交給他需要的東西。**每樣都要私下給，不要貼在公開的地方。**
2. **打什麼**：不用打指令。

   | 朋友要用 | 你要給他 | 怎麼拿到 |
   |---|---|---|
   | **明文**（簡單，但中間看得到內容） | API 連結＋一把 key | API 連結：步驟 A9。key：本機控制台 `http://127.0.0.1:8781/` 用 `admin` 登入 →「GUEST 金鑰」→「加密」選「可不加密」→ 按「發 GUEST 金鑰」，畫面上的 `sk-llop-…` **只顯示一次** |
   | **加密**（建議） | 帳號網站網址＋帳號 `alice`＋密碼＋指紋 | 網址：步驟 A8 的 `Web login` 那行（去掉 `/ui/login`）；密碼：步驟 A6；指紋：步驟 A7 |

3. **會看到什麼**：朋友照這份教學的 B 段做完，就能用了。
4. **不是這樣怎麼辦**：朋友說連不上：先確認你的 `llmop serve` 視窗還開著、網址是最新的（快速通道重開會換網址）。

### 步驟 A11：確認安全（對外之前一定做）

1. **做什麼**：在 `llmop serve` 跑著的時候，用第二個視窗檢查「不該被外面連到的東西，真的連不到」。
2. **打什麼**（一次一行，每行跑完再打下一行）：

   ```powershell
   llmop audit exposure
   ```

   ```powershell
   llmop audit exposure --public
   ```

   ```powershell
   llmop audit exposure --with-key
   ```

3. **會看到什麼**：每一項一行，`[PASS]` 是通過；最後兩行像這樣：

   ```
   llmop audit exposure

   [PASS] local console (port 8781) not reachable on 192.168.1.10
   [PASS] account website (port 8780) not reachable on 192.168.1.10
   ...
   [PASS] tunnels point only at the public listeners
          ui -> 8780, api -> 8782; never 8781 (console) or a backend

   Result: 12 PASS
   OK - nothing exposed that should not be.
   ```

   （IP 與數量每台不同。）`--public` 會經過通道再檢查一次；`--with-key` 會臨時發一把 key 測試、做完自動撤銷。
4. **不是這樣怎麼辦**：
   - 最後一行是 `NOT OK - fix the FAIL lines before sharing the API link.`：**先不要把連結給別人**。看 `[FAIL]` 那幾行寫什麼，
     最常見的是 LM Studio 開了「在區網提供服務」，或 Ollama 被設成對外。
   - `[INFO] llmop serve does not seem to run`：`llmop serve` 沒在跑，回到步驟 A8，在**另一個視窗**跑這一步。

### 步驟 A12：關掉，與撤銷 key

1. **做什麼**：停止分享；或不讓某一把 key 再用；或 API 連結外流時換一條。
2. **打什麼**：
   - **停止分享**：在 `llmop serve` 的視窗按 `Ctrl+C`（或直接關掉視窗）。
   - **看有哪些 key**：

     ```powershell
     llmop keys
     ```

   - **撤銷一把 key**（`<ID>` 換成上一行列出來的 id）：

     ```powershell
     llmop revoke-key <ID>
     ```

   - **API 連結外流了，換一條**（key 不用換）：

     ```powershell
     llmop tunnel rotate-api
     ```

3. **會看到什麼**：
   - 停止：視窗最後印 `llmop stopped.`。
   - `llmop keys`：一張表，欄位是 `id  user  name  key  created  last used  expires  status`（`key` 欄只顯示遮住的樣子；還沒有 key 時是 `No API keys yet.`）。
   - 撤銷：`[OK] revoked <ID>; it stops working immediately`。
   - 換連結：`[OK] new API link:`，下面是新的完整連結，以及 `the old link stopped working. API keys are unchanged.`。把新連結私下給朋友，他用 B 段的步驟 B7 換掉。
4. **不是這樣怎麼辦**：
   - `no active API key with id '<ID>' (see: llmop keys)`：id 打錯，或已經撤銷過了。用 `llmop keys --all` 看全部（含已撤銷）。
   - 也可以在本機控制台按按鈕做同樣的事（撤銷 key、更換 API 連結、自動定期更換）。

---

## B：別人給了我連結和 key，我要用

### 你需要準備什麼

| 要準備的 | 說明 |
|---|---|
| 一台 Windows 電腦 | **不需要**模型、不需要顯示卡 |
| Python 3.11 或更新 | 步驟 B1 會檢查 |
| A 給你的東西 | **明文**：API 連結＋key。**加密**：帳號網站網址＋帳號＋密碼＋指紋 |
| 網路 | |

> 只用明文的話，其實不用裝任何東西：任何「OpenAI 相容」的工具，把網址填 API 連結、key 填 `sk-llop-…` 就能用（見 README 的「直連模式」）。
> 下面裝的 SDK（**SDK**＝給程式用的工具包）幫你把連結和 key 存好、處理加密，並在連結換掉時告訴你。

### 步驟 B1：打開 PowerShell、確認 Python

1. **做什麼**：開終端機，看 Python 版本。
2. **打什麼**：按 Windows 鍵，打 `PowerShell`，按 Enter。然後：

   ```powershell
   python --version
   ```

3. **會看到什麼**：`Python 3.` 開頭的一行，例如 `Python 3.12.8`。`3.` 後面要 **11 或更大**。
4. **不是這樣怎麼辦**：沒裝或太舊，到 <https://www.python.org/downloads/> 下載 Windows 安裝檔，安裝畫面第一頁**一定要勾「Add python.exe to PATH」**，裝好後重開 PowerShell。

### 步驟 B2：開資料夾、建虛擬環境

1. **做什麼**：開一個練習用的資料夾，建立並啟用虛擬環境（獨立的 Python 小房間）。
2. **打什麼**（一次一行）：

   ```powershell
   mkdir $HOME\llmop-client
   ```

   ```powershell
   cd $HOME\llmop-client
   ```

   ```powershell
   python -m venv .venv
   ```

   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```

3. **會看到什麼**：提示字最前面多了 `(.venv)`：

   ```
   (.venv) PS ...\llmop-client>
   ```

4. **不是這樣怎麼辦**：
   - 紅字「**因為這個系統上已停用指令碼執行**」：先打 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`，再打一次 `.\.venv\Scripts\Activate.ps1`。
   - **以後每次重開 PowerShell**，都要先 `cd $HOME\llmop-client`，再打 `.\.venv\Scripts\Activate.ps1`。

### 步驟 B3：安裝 SDK

1. **做什麼**：只裝用戶端需要的部分（不裝伺服器）。
2. **打什麼**：

   ```powershell
   python -m pip install "localllm-outsider-provider[sdk] @ https://github.com/ChouYangEn0401/LocalLLMOutsiderProvider/releases/download/v0.3.0a4/localllm_outsider_provider-0.3.0a4-py3-none-any.whl"
   ```

   裝好後：

   ```powershell
   llmop client profiles
   ```

3. **會看到什麼**：安裝最後一行是 `Successfully installed` 開頭。第二個指令印出：

   ```
   還沒有任何設定檔。用 llmop client enroll 或 llmop client add-plain 建立。
   ```

4. **不是這樣怎麼辦**：
   - 一定要有**前後的雙引號**，不然 PowerShell 看不懂 `[` `]` 和 `@`。
   - `llmop` 無法辨識：提示字前面沒有 `(.venv)`，回到步驟 B2。
   - 在 B 這台打 `llmop serve`、`llmop --version` 會說 `needs the server extra`：正常，B 不需要伺服器，`llmop client …` 能用就好。

接下來**明文**做步驟 B4，**加密**做步驟 B5，兩個挑一個。

### 步驟 B4：明文——用 API 連結和 key 建設定檔

1. **做什麼**：把 A 給你的 API 連結和 key 存成一個設定檔，名字叫 `tiny`（名字可以自己取）。
2. **打什麼**（網址換成 A 給你的 API 連結）：

   ```powershell
   llmop client add-plain --profile tiny --api-link https://example.trycloudflare.com/xxxxxxxx/v1
   ```

   它會問 key：

   ```
   API key（不會顯示）：
   ```

   在視窗裡**按右鍵貼上** `sk-llop-…`（畫面**不會顯示任何字**，這是正常的），按 Enter。
3. **會看到什麼**：

   ```
   已建立明文設定檔「tiny」（sk-llop-…xxxx，https://example.trycloudflare.com/…）
   ```

   key 和秘密路徑都只顯示一部分，這是故意的。
4. **不是這樣怎麼辦**：
   - `錯誤（bad_request）：API key 必須以 sk-llop- 或 llmop_sk_ 開頭`：貼錯了，或貼的時候多了空白。重打一次。
   - `錯誤（bad_request）：API 連結必須是 https://（只有連到本機 127.0.0.1 時才可以用 http://）`：網址打錯，要 `https://` 開頭。
   - `錯誤（bad_request）：設定檔「tiny」已存在；換個名稱，或先 llmop client forget --profile tiny`：照它說的做。
   - 明文模式的對話內容，A 那邊的閘道和 Cloudflare 都看得到。介意的話請 A 給你加密的帳號，改做步驟 B5。

### 步驟 B5：加密——用帳號建設定檔

1. **做什麼**：用 A 給你的帳號密碼，自動建一把「必須加密」的 key、在你電腦產生自己的金鑰（私鑰不會離開你的電腦）、核對 A 的指紋，存成設定檔 `home`。
2. **打什麼**（網址、帳號、指紋換成 A 給你的）：

   ```powershell
   llmop client enroll --account-url https://example-ui.trycloudflare.com --user alice --fingerprint <22 個字元的指紋> --profile home --test
   ```

   它會問密碼（畫面不會顯示）：

   ```
   alice 的密碼：
   ```

3. **會看到什麼**：

   ```
   已在這台電腦建立簽章金鑰（sig_key_id …；私鑰不會離開這台電腦）
   API key 已存在本機設定檔「home」（sk-llop-…xxxx）
   伺服器指紋相符，已釘選：<指紋>
   傳輸方式：e2e　狀態：ready　API 連結：https://example.trycloudflare.com/…
   測試回覆：…
   ```

   `測試回覆` 後面是模型回的一句話（因為加了 `--test`）。
4. **不是這樣怎麼辦**：
   - `錯誤（trust）：指紋不符，可能被調包，沒有釘選`：**停下來，不要繼續**。用別的管道跟 A 重新核對指紋。
   - `錯誤（trust）：伺服器沒有公布這個指紋的金鑰（worker 沒上線，或指紋打錯）；沒有釘選`：指紋打錯，或 A 沒開加密（步驟 A7）。
     核對好之後補上：`llmop client pin --profile home --fingerprint <指紋>`。
   - `錯誤（auth）：…`：帳號或密碼不對。
   - `錯誤（bad_request）：帳號網站沒有提供 API 連結；請用 --api-link 指定（向服務提供者拿）`：請 A 給你 API 連結，加上 `--api-link <連結>` 重打。

### 步驟 B6：自我檢查

1. **做什麼**：檢查設定檔是否完整、連結和 key 是否有效、檔案只有你自己讀得到。不會送任何對話內容。
2. **打什麼**（`tiny` 換成你的設定檔名稱，例如 `home`）：

   ```powershell
   llmop client self-check --profile tiny
   ```

3. **會看到什麼**：每項一行，`PASS` 是通過：

   ```
   PASS  檔案權限：config.json　只有本人
   PASS  設定檔完整
   PASS  API 連結連得到
   PASS  API key 有效（plain）　1 個模型
   ```

   加密的設定檔還會多幾行：`檔案權限：signing_key.json`、`明文被拒（必須加密的 key）`、`沒有簽章的請求被拒`，都要是 `PASS`。
4. **不是這樣怎麼辦**：
   - `FAIL  檔案權限：…　還有其他人有權限：…`：加 `--fix` 再跑一次：`llmop client self-check --profile tiny --fix`。
   - `FAIL  API 連結連得到　連不上或連結已更換（向服務提供者拿新連結）`：A 的伺服器沒開，或連結換了（見步驟 B8）。
   - `FAIL  API key 有效（…）　auth: …`：key 被撤銷或過期，請 A 給你新的。

### 步驟 B7：試問一句

1. **做什麼**：寫一個小程式，用設定檔問一句話。
2. **打什麼**：

   ```powershell
   notepad ask.py
   ```

   記事本問「要建立新檔案嗎？」按「是」。貼上下面的內容（第 3 行的 `tiny` 換成你的設定檔名稱），按 Ctrl+S 存檔，關掉記事本：

   ```python
   from llmop.sdk import Client, SdkError

   try:
       with Client.from_profile("tiny") as client:
           reply = client.chat({"messages": [{"role": "user", "content": "用一句話介紹你自己"}]}, max_tokens=200)
           print(reply["choices"][0]["message"]["content"])
   except SdkError as e:
       print(f"錯誤（{e.kind_hint}）：{e.message}")
   ```

   執行：

   ```powershell
   python ask.py
   ```

3. **會看到什麼**：等一下，印出模型的回答。
4. **不是這樣怎麼辦**：

   | 看到 | 意思 | 怎麼辦 |
   |---|---|---|
   | `錯誤（link_changed）：API 連結已更換，請向服務提供者拿新連結（然後執行 llmop client set-link）` | A 換了 API 連結 | 做步驟 B8 |
   | `錯誤（offline）：…` | A 的伺服器沒開、通道斷了 | 請 A 確認 `llmop serve` 在跑 |
   | `錯誤（quota）：…` | 用量用完 | 等用量回來（A 的設定決定多久回來） |
   | `錯誤（auth）：…` | key 無效、過期或被撤銷 | 請 A 給你新的 key |
   | `錯誤（rate）：…` | 太頻繁或排隊太多 | 等一下再試 |
   | `錯誤（trust）：…` | 加密的信任問題 | **不要自己繞過**，跟 A 核對指紋 |
   | `ModuleNotFoundError: No module named 'llmop'` | 虛擬環境沒啟用 | 回到步驟 B2 |

### 步驟 B8：連結換了，換成新的

1. **做什麼**：A 換了 API 連結（快速通道重開、或連結外流換掉）時，把設定檔裡的連結換成新的。key 和指紋都不用動。
2. **打什麼**（網址換成 A 給你的新連結）：

   ```powershell
   llmop client set-link --profile tiny https://example.trycloudflare.com/zzzzzzzz/v1
   ```

3. **會看到什麼**：

   ```
   已換成新的 API 連結：https://example.trycloudflare.com/…
   ```

4. **不是這樣怎麼辦**：
   - 多一行 `（試連失敗：連結不通或伺服器沒在跑；已經存下，稍後再試）`：新連結已經存好了，只是現在連不上。A 的伺服器開了之後再試。
   - `錯誤（bad_request）：找不到設定檔「tiny」。先執行 llmop client enroll 或 llmop client add-plain`：名字打錯，用 `llmop client profiles` 看有哪些。

---

## 常見問題

**A：朋友說網址打不開？**
快速通道的網址每次重開 `llmop serve` 都會變。重開後要把新的帳號網站網址、API 連結（`llmop tunnel show-api`）重新給朋友。要固定網址，得用自己的網域建 Cloudflare 具名通道（named tunnel），見 README 的「兩個通道與更換 API 連結」。

**A：很長的回答會失敗（524）？**
Cloudflare 大約 100 秒沒回應就會切斷。加密模式會自動改用輪詢，不受影響；明文模式請縮短 `max_tokens`。

**A：可以把 LM Studio 或本機控制台直接開到網路上嗎？**
**不行。** 只用 `llmop serve` 自己開的兩條通道對外。LM Studio（1234）、Ollama（11434）、本機控制台（8781）都不要對外，也不要改路由器或防火牆。`llmop audit exposure` 會幫你檢查。

**A：`data` 資料夾可以備份到雲端嗎？**
不要。裡面有資料庫和私鑰。換新版本時，把 `config.yaml` 和整個 `data` 資料夾複製到新資料夾就好。

**B：我的金鑰、key 存在哪？**
在 `$HOME\.llmop\client\<設定檔名稱>\`，只有你的 Windows 帳號讀得到。不要分享這個資料夾。不用了可以 `llmop client forget --profile <名稱>` 刪掉（這不會撤銷 A 那邊的 key；要撤銷請 A 做，或加密設定檔用 `llmop client revoke --profile <名稱>`）。

**B：可以接 ntu_easy_llm 或其他程式嗎？**
可以。ntu_easy_llm 的 `remote/api_key/llop` 來源只要寫設定檔名稱（`"llop_profile": "tiny"`），見 ntu_easy_llm 的教學。

---

## 全部做完了，接下來可以…

- **A**：讀 README 的「等級與用量」設定每個人的額度；在本機控制台設定「API 連結自動更換」；讀 [docs/secure/DEPLOY.md](secure/DEPLOY.md) 學怎麼把 worker 放在另一台電腦、怎麼換 worker 金鑰。
- **B**：讀 [docs/SDK.md](SDK.md)，在自己的程式裡用 `Client`（列模型、非同步版 `AsyncClient`、錯誤種類）。
- **B**：用 ntu_easy_llm 把 LLOP 跟其他來源（本地、API 金鑰）放進同一份設定檔，還能設定備援。
