# 密碼設計與安全邊界

本文件說明本系統的密碼設計（定案版）、為什麼這樣設計，以及它保護與不保護的範圍。
線路格式見 [PROTOCOL.md](PROTOCOL.md)；程式實作在 `src/llmop/crypto.py`。

v0.2：閘道是 `llmop serve` 節點的一部分，加密面在 API 連結的 `/<秘密路徑>/e2e/v1/*`，worker 連 `/<秘密路徑>/worker/ws`。同一個 API 連結底下的 `/v1/*` 與本機的 `/v1` 是**明文直連**，不受本文件的任何保護；建立 key 時選「必須加密」（`encryption: required`），閘道就拒絕這把 key 走明文。

---

## 1. 金鑰（Keys）

| 金鑰 | 類型 | 壽命 | 存放位置 | 用途 |
|---|---|---|---|---|
| worker 金鑰 | X25519 | 長期，可輪替 | 只在 worker（`keys.json`，權限 0600） | 請求的 HPKE 收件者金鑰 |
| 用戶端簽章金鑰 | Ed25519 | 長期 | 只在用戶端 | 簽署每一筆請求 |
| 回覆金鑰 | X25519 | 一筆請求 | 只在用戶端記憶體 | 那一筆回覆的 HPKE 收件者金鑰 |

- 每把金鑰只做一件事。不會把一把金鑰轉換成另一種類型，也不會從一把金鑰衍生出另一把。
- worker 在 `llmop worker show-key` 與啟動時，會印出自己的 `key_id` 與公鑰。
  **這份主控台輸出才是釘選的依據。** 閘道的本機控制台也會顯示同樣的值，只是為了方便；從閘道複製來的釘選值，等於信任了閘道。
- 閘道不持有任何種類的私鑰。
- worker 的私鑰檔沒有加密，以 0600 權限放在磁碟上：worker 必須能無人值守地啟動，用密碼保護就得每次有人在場輸入。

---

## 2. 函式庫與演算法組合（Library and suite）

- 組合：`DHKEM(X25519, HKDF-SHA256)`、`HKDF-SHA256`、`ChaCha20-Poly1305`，HPKE **base 模式**，單次（single-shot）。
- **使用 `cryptography` 內建的 HPKE。執行期不依賴 `pyhpke`**（`pyhpke` 只在一個交叉驗證測試中使用，沒裝就跳過）。

```python
from cryptography.hazmat.primitives import hpke
SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
sealed = SUITE.encrypt(plaintext, recipient_public_key, info)      # 回傳 enc（32 位元組）‖ ct
plaintext = SUITE.decrypt(sealed, recipient_private_key, info)     # 失敗丟 cryptography.exceptions.InvalidTag
```

在 `cryptography` 50.0.1 上確認過的事：

- `encrypt` 回傳 `enc ‖ ct`：5 位元組的明文得到 53 位元組（32 + 5 + 16）。
- `info` 錯誤或缺少時，解密以 `InvalidTag` 失敗。
- 與 `pyhpke` 0.6.5 雙向互通（`pyhpke` 端使用 `aad=b""`）。所以格式就是標準的 RFC 9180 單次加密、AAD 為空，
  非 Python 的用戶端可以用任何 RFC 9180 函式庫實作。

沒有確認的事：47 到 49 版的行為。`tests/test_crypto.py` 在 API 不存在時會明確失敗；裝了 `pyhpke` 時會跑交叉驗證。

內建 API 有兩個限制，影響了設計：

- **沒有 AAD 參數。** 信封的中繼資料改用 `info` 綁定。`info` 會進入 HPKE 的金鑰排程，所以標頭一改，金鑰就不同，解密必然失敗。
  這樣的綁定強度與 AAD 相同。
- **沒有 secret exporter，也沒有多訊息的 context。** 因此無法使用 RFC 9180 第 5.2 節「透過 export 做雙向通訊」的做法。
  回覆改用另一種做法：對用戶端提供的一次性金鑰，再做一次獨立的 HPKE 封裝。

---

## 3. 封裝請求（用戶端）（Sealing a request）

對應 `crypto.seal_request`。

