# OpenCode IP Rotator & Proxy Server

[![GitHub release](https://img.shields.io/github/v/release/alztrk/opencode-ip-rotator?style=flat-square&color=blue)](https://github.com/alztrk/opencode-ip-rotator)
[![Docker](https://img.shields.io/badge/docker-compose-blue.svg?style=flat-square&logo=docker)](https://github.com/alztrk/opencode-ip-rotator)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg?style=flat-square&logo=python)](https://python.org)

A modular, lightweight proxy server for OpenCode's free inference tier. It handles upstream protocol requirements, TLS cipher matching, model auto-discovery, SSE streaming, and outbound IP rotation.

Fork maintained by [@tirforge](https://github.com/tirforge). The web dashboard uses a [Tokyo Night](https://github.com/folke/tokyonight.nvim) palette in a flat, shadcn-style layout.

![OpenCode IP Rotator Dashboard Preview](docs/dashboard_preview.jpg)

---

## Features

- **OpenAI & Anthropic Compatible:** Exposes `/v1/chat/completions`, `/v1/messages`, and `/v1/models` for OpenCode, Claude clients, Cursor, or standard SDKs.
- **Protocol & TLS Compatibility:** Injects required OpenCode session headers, environment markers, and matches OpenSSL TLS 1.2 ciphers expected by upstream Cloudflare gates.
- **Dynamic Model Discovery:** Periodically checks OpenCode's catalog and automatically exposes all free models (including `big-pickle`) without manual configuration.
- **IP Rotation & Proxy Pool:** Rotates egress IPs via Cloudflare WARP or through a custom proxy pool defined in `proxies.txt`.
- **Stream Safety:** Protects active SSE streaming sessions from IP rotation interruptions until generation completes.
- **Web Dashboard & Metrics:** Real-time web UI at `/dashboard` displaying token counts, verified public IP, active connections, and SQLite history. The `tok/s` figure is generated output tokens (reasoning + visible text) divided by the first-to-last streamed chunk, so it reflects real generation speed rather than round-trip latency.

---

## Project Structure

```
opencode-ip-rotator/
├── server.py              # FastAPI application entrypoint
├── rotator.py             # Cloudflare WARP IP rotation service
├── core/
│   ├── config.py          # Centralized configuration and timeouts
│   ├── db.py              # SQLite storage for IP history and token metrics
│   └── metrics.py         # In-memory and Prometheus metrics
├── routes/
│   ├── chat.py            # /v1/chat/completions, /v1/messages, /v1/responses
│   ├── models.py          # /v1/models catalog endpoint
│   └── system.py          # /health, /status, /metrics, /dashboard
├── services/
│   ├── discovery.py       # OpenCode free model auto-discovery
│   ├── proxy_pool.py      # Round-robin outbound proxy rotation
│   └── upstream.py        # OpenCode HTTP transport, TLS ciphers & stream handling
├── proxies.txt            # Optional list of custom outbound proxies
├── docker-compose.yml     # Multi-container Docker deployment
└── tests/                 # Automated unit and integration tests
```

---

## Quickstart

### Method 1: Docker (Recommended)

1. Clone and start the containers:
   ```bash
   docker compose up -d --build
   ```

2. Open the dashboard:
   Navigate to `http://localhost:8000/dashboard`

3. Stop the services:
   ```bash
   docker compose down
   ```

### Method 2: Local Native Run

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Start the proxy server:
   ```bash
   python server.py
   ```

3. (Optional) Run the Cloudflare WARP rotator if `warp-cli` is installed locally:
   ```bash
   python rotator.py
   ```

---

## Deploy on Vercel (Hobby plan)

> **Read this first.** Vercel Functions are serverless: no Docker, no `warp-cli`,
> no long-running process and no persistent disk. **Cloudflare WARP IP rotation
> cannot run on Vercel.** What *does* run is the proxy itself (protocol headers,
> TLS impersonation, SSE streaming, model discovery) plus proxy-pool rotation
> through `PROXY_LIST`. Metrics live in `/tmp` per instance and the dashboard is
> per-instance/best-effort. If you need real WARP IP rotation, use the Docker
> compose setup above on a VPS.
>
> Vercel egress IPs are shared AWS addresses, so OpenCode Zen may rate-limit
> (HTTP 429) more often than a residential/WARP IP. This fork does not hide that.

### 1. Import the repository

Fork/clone is already public at `tirforge/opencode-ip-rotator`. In Vercel:

1. **Add New → Project → Import Git Repository** and pick the fork.
2. Framework preset: **FastAPI** (auto-detected from `server.py` + `requirements.txt`).
3. Root directory: repository root. Build settings can stay empty.

### 2. Set the API key (required)

In **Project → Settings → Environment Variables**, add:

| Name | Value |
| --- | --- |
| `PROXY_API_KEY` | a long random secret, e.g. `openssl rand -hex 32` |

Without `PROXY_API_KEY`, the Vercel deployment **fails closed** and returns
`503` for every proxy route. Multiple clients can share multiple keys by
comma-separating them: `key-one,key-two`.

Optional hardening variables:

| Name | Default | Purpose |
| --- | --- | --- |
| `PROXY_RATE_LIMIT` | `60` | Requests per key + IP per window |
| `PROXY_RATE_WINDOW` | `60` | Window length in seconds |
| `PROXY_IP_ALLOWLIST` | empty | Comma-separated client IPs allowed to connect |
| `PROXY_LIST` | empty | Round-robin outbound proxies (`http://user:pass@host:port,...`) |
| `CORS_ALLOW_ORIGINS` | empty | Only needed for browser clients |

### 3. Deploy and verify

```bash
# Unauthenticated -> 401
curl -i https://<your-app>.vercel.app/v1/models

# Authenticated -> model list
curl https://<your-app>.vercel.app/v1/models \
  -H "Authorization: Bearer $PROXY_API_KEY"

# Streaming chat completion
curl -N https://<your-app>.vercel.app/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"big-pickle","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

Dashboard: open `https://<your-app>.vercel.app/dashboard?key=<PROXY_API_KEY>`.
The key is exchanged for an HttpOnly cookie and removed from the URL.

### Vercel limits that matter

- **Max duration:** 300s on Hobby (Fluid compute). `STREAM_TIMEOUT` defaults to 120s.
- **Request body:** 4.5 MB.
- **Filesystem:** read-only except `/tmp` (ephemeral, per instance).
- **State:** in-memory metrics are per instance, so counters reset on cold starts.
- **Rotation:** only `PROXY_LIST` / `CUSTOM_OUTBOUND_PROXY`, never WARP.

---

## Client Configuration

### OpenCode (`~/.config/opencode/opencode.jsonc`)

#### Modern Schema (`providers`)

Add the local proxy using the current OpenCode provider schema:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "opencode-ip-rotator": {
      "name": "OpenCode IP Rotator",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "https://<your-app>.vercel.app/v1",
        "apiKey": "<PROXY_API_KEY>"
      },
      "models": {
        "big-pickle": {
          "name": "Big Pickle"
        }
      }
    }
  }
}
```

#### Legacy Schema (`provider`)

For older OpenCode installations using the deprecated `@ai-sdk` schema:

```jsonc
{
  "provider": {
    "opencode-ip-rotator": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "any"
      },
      "name": "OpenCode IP Rotator"
    }
  }
}
```

Or configure it as an Anthropic provider:

```jsonc
{
  "provider": {
    "my-anthropic-proxy": {
      "npm": "@ai-sdk/anthropic",
      "options": {
        "baseURL": "http://127.0.0.1:8000",
        "apiKey": "any"
      }
    }
  }
}
```

---

## Outbound Proxy Pool

If you have residential or datacenter proxies, you can route upstream requests through them:

1. Add your proxies to `proxies.txt` (one per line):
   ```text
   http://username:password@proxy1.example.com:8080
   socks5://proxy2.example.com:1080
   ```

2. Or set the `PROXY_LIST` environment variable:
   ```bash
   PROXY_LIST="http://proxy1:8080,socks5://proxy2:1080" docker compose up -d
   ```

Requests rotate across the pool in round-robin order.

---

## Endpoints

| Endpoint | Method | Auth | Description |
| :--- | :--- | :--- | :--- |
| `/v1/chat/completions` | `POST` | key | OpenAI-compatible chat completion (streaming and non-streaming) |
| `/v1/messages` | `POST` | key | Anthropic-compatible chat completion |
| `/v1/models` | `GET` | key | Currently active free models with their full OpenCode catalog metadata |
| `/dashboard` | `GET` | key | Web management dashboard (`?key=<PROXY_API_KEY>`) |
| `/metrics` | `GET` | key | Public IP, token metrics, and operational stats (JSON or Prometheus) |
| `/health` | `GET` | public | Health check probe |

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PROXY_API_KEY` | `""` | **Required on Vercel.** One key or comma-separated keys. Service fails closed when missing. |
| `PROXY_RATE_LIMIT` | `60` | Requests allowed per key + IP per window |
| `PROXY_RATE_WINDOW` | `60` | Rate-limit window in seconds |
| `PROXY_IP_ALLOWLIST` | `""` | Comma-separated client IP allowlist |
| `PORT` | `8000` | Port for the proxy server |
| `DEFAULT_MODEL` | `big-pickle` | Fallback model if omitted by the client |
| `ALL_MODELS` | `0` | Accept any model id and route unknown ones to `ALL_MODELS_TARGET` |
| `ALL_MODELS_TARGET` | `""` | Free model used for unknown ids when `ALL_MODELS=1` |
| `MODEL_ALIASES` | `{}` | JSON map of model id → free model, e.g. `{"gpt-4o":"big-pickle"}` |
| `DEFAULT_REASONING_EFFORT` | `""` | Effort applied when the client sends none (`chat`/`responses`); `#effort` and client values win |
| `WARP_PROXY_URL` | `http://127.0.0.1:40000` | Local Cloudflare WARP proxy created by `warp-cli mode proxy` |
| `WARP_POOL` | `""` | Comma-separated HTTP proxies, one per independent WARP registration (`usque http-proxy`). Rotation switches exits instantly and always changes IP |
| `WARP_ROTATOR_URL` | `http://warp-rotator:8001` | Rotator API; the proxy rotates and retries once on upstream 429 |
| `WARP_ROTATION_INTERVAL` | `300` | Seconds between scheduled WARP rotations; `0` disables |
| `MODEL_DISCOVERY_INTERVAL` | `300` | Interval in seconds to refresh free models |
| `STREAM_TIMEOUT` | `120` | Maximum timeout for upstream streaming requests |
| `PROXY_LIST_FILE` | `data/proxies.txt` | Path to custom proxy pool file |
| `CUSTOM_OUTBOUND_PROXY` | `""` | Single static outbound proxy URL |
| `CORS_ALLOW_ORIGINS` | `""` | Comma-separated browser origins allowed to call the API |
| `WARP_ROTATOR_URL` | `http://warp-rotator:8001` | Internal URL for the WARP rotator service (Docker only) |

---

## Testing

Run unit and integration tests:

```bash
pytest
```

Run end-to-end smoke tests against a live instance:

```bash
python test.py
```

---

## License

This project is licensed under the [MIT License](LICENSE).
