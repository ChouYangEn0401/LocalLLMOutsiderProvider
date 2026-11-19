# 待辦事項 (TODO)

> 由主 session 維護。新增項目在最上面。處理時先在這裡標註狀態。

## T1 — 「Context size has been exceeded」(HTTP 500) 的處理 【未開始，等擁有者說開工】

**來源**：擁有者在 DeepSeek harness 用本機 LM Studio 時，兩個 session 同時送訊息，後端回：

```json
{"code":500,"message":"Context size has been exceeded.","type":"server_error"}
```

**為什麼要記**：這個問題在本機直連就會發生；經過 llmop 閘道（尤其加密模式）後更難處理，因為
- 閘道看不到內容，無法代為截斷或摘要對話歷史；
- 加密模式下，本機 agent（例如 SYNOX）收到錯誤後，沒辦法讓對方「即時重新產生對話」；
- 錯誤是後端的 500，用戶端分不出是「context 太長」還是「伺服器壞了」，也就不知道該縮短歷史重試。

**待釐清（假設，都還沒驗證）**：
1. 根因是「兩個 session 的歷史加起來超過 context」，還是 LM Studio / llama.cpp 的**並行槽位（parallel slots）把總 context 平分給同時進來的請求**，導致每個請求能用的 context 變小？後者的話，限制同時請求數（排隊）比縮短對話更有效。（擁有者貼的分析來自另一個 LLM，沒有查證，且把錯誤歸因於「DeepSeek Harness 後端」，與事實不符，不可直接採信。）
2. LM Studio 實際回傳的錯誤格式、HTTP 狀態與是否可偵測，需要實測。
3. 各後端的 context 設定來源：Ollama 預設 4096（`OLLAMA_CONTEXT_LENGTH`）、vLLM `--max-model-len`、LM Studio 載入時的 Context Length。

**需要檢查的地方**：
- SYNOX：`src/synox/llm/local.py`（`_http_text`、context 偵測 97% 警告、`_learn_context`）與其錯誤對應；對「context 超限」是否有重試或縮短歷史。（唯讀）
- llmop：`src/llmop/backends/*`、`src/llmop/api/proxy.py` 的後端錯誤對應，以及合併後的佇列（D5）與加密模式（D1、D3）。

**可能的做法（供之後決策，尚未選擇）**：
- 閘道偵測後端 context 超限，轉成清楚的 OpenAI 格式錯誤，例如 400 `context_length_exceeded`，讓用戶端知道要縮短而不是重試；
- 回報每個模型的 context 長度（`/v1/models` 的 `context_length`，目前已有）；用戶端 / SYNOX 據此自行截斷；
- 以佇列限制同時請求數（D5 的優先佇列），避免並行槽位互相擠壓；
- 加密模式：由用戶端代理（client proxy）在加密前依 context 長度截斷歷史，閘道不介入。

**狀態**：只記錄，尚未動手。擁有者說「等等會需要處理」。
