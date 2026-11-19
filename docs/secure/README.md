# 加密模式（端對端加密中繼）

> v0.2：加密中繼已併入 `llmop serve` 節點。本資料夾只講**加密模式**；整個產品（直連、帳號、等級、通道）先讀
> [專案 README](../../README.md)。

請求在用戶端就先加密並簽章，經過 Cloudflare 隧道送到閘道（gateway），閘道轉給 worker；只有 worker 解得開，
解開後交給本機的 LLM 伺服器推論，回覆再加密給當初發出請求的那個用戶端。閘道負責 API 金鑰、用量與排隊、worker 註冊，
但從頭到尾看不到對話內容。一般的 OpenAI 相容程式不需要修改：指向用戶端這邊的本機代理就能使用。

## 三個角色

- **gateway（閘道）**：`llmop serve` 節點的一部分。加密面掛在 **API 連結**底下：`https://<api 主機>/<秘密路徑>/e2e/v1/*`，
  worker 連 `…/<秘密路徑>/worker/ws`。三個入口都只綁 `127.0.0.1`，靠 Cloudflare 隧道對外。它不持有任何私鑰，讀不到內容。
- **worker**：跑在本機 LLM 伺服器旁邊。它主動以 WebSocket 連到閘道（自己不開任何監聽的連接埠，所以放在 NAT 後面也行），
  驗證簽章、解開請求、呼叫本機後端推論，再把回覆加密送回。和閘道同一台機器時，在 `config.yaml` 設
  `roles: [gateway, worker]`，節點會自己啟動它。
- **client（用戶端外掛）**：SDK、本機 OpenAI 相容代理（`127.0.0.1:8788`）與命令列工具。負責加密與簽署請求、解開回覆。

```
【使用者的機器】
    OpenAI 相容程式（例如 SYNOX）
          │  一般的 OpenAI 請求（明文，只在本機）
          ▼
    llmop client proxy ── 127.0.0.1:8788
          │  加密並簽章後的信封
══════════╪══════════ 網際網路（HTTPS）
          ▼
    Cloudflare 通道 "api" ── https://<api 主機>/<秘密路徑>
          │  只轉到 API 連結的連接埠 127.0.0.1:8782
══════════╪══════════
【伺服器（llmop serve）】
          ▼
    API 連結 127.0.0.1:8782
        /<秘密路徑>/e2e/v1/*      加密 API（只看得到中繼資料）
        /<秘密路徑>/worker/ws     遠端 worker 的 WebSocket
        /<秘密路徑>/v1/*          明文直連（不在本文件範圍）
    本機控制台 127.0.0.1:8781     管理（建立 worker、GUEST key、更換 API 連結）
        /worker/ws                同一台機器的 worker
          ▲
          │  WebSocket，由 worker 主動連出
══════════╪══════════
【worker（可以就是伺服器本身）】
    llmop worker run（沒有任何監聽的連接埠）
          │  明文，只走 loopback
          ▼
    Ollama 127.0.0.1:11434 ／ LM Studio ／ vLLM
```

## 網路規則

- **隧道只能指向閘道的連接埠**：通道 `api` 指向 `127.0.0.1:8782`、通道 `ui` 指向 `127.0.0.1:8780`，由 `llmop serve` 自己啟動。
  不可以指向本機控制台（8781）或任何其他服務。
- **本機 LLM 伺服器只讓 worker 從 loopback 連線。** worker 設定裡每個後端的 `base_url` 都必須是
  `http://127.0.0.1…`、`http://[::1]…` 或 `http://localhost…`，否則 worker 拒絕啟動，沒有例外開關。
  LLM 伺服器本身也要只聽 `127.0.0.1`（Ollama 預設如此；vLLM 請加 `--host 127.0.0.1`；LM Studio 不要開「Serve on Local Network」）。
- worker 沒有監聽的 socket；用戶端代理只綁 `127.0.0.1`，因為它沒有任何驗證。
- 遠端 worker 一律用 `wss://`；只有閘道就在同一台機器時才用 `ws://127.0.0.1:8781/worker/ws`（節點內建的 worker 自動如此）。

## 快速開始

需要 Python 3.11 以上，並裝 `secure` 附加套件（Windows 免安裝版已內含）：

```
pip install "localllm-outsider-provider[secure]"
```

每一步要確認什麼、為什麼，請看 [DEPLOY.md](DEPLOY.md)。worker 與用戶端的狀態檔放在 `$LLMOP_HOME/<角色>/`
（預設 `~/.llmop`；Windows 免安裝版是 `data\llmop_home`）。

**1. 伺服器**（對外的那台機器）

```
llmop init                                        # 建立 config.yaml，設定 admin 密碼
llmop serve --tunnel                              # 三個入口＋兩個通道
llmop tunnel show-api                             # 印出 API 連結（加密用 …/e2e/v1）
```

**2. worker**

同一台機器：`config.yaml` 設 `roles: [gateway, worker]`，執行一次 `llmop worker init`、確認
`$LLMOP_HOME/worker/config.json` 的 `backends`，重新 `llmop serve`。節點會自己註冊並啟動它。

另一台機器：

```
llmop worker init                                 # 建立金鑰與設定範本，印出 key_id 與 public_key
# 編輯 config.json：gateway_url 改成 wss://<api 主機>/<秘密路徑>/worker/ws，backends 指向本機後端
llmop worker run --reg-token llmop_reg_…          # 第一次：本機控制台「Worker 與註冊碼」拿到的註冊碼
llmop worker run                                  # 之後每次
```

**3. 用戶端**

