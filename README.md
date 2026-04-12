# Caddy Proxy Manager

A web UI to manage Caddy reverse proxy entries by combining **Caddy**, **Docker**, **Tailscale**, and **Cloudflare DNS** — all in one place.

Create, edit, and delete proxy entries with automatic DNS record management and optional SSL certificates (HTTP-01 or DNS-01 via Cloudflare). Both public and Tailscale-only deployments are supported.

---

## Features

- Browse running **Docker containers** and **Tailscale nodes** as proxy targets, or enter a custom host/IP
- Choose between **Public IP** or **Tailscale IP** as the source address
- Automatic **Cloudflare A record** creation and updates
- **SSL certificate** provisioning via Caddy (HTTP-01 auto or DNS-01 via Cloudflare)
- Enforces SSL compatibility rules (DNS-01 only for Tailscale-sourced entries)
- Real-time cert readiness polling with status badges
- Shows **unmanaged Cloudflare A records** with one-click removal
- Auto-refresh of proxy state on a configurable interval
- Dark mode support (follows OS preference)

---

## Screenshots

> _Screenshots coming soon_

<!-- Main page -->
![Main page](docs/screenshots/main.png)

<!-- Add entry form -->
![Add proxy entry](docs/screenshots/form.png)

---

## Deployment

### Prerequisites

- Docker + Docker Compose
- A Cloudflare API token with DNS edit permissions
- A Tailscale API key and tailnet name
- An email address for ACME certificate registration

### Quick start

```bash
# 1. Copy the example env file and fill in your values
cp .env.example .env
$EDITOR .env

# 2. Start the stack
docker compose up -d
```

The web UI is available at `http://<host>:8088`.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `CF_API_TOKEN` | Yes | Cloudflare API token — DNS management and Caddy DNS-01 |
| `TS_API_KEY` | Yes | Tailscale API key |
| `TS_TAILNET` | Yes | Tailscale tailnet name |
| `ACME_EMAIL` | Yes | Email for ACME certificate registration |
| `REFRESH_INTERVAL` | No | Proxy list refresh interval in seconds (default: `60`, min: `30`) |
| `DEBUG` | No | Set to `true` for verbose logging |
| `APP_SECRET` | No | NiceGUI session secret — auto-generated and persisted on first boot |

> `CF_API_TOKEN` is injected into the Caddy config payload at runtime and never written to disk.

---

## Architecture

```
┌──────────────┐     Admin API      ┌──────────────┐
│  Proxy UI    │ ─────────────────► │    Caddy     │  :80 / :443
│  (NiceGUI)   │                    │  (custom     │
│  :8088       │                    │   xcaddy     │
└──────┬───────┘                    │   build)     │
       │                            └──────────────┘
       │  Docker socket
       ▼
┌──────────────┐
│  Docker API  │  (running containers as proxy targets)
└──────────────┘

       │  Cloudflare API
       ▼
┌──────────────┐
│  Cloudflare  │  (A record management, DNS-01 challenge)
└──────────────┘

       │  Tailscale API
       ▼
┌──────────────┐
│  Tailscale   │  (node IP lookup, Tailscale-only routes)
└──────────────┘
```

Caddy and the app run as separate containers in the same Compose network. The Caddy Admin API is always reached at `http://caddy:2019`.

---

## Development

**Requirements:** Python 3.13+, [uv](https://github.com/astral-sh/uv)

```bash
# Install dependencies
uv sync

# Copy and configure environment
cp .env.example .env
$EDITOR .env

# Run the app
uv run python main.py

# Lint and format
uv run ruff check . --fix
uv run ruff format .
```

The app will be available at `http://localhost:8088`. It expects a running Caddy instance at `http://caddy:2019` — when developing locally you can point `CADDY_ADMIN_URL` or use an SSH tunnel / local Caddy instance.

---

## License

MIT — see [LICENSE](LICENSE) _(placeholder)_
