# LocalLLMOutsiderProvider

把你自己機器上的本機 LLM（Ollama、LM Studio、vLLM）安全地提供給外部使用。
請求在用戶端加密，經過對外的閘道轉送到 worker，只有 worker 能解開；閘道從頭到尾看不到對話內容。

> 本文件目前是骨架，快速入門與部署說明會在各角色完成後補上。

## 三個角色

- **gateway（閘道）**：唯一對外的服務，放在 Cloudflare 隧道後面。負責 API 金鑰、配額、worker 註冊與管理介面，
  並把加密過的請求轉送給 worker。它不持有任何私鑰，也讀不到內容。
- **worker**：跑在本機 LLM 伺服器旁邊，主動以 WebSocket 連到閘道（自己不開任何對外的連接埠），
  解開請求、呼叫本機後端推論，再把回覆加密送回。本機 LLM 伺服器只允許 worker 從 loopback 存取。
- **client（用戶端）**：SDK、本機 OpenAI 相容代理與命令列工具。負責加密與簽署請求、解開回覆。
  一般的 OpenAI 相容程式只要指向本機代理即可使用。

```
用戶端 ──(HTTPS，內容已加密)──▶ Cloudflare ──▶ gateway ◀──(WebSocket，worker 主動連出)── worker ──▶ 本機 LLM
```

## 文件

- [docs/PROTOCOL.md](docs/PROTOCOL.md)：線路協定（信封格式、HTTP API、WebSocket 訊框、錯誤碼、上限）。
- [docs/SECURITY.md](docs/SECURITY.md)：密碼設計、威脅分析與安全邊界。

## 安全性

> **安全邊界**
>
> 本系統的端對端加密保護的是「內容」：對話訊息、工具定義與模型的回覆，從用戶端加密後才送出，只有持有 worker 私鑰的那台機器能解開。閘道（gateway）的營運者、閘道的紀錄檔、Cloudflare 以及網路路徑上的任何人，都看不到內容，也無法竄改而不被發現。
>
> 以下是本系統**不**保護、也無法保護的部分：
>
> - **worker 機器本身。** 模型必須看到明文才能推論。能控制 worker 機器的人（或該機器上的惡意程式）看得到全部內容。請只把請求送給你信任的 worker。
> - **中繼資料。** 閘道與 Cloudflare 看得到：哪一把 API 金鑰、哪個模型、哪一台 worker、什麼時候、`max_tokens`、用掉的 token 數，以及密文大小（以 1 KiB 為單位）。
> - **請求沒有前向保密。** 請求是用 worker 的長期公鑰加密的。若有人先錄下密文，日後又取得該 worker 的私鑰，就能解開當時錄下的請求。多輪對話的請求裡會帶著先前的回覆，所以那些回覆也會跟著曝光。回覆本身用一次性金鑰加密，單獨來看不受長期金鑰外洩影響。請定期輪替 worker 金鑰並刪除舊私鑰。
> - **金鑰的來源。** 用戶端必須用 worker 主控台顯示的指紋來釘選公鑰。若直接相信閘道提供的公鑰，惡意的閘道可以換成自己的金鑰。
> - **可用性。** 閘道或 Cloudflare 可以丟棄、延遲請求，本系統不防阻斷服務。
> - **被入侵的閘道可以佔用 worker 的運算資源**（除非 worker 設定了 `pinned_clients`），但仍然讀不到任何人的內容。
>
> 用戶端與 worker 的時鐘誤差必須在五分鐘以內，否則請求會被當成過期而拒絕。

## 開發

```
pip install -e ".[dev]"
pytest
```

需要 Python 3.11 以上。執行期相依套件只有 `aiohttp` 與 `cryptography`。