1. 產生一組一次性的 X25519 金鑰對 `(reply_sk, reply_pk)`。
2. 以全新的隨機 `request_id` 與目前的 `ts` 建立標頭，編碼成 `protected`。
3. 建立明文 `{"reply_key": b64u(reply_pk), "body": …}`，序列化成緊湊的 UTF-8 JSON，然後在結尾補 ASCII 空白，
   直到長度是 1024 的倍數。JSON 允許結尾空白，所以收方不需要去填充。
4. `sealed = b64u(SUITE.encrypt(明文, worker 公鑰, info_request))`。
5. `sig = b64u(Ed25519-Sign(簽章私鑰, signing_input))`。
6. 把 `reply_sk` 與 `request_hash` 留在記憶體，直到回覆到達或放棄這筆請求，然後丟掉 `reply_sk`。絕不寫入磁碟。

`info_request`、`signing_input`、`request_hash`、`info_response` 的定義見 PROTOCOL.md「衍生值」。

填充的目的：讓閘道看不到提示與回覆的確切長度，只看得到以 1 KiB 為單位的大小。

---

## 4. 開啟請求與封裝回覆（worker）（Opening a request and sealing the reply）

依下列順序進行；遇到第一個失敗就停下來，回傳裸錯誤碼（`response_error`）：

1. 嚴格解析信封與標頭。→ `bad_envelope`、`unsupported_version`
2. `header.key_id` 是這台 worker 的金鑰之一。→ `unknown_key_id`
3. 解碼 `client_sig_pub`；它的 `sig_key_id` 等於 `header.sig_key_id`。→ `bad_signature`
4. 如果 `pinned_clients` 不是空的，`client_sig_pub` 必須在裡面。→ `untrusted_client`
5. Ed25519 簽章在 `signing_input` 上驗證通過。→ `bad_signature`
6. 重放檢查（見第 6 節）。→ `stale_timestamp`、`replayed`
7. 這台 worker 提供 `header.model`。→ `model_unavailable`
8. `SUITE.decrypt(sealed, worker_key[key_id], info_request)`。→ `decrypt_failed`
9. 解析明文；`reply_key` 解碼後是 32 位元組。→ `bad_envelope`

從第 9 步開始，每一種結果（包括失敗）都**封裝**後送回：

10. 用 `body` 呼叫後端，`model` 與 `max_tokens` 強制使用標頭裡的值。
11. 建立回覆明文（成功時是 `ok: true` 加上完成結果；失敗時是 `ok: false` 加上錯誤碼與訊息），填充後計算
    `sealed = b64u(SUITE.encrypt(填充後的明文, reply_pk, info_response))`。
12. 送出 `{"type": "response", "ok": true, "sealed": …, "usage": …}`。

實作說明：如果第 9 步本身失敗（明文不是合法 JSON、缺欄位、或 `reply_key` 不是 32 位元組），worker 手上沒有可用的回覆公鑰，
所以只能回裸錯誤碼 `bad_envelope`；`crypto.open_request` 就是這樣做的。只要拿到了合法的 `reply_key`，之後的所有結果都封裝送回。

簽章在解密之前檢查，所以未經驗證的密文永遠不會進入解密流程。
程式中：第 3、5 步是 `crypto.verify_request`，第 8、9 步是 `crypto.open_request`（呼叫前必須先通過 `verify_request`），
第 11 步是 `ReplySealer.seal`。

---

## 5. 閘道檢查什麼（What the gateway checks）

閘道用這把 API 金鑰登記的公鑰，驗證同一個簽章（HTTP 處理順序的第 3 步）。它做得到，是因為簽章只涵蓋它本來就看得到的位元組。
它因此沒有多知道任何東西，也不持有任何秘密。worker 會再檢查一次，不依賴閘道的結果。

這樣做的理由：Cloudflare 終結 TLS，看得到 API 金鑰。有了閘道端驗簽，只偷到 API 金鑰的人無法消耗配額，也碰不到 worker。

---

## 6. 重放防禦（Replay defence）

worker 端：

