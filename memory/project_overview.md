---
name: Project Overview — caddy-cloudflare-proxy-v2
description: Purpose, stack, and planned architecture of the caddy-cloudflare-proxy-v2 project
type: project
---

Web UI to manage Caddy reverse proxy entries combining Caddy, Docker, Tailscale, and Cloudflare DNS.

**Why:** User needs a UI to create proxy entries without manually editing Caddy config or Cloudflare DNS.

**Stack:** NiceGUI + FastAPI (via NiceGUI) · httpx · docker SDK · Pydantic v2 + pydantic-settings · aiofiles · uv · ruff

**Key constraint:** SSL/source-IP compatibility matrix:
- PUBLIC source IP → NONE, HTTP-01, DNS-01
- TAILSCALE source IP → NONE, DNS-01 only (no HTTP-01)

**Infrastructure:** Caddy + app in same Compose network. Caddy Admin API at http://caddy:2019. Docker socket read-only mounted.

**How to apply:** 10 sequential implementation plans live in plans/ directory. Always follow the plan order. Each plan builds on the previous.

**Plans written (2026-04-07):** plans/01 through plans/10 covering: scaffolding → settings → models/store → docker client → tailscale client → cloudflare client → caddy client → proxy service → UI main page → UI form + main.py
