# LocalLLMOutsiderProvider (`llmop`)

Expose local LLM services to online users with lightweight session-based authentication for persistent access.

把家裡的 **Ollama / vLLM / LM Studio / llama.cpp** 透過 **Cloudflare tunnel** 分享出去，前面加一層 **登入、API key、token 額度**。API 跟 OpenAI 相容，任何 OpenAI SDK / Open WebUI / Continue / Cline 都能直接用。

> ⚠️ 這是**超級精簡的測試架構**（v0.1），適合自己和少數朋友使用。不是商業級服務。

```
使用者 ── https://xxx.trycloudflare.com ── cloudflared ── llmop :8780 ── Ollama :11434
                                                          ├ /v1/*  API（API key + 額度）
                                                          └ /ui/*  網頁（登入、用量、申請 key）
```

## 5 分鐘上手（Windows）

1. 先準備好 Ollama，並拉一個模型：`ollama pull qwen3.5:9b`
2. 安裝 cloudflared（要對外分享時才需要）：`winget install --id Cloudflare.cloudflared`
3. 在專案資料夾雙擊 `scripts\windows\Setup.bat`
   - 它會建立 `.venv`、安裝套件，並執行 `llmop init`
   - `init` 會建立 `config.yaml`，並請你設定 admin 密碼
4. 雙擊 `scripts\windows\Start-With-Tunnel.bat`。只在本機測試的話，用 `Start.bat`。
5. 打開畫面上印出的網址 → 登入 → 在「API 金鑰」申請一把 key（**只會顯示一次**）

用 whl 安裝也可以：

```bash
pip install localllm_outsider_provider-0.1.0-py3-none-any.whl
```

```bash
llmop init
```

```bash
llmop serve --tunnel
```

## 呼叫 API

```bash
curl https://xxx.trycloudflare.com/v1/chat/completions \
  -H "Authorization: Bearer sk-llop-..." -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5:9b","messages":[{"role":"user","content":"你好"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="https://xxx.trycloudflare.com/v1", api_key="sk-llop-...")
print(client.chat.completions.create(model="qwen3.5:9b",
      messages=[{"role": "user", "content": "你好"}]).choices[0].message.content)
```

| 端點 | 說明 |
|---|---|
| `GET /v1/models` | 這把 key 能用的模型 |
| `POST /v1/chat/completions` | 對話（支援 `stream`） |
| `POST /v1/completions` | 舊式補全 |
| `POST /v1/embeddings` | 向量 |
| `GET /v1/me` | 我的帳號、額度、今日用量 |

Key 可以放在 `Authorization: Bearer ...`，也可以放在 `x-api-key: ...`。錯誤格式跟 OpenAI 一樣：

- `401` key 錯
- `403` 不能用這個模型
- `429 rate_limit_exceeded` 太頻繁
- `429 insufficient_quota` 額度用完

## 帳號與額度（`config.yaml`）

帳號和額度都寫在 YAML 裡，**存檔就生效，不用重啟**。範例見 [`config.example.yaml`](config.example.yaml)。

```yaml
default_quota:            # 0 = 不限
  tokens_per_day: 200000
  tokens_per_month: 3000000
  requests_per_minute: 20
  max_concurrent: 2
  max_tokens_per_request: 4096
accounts:
  friend1:
    password_hash: "scrypt$..."   # llmop hash-password
    models: ["qwen*"]
    quota: { tokens_per_day: 50000 }
```

新增使用者：`llmop add-user friend1`，或雙擊 `scripts\windows\Add-User.bat`。

使用量、API key、登入 session 存在 `data/llmop.sqlite3`。用下面的指令查看：

```bash
llmop usage
```

```bash
llmop keys
```

## 常用指令

| 指令 | 用途 |
|---|---|
| `llmop init` | 建立 `config.yaml` 和 admin 帳號 |
| `llmop serve [--tunnel]` | 啟動（`--tunnel` 同時開 trycloudflare） |
| `llmop check` | 檢查設定和後端是否連得上 |
| `llmop add-user NAME` | 新增帳號 |
| `llmop hash-password` | 產生密碼雜湊 |
| `llmop usage` / `keys` / `revoke-key ID` | 看用量、key，撤銷 key |
| `llmop tunnel` | 只開 tunnel（伺服器已經在跑時） |

## Docker

見 [`docker/README.md`](docker/README.md)。有四種組合：

- 閘道加 Ollama
- 閘道加 vLLM
- 只有閘道，接主機上的 Ollama
- 以上任一種再加 cloudflared

## 一定要知道的限制

- **Quick tunnel 的網址每次重啟都會變。** 登入後的網頁永遠顯示目前的網址。要固定網址，請用 named tunnel（自己的網域，免費），然後設定 `server.public_url`。
- **Cloudflare 官方說 quick tunnel 不支援 SSE 串流。** 實際上通常可以用，但不保證。閘道在等待時會送 keepalive；不穩的時候改用 `stream: false`。
- **非串流請求超過約 100 秒會被 Cloudflare 回 524。** 慢的模型請用串流。
- **Ollama 預設 context 很小**，超過時會靜默截斷前面的內容。請在 Ollama 那邊設定 `OLLAMA_CONTEXT_LENGTH=8192`。
- **不要把 Ollama 的 11434 port 直接對外。** 只開放這個閘道。
- 額度是「軟性」的：單一請求可能超過一點點，這跟 OpenAI 和 LiteLLM 的作法一樣。

## 開發

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/python -m build
```

文件：

- [研究報告](docs/research/RESEARCH_REPORT.md)
- [開發計畫書](docs/plan/DEVELOPMENT_PLAN.md)
- [Docker 用法](docker/README.md)

## License

MIT
