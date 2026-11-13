# llmop in Docker

On Windows the simplest way is **without Docker**: `scripts\windows\Setup.bat`, then
`Start-With-Tunnel.bat`. Use Docker when you want Ollama / vLLM in containers too,
or run on a Linux server.

## Pick one compose file

| File | What runs in Docker | Backend URL in `config/config.yaml` |
|---|---|---|
| `compose.host-ollama.yml` | llmop only; Ollama is installed on Windows | `http://host.docker.internal:11434` |
| `compose.ollama.yml` | llmop + Ollama | `http://ollama:11434` |
| `compose.vllm.yml` | llmop + vLLM (NVIDIA GPU required) | `http://vllm:8000` |
| `compose.tunnel.yml` | add-on: Cloudflare quick tunnel (public https URL) | - |

## First time (run inside this `docker` folder)

```bat
copy config.docker.example.yaml config\config.yaml
docker compose -f compose.ollama.yml run --rm --no-deps llmop hash-password
```

Paste the printed hash into `config\config.yaml` as the admin `password_hash`
(and pick the matching `backends:` entry), then:

```bat
docker compose -f compose.ollama.yml up -d --build
docker compose -f compose.ollama.yml exec ollama ollama pull qwen3.5:9b
```

Open <http://127.0.0.1:8780>. With a public URL, add `-f compose.tunnel.yml` to every
command and read the address with
`docker compose -f compose.ollama.yml -f compose.tunnel.yml logs cloudflared | findstr trycloudflare`;
paste it into `config\config.yaml` as `server.public_url` so the web page shows it.

## Good to know

- Ports are published on `127.0.0.1` only; Ollama / vLLM are not published at all.
  The internet reaches llmop only through the tunnel.
- `config\config.yaml` is mounted read-only and re-read automatically when you edit it.
  Add accounts by editing it (hashes from `... run --rm --no-deps llmop hash-password`).
- Keys, sessions and usage live in the `llmop-data` volume; models in `ollama-models` /
  `hf-cache`. `docker compose ... down` keeps them; `down -v` deletes them.
- Settings such as `TZ`, `VLLM_MODEL`, `HF_TOKEN`, `VLLM_API_KEY` go in `docker\.env`.
- GPU: Docker Desktop with the WSL 2 backend and a current NVIDIA driver; uncomment the
  `deploy:` block in `compose.ollama.yml`.
- Any llmop command works through the container, e.g.
  `docker compose -f compose.ollama.yml run --rm --no-deps llmop check`.