```
llmop client keygen                               # 印出簽章公鑰 public_key 與 sig_key_id
```

到帳號網站建立 API key，「加密」選「必須加密」，貼上 `public_key`；畫面只顯示一次的 `sk-llop-…` 抄下來。
建立 `$LLMOP_HOME/client/config.json`（權限 0600）：

```json
{
  "gateway_url": "https://<api 主機>/<秘密路徑>",
  "api_key": "sk-llop-…",
  "pinned_workers": ["<worker 主控台印出的 public_key>"]
}
```

```
llmop client models                               # 列出已釘選的 worker 提供的模型
llmop client chat --model qwen3:8b "你好"         # 送一筆試試
llmop client proxy                                # 本機 OpenAI 相容代理：http://127.0.0.1:8788/v1
```

`pinned_workers` 一定要從 **worker 主控台**抄，不要從閘道抄；理由見下面的「安全邊界」。
更換 API 連結後，只要改 `gateway_url` 並重新啟動代理（與遠端 worker）；API 金鑰與釘選值都不用動。

## 讓 SYNOX（或其他 OpenAI 相容程式）使用

先在使用者的機器上執行 `llmop client proxy`，再把程式的本機 LLM 設定指向它。以 SYNOX 為例：

| 設定 | 值 |
|---|---|
| kind | `local` |
| server | `other` |
| base_url | `http://127.0.0.1:8788` |
| model | `llmop client models` 列出的名稱，例如 `qwen3:8b` |

- 代理沒有驗證，程式裡的 API key 欄位如果必填，隨便填一個值即可；真正的 API 金鑰與簽章金鑰在代理的設定檔裡。
- 其他程式如果要求 base_url 含 `/v1`，就填 `http://127.0.0.1:8788/v1`。代理提供 `GET /v1/models` 與 `POST /v1/chat/completions`。
- 程式要求 `stream: true` 也可以（SYNOX 一律如此）：v0.2 的加密面不做真串流，代理會先回 SSE 標頭、等待時送註解行，
  完成後把整則回答放在一個 chunk 裡送出，最後是 `data: [DONE]`。真串流排在 v0.3。
- `max_tokens`（或 `max_completion_tokens`）沒給時，用設定檔的 `default_max_tokens`（預設 1024）。這個數字會明文送到閘道。

## 安全邊界

> **安全邊界**
>
> 本系統的端對端加密保護的是「內容」：對話訊息、工具定義與模型的回覆，從用戶端加密後才送出，只有持有 worker 私鑰的那台機器能解開。閘道（gateway）的營運者、閘道的紀錄檔、Cloudflare 以及網路路徑上的任何人，都看不到內容，也無法竄改而不被發現。
>
> 以下是本系統**不**保護、也無法保護的部分：
>
> - **直連（明文）的請求。** 送到 `/v1`（API 連結或本機入口）的請求沒有端對端加密，閘道與 Cloudflare 都看得到內容。只有經用戶端外掛送到 `/e2e/v1` 的請求受上面的保護；要讓一把 key 只能加密使用，建立時選「必須加密」（`encryption: required`）。
> - **worker 機器本身。** 模型必須看到明文才能推論。能控制 worker 機器的人（或該機器上的惡意程式）看得到全部內容。請只把請求送給你信任的 worker。閘道與 worker 在同一台機器時（`roles: [gateway, worker]`），閘道的營運者也就看得到內容。
> - **中繼資料。** 閘道與 Cloudflare 看得到：哪一把 API 金鑰、哪個模型、哪一台 worker、什麼時候、`max_tokens`、用掉的 token 數，以及密文大小（以 1 KiB 為單位）。
> - **請求沒有前向保密。** 請求是用 worker 的長期公鑰加密的。若有人先錄下密文，日後又取得該 worker 的私鑰，就能解開當時錄下的請求。多輪對話的請求裡會帶著先前的回覆，所以那些回覆也會跟著曝光。回覆本身用一次性金鑰加密，單獨來看不受長期金鑰外洩影響。請定期輪替 worker 金鑰並刪除舊私鑰。
> - **金鑰的來源。** 用戶端必須用 worker 主控台顯示的指紋來釘選公鑰。若直接相信閘道提供的公鑰，惡意的閘道可以換成自己的金鑰。
> - **可用性。** 閘道或 Cloudflare 可以丟棄、延遲請求，本系統不防阻斷服務。
> - **被入侵的閘道可以佔用 worker 的運算資源**（除非 worker 設定了 `pinned_clients`），但仍然讀不到任何人的內容。
>
> 用戶端與 worker 的時鐘誤差必須在五分鐘以內，否則請求會被當成過期而拒絕。

## 文件

- [DEPLOY.md](DEPLOY.md)：部署步驟——worker 的建立與註冊、比對指紋與釘選、加密 key 與簽章金鑰、金鑰輪替、時鐘、
  本機控制台的存取範圍、quick tunnel 的限制。
- [PROTOCOL.md](PROTOCOL.md)：線路協定（信封格式、`/e2e/v1` HTTP API、WebSocket 訊框、錯誤碼、上限）。
- [SECURITY.md](SECURITY.md)：密碼設計、逐項的設計檢討，以及上面這段安全邊界的出處。

## 開發

```
pip install -e ".[dev,secure]"
pytest
```

加密相關的測試在 `tests/secure/`、`tests/worker/`、`tests/client/` 與 `tests/e2e/`：在 loopback 上跑真的節點、真的 worker、
真的本機代理，加上假的後端，並錄下閘道收送的每一個位元組，確認裡面沒有任何對話內容。