- **時間窗。** `|worker_now − ts| > 300` 就拒絕。前後合計就是十分鐘的窗。
- **開機門檻。** `ts < worker 啟動時間` 就拒絕。已見 ID 的快取在重啟時會遺失；這條規則讓重啟前的所有請求都無法再用。
  代價：時鐘落後的用戶端，在 worker 剛重啟後的一小段時間內會收到 `stale_timestamp`。
- **已見 ID。** 把 `(sig_key_id, request_id)` 保留到 `ts + 300`，重複出現就以 `replayed` 拒絕。插入時順便清掉過期的項目。
  只要簽章與時間檢查一通過就記錄，不等解密與推論。

閘道端：同樣的時間窗檢查，加上透過待處理與結果表做 `(api_key_id, request_id)` 去除重複。

就算有重放漏網，攻擊者也讀不到任何東西：回覆是封裝給原用戶端的一次性金鑰。這套防禦保護的是運算資源與配額。

用戶端與 worker 的時鐘差距必須在五分鐘以內。

---

## 7. 開啟回覆（用戶端）（Opening the reply）

對應 `ReplyOpener.open`。

1. `SUITE.decrypt(sealed, reply_sk, info_response)`。失敗代表這份回覆不是 worker 發的，或不是這筆請求的回覆；
   回報錯誤（`decrypt_failed`），不要放寬檢查重試。
2. 解析 JSON，回傳 `body`，或把封裝起來的錯誤丟出來。
3. 丟掉 `reply_sk`（呼叫端放掉對 `ReplyOpener` 的參照）。

---

## 8. 設計檢討：哪裡穩固、哪裡補強過

結論：核心選擇（標準 HPKE、獨立的簽章金鑰、一次性回覆金鑰）是穩固的。原始構想在「各部分如何綁在一起」有缺口，
而且有一項性質比字面上聽起來弱。以下逐項說明缺口是什麼、上面的設計怎麼處理。編號固定，其他文件可以直接引用「檢討第 N 項」。

### 檢討 1：請求沒有前向保密，只有回覆有

狀態：不改變擁有者的決定就無法修正；已緩解。

- 請求是用 worker 的長期金鑰加密的。任何錄下請求密文的人（閘道、它的紀錄、Cloudflare），日後如果取得 worker 私鑰，就能解開所有錄下的請求。
- 這在實務上也削弱了回覆的保密性：多輪對話會在 `messages` 裡重送先前助理的回覆，所以舊回覆會出現在之後的請求裡。
- 仍然成立的部分：一份從來沒有被後續請求帶回去的回覆，即使日後所有長期金鑰都外洩，仍然保密。
- 第 1 版的緩解：`llmop worker rotate-key` 產生新金鑰並設為目前金鑰；`llmop worker prune-keys` 刪除舊私鑰。
  刪除之後，用舊金鑰加密的密文就再也解不開。輪替會改變釘選值，所以用戶端必須更新。閘道從不儲存信封。
- 真正的修正是「由長期 worker 身分金鑰簽署的短期預備金鑰」。這屬於第 2 版，需要擁有者決定。

### 檢討 2：簽章被剝除後重簽

狀態：已關閉。

- 在先加密後簽章的設計裡，看得到信封的攻擊者可以拿掉原簽章，用自己的金鑰簽同一份密文，把請求冒充成自己的。
- 兩道防線關閉了它：`sig_key_id` 在標頭裡，標頭雜湊又在 `info_request` 裡，所以換了簽署者，worker 推導出的金鑰就不同，解密失敗。
  而且回覆公鑰在密文裡面，攻擊者無論如何都讀不到回覆。

### 檢討 3：誰為用戶端公鑰背書

狀態：範圍有限且已記錄；另外提供選用的強化。

- worker 是從閘道得知用戶端的簽章公鑰。完全被入侵的閘道因此可以轉送由它自選金鑰簽署的請求。
- 損害僅限於使用 worker 的運算資源。攻擊者讀不到也改不了其他用戶端的請求或回覆。
- 強化：worker 設定中的 `pinned_clients`。設定之後，不論閘道怎麼說，worker 只接受列出的簽章公鑰。

### 檢討 4：worker 金鑰被調包

狀態：靠釘選關閉；沒有其他程式手段能關閉。

