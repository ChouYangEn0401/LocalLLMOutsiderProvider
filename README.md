# LocalLLMOutsiderProvider (`llmop`)

Expose local LLM services to online users with lightweight session-based authentication for persistent access.

把家裡的 **Ollama / vLLM / LM Studio / llama.cpp** 透過 **Cloudflare tunnel** 分享出去，前面加一層 **登入、API key、token 額度**。API 跟 OpenAI 相容，任何 OpenAI SDK / Open WebUI / Continue / Cline 都能直接用。

> ⚠️ 這是**超級精簡的測試架構**（v0.1），適合自己和少數朋友使用。不是商業級服務。

```
使用者 ── https://xxx.trycloudflare.com ── cloudflared ── llmop :8780 ── Ollama :11434
                                                          ├ /v1/*  API（API key + 額度）
                                                          └ /ui/*  網頁（登入、用量、申請 key）
```

## 架設端 vs 應用端（先搞懂這個）

| | 架設端（你） | 應用端（朋友、你的其他裝置） |
|---|---|---|
| 要裝什麼 | 本專案（whl 或 git clone）+ cloudflared + Ollama | **什麼都不用裝**，用現成的 OpenAI SDK / curl / Open WebUI |
| 拿到什麼 | 公開網址 | 網址 + `sk-llop-...` API key（登入網頁自己申請） |

**whl 是架設端的安裝檔**，不是給使用者的。應用端因為 API 跟 OpenAI 相容，所以不需要任何我們的套件。完整說明、安裝方式比較和交接事項見 [docs/HANDOFF.md](docs/HANDOFF.md)。

## 5 分鐘上手（Windows，免安裝版）

不需要先裝 Python。

1. 先準備好 Ollama，並拉一個模型：`ollama pull qwen3.5:9b`
2. 從 GitHub Release 下載 `llmop-<版本>-windows-x64-portable.zip`，解壓縮到任意資料夾。
3. 雙擊 `Start-With-Tunnel.bat`。只在本機測試的話，用 `Start.bat`。
   - 第一次啟動會請你設定 admin 密碼，並建立 `config.yaml`。
   - 沒有 cloudflared 的話，它會問要不要用 winget 幫你裝。
4. 瀏覽器會自動打開。登入後在「API 金鑰」申請一把 key（**只會顯示一次**）。
5. 把畫面上的 `https://xxx.trycloudflare.com` 和 key 給朋友。朋友那邊不用裝任何東西。

新增朋友帳號：雙擊 `Add-User.bat`。

### 其他安裝方式

- **開發者**：`git clone` 之後雙擊 `scripts\windows\Setup.bat`，然後用 `scripts\windows\Start-With-Tunnel.bat` 啟動。
- **已經有 Python**：

```bash
pip install localllm_outsider_provider-0.1.0-py3-none-any.whl
```

```bash
llmop init
```

```bash
llmop serve --tunnel
```

- **Docker**：見 [`docker/README.md`](docker/README.md)。

為什麼有 whl 又有 zip？whl 是 Python 的「零件」，需要電腦上已有 Python。zip 是把零件連同 Python 一起包好的「成品」。兩者裝的都是架設端，詳見 [docs/HANDOFF.md](docs/HANDOFF.md)。

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
- [Docker 用法](docker/README.md) · [部署手冊 (HTML)](docs/DEPLOYMENT_GUIDE.html)

## License

MIT