- 如果用戶端直接採用 `GET /e2e/v1/models` 給的 worker 公鑰而不檢查，惡意閘道或被註冊的流氓 worker 可以換上自己的金鑰，讀到一切。
- 因此用戶端拒絕任何沒有在設定中釘選的金鑰，而釘選值來自 worker 的主控台。沒有「第一次使用就信任」的模式。

### 檢討 5：網域分離

狀態：已關閉。

- 每一種用途都有自己的前綴：`LLMOP/v1/sig/request`、`LLMOP/v1/hpke/request`、`LLMOP/v1/hpke/response`，以及兩個金鑰 ID 前綴
  `LLMOP/v1/key-id/x25519`、`LLMOP/v1/key-id/ed25519`。每個前綴後面都接一個零位元組。
- 所以請求密文不能被當成回覆重放，簽章不能挪作他用，`key_id` 也不會和 `sig_key_id` 撞在一起。
- 標頭還帶有 `typ`，日後若出現第二種被簽署的物件，也不會和請求混淆。

### 檢討 6：回覆的綁定

狀態：已關閉。

- `info_response` 含有 `request_hash`，而它涵蓋了標頭與請求密文。回覆只能被它所回應的那一筆請求開啟，閘道無法在請求之間調換回覆。

### 檢討 7：回覆的真實性

狀態：成立；以下說明其前提。

- 回覆用 base 模式封裝，本身沒有寄件者認證。真實性來自：`reply_pk` 只有用戶端、以及解開請求的那一方（也就是持有 worker 金鑰者）知道。
- 要產生能用 `reply_sk` 開啟的密文，必須知道 `reply_pk`，因為 KEM 會把收件者公鑰混進共享秘密裡。
- 這個論證是可靠的，但它依賴「把公鑰保密」，不是教科書上的標準構造。如果需要傳統意義上的保證，修正方式是讓 worker 用一把
  Ed25519 金鑰簽署每一份回覆；那會多一把要釘選的金鑰，第 1 版沒有採用。

### 檢討 8：金鑰外洩後的冒充（KCI）

狀態：依構造不適用。

- HPKE 的 Auth 模式會讓偷到收件者金鑰的人，能對收件者冒充任何寄件者。本設計不使用 Auth 模式；寄件者認證靠 Ed25519 簽章，
  偷到 worker 金鑰也偽造不了。

### 檢討 9：nonce 重用

狀態：依構造不適用。

- 每一次封裝都是全新的單次 HPKE context，有自己的臨時金鑰，只用於一則訊息。程式從不處理 nonce、計數器或可重用的 context。
  實作者不得快取或重用 `SUITE.encrypt` 的任何東西。

### 檢討 10：解密錯誤預言機（oracle）

狀態：已關閉。

- 解密成功之前，worker 只回裸錯誤碼；成功之後，一切都封裝起來。可能引用到提示內容的後端錯誤文字，永遠不會以明文傳送。

### 檢討 11：API 金鑰暴露給 Cloudflare

狀態：已緩解。

- Cloudflare 終結 TLS，看得到 bearer 權杖。但只有 API 金鑰、沒有簽章私鑰的人無法送出請求，因為閘道與 worker 都會驗簽。

### 檢討 12：仍然看得見的中繼資料

狀態：接受；已記錄。

- 閘道與 Cloudflare 看得到：哪一把 API 金鑰、哪個模型、哪一台 worker、什麼時候、`max_tokens`、token 用量，
  以及以 1 KiB 為單位向上取整的密文大小。

### 檢討 13：實作絕對不能做的事

- 記錄、印出或保存信封、封裝過的回覆、明文、API 金鑰或任何權杖。紀錄只能有 `request_id`、`api_key_id`、模型、大小與狀態。
- 攔下 `InvalidTag` 然後換別的輸入重試。（`llmop.crypto` 一律把 `InvalidTag` 與 `InvalidSignature` 轉成 `ProtocolError`，不外漏、不重試。）
- 執行工具呼叫。worker 把 `tool_calls` 原樣交還給用戶端，自己從不執行任何東西。

---

## 9. 安全邊界（給使用者）

下面這段原文照錄於 README，修改時兩邊必須一致：

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
