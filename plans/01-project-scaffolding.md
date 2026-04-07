# Plan 01 — Project Scaffolding

## Goal

Establish the complete project skeleton: dependency manifest, directory layout, Docker
infrastructure, environment template, GitHub Actions CI, and all empty placeholder files.
After this plan, `uv sync` must install all declared packages and the GitHub Actions
workflow must be ready to build and push Docker images on push.

**Docker is not required on the development machine.** Images are built on GitHub CI
when code is pushed. Development and linting happen entirely via `uv`.

---

## Library Versions (pinned to latest stable, April 2026)

| Package | Version |
|---|---|
| Python | 3.13 |
| nicegui | 3.9.* |
| fastapi | 0.135.* |
| httpx | 0.28.* |
| docker (SDK) | >=7.1 |
| pydantic | >=2.9 |
| pydantic-settings | 2.13.* |
| aiofiles | 25.1.* |
| ruff | >=0.9 |

> **Docker SDK note**: The `docker` package is a runtime dependency used inside the
> container (Docker socket is mounted at `/var/run/docker.sock`). It does NOT require
> Docker to be installed on the development machine — it is a pure Python package.
> Graceful degradation is built in: if the socket is unavailable, container listing
> returns an empty list.

---

## Directory Layout

```
caddy-cloudflare-proxy-v2/
├── .github/
│   └── workflows/
│       └── docker-build.yml      # Build & push on push to main
├── core/
│   ├── __init__.py
│   ├── config.py             # (stub — Plan 02)
│   ├── models.py             # (stub — Plan 03)
│   ├── store.py              # (stub — Plan 03)
│   ├── docker_client.py      # (stub — Plan 04)
│   ├── tailscale_client.py   # (stub — Plan 05)
│   ├── cloudflare_client.py  # (stub — Plan 06)
│   ├── caddy_client.py       # (stub — Plan 07)
│   └── proxy_service.py      # (stub — Plan 08)
├── ui/
│   ├── __init__.py
│   ├── main_page.py          # (stub — Plan 09)
│   └── form_page.py          # (stub — Plan 10)
├── data/
│   └── .gitkeep              # volume mount point; empty in repo
├── plans/                    # these files
├── Dockerfile
├── Dockerfile.caddy
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── .gitignore
├── main.py                   # (stub — Plan 10)
└── CLAUDE.md
```

---

## pyproject.toml

The existing file only contains `[tool.ruff.*]`. Add the `[project]` and `[build-system]`
sections. Correct `target-version` from `py314` (unreleased) to `py313`.

```toml
[project]
name = "caddy-cloudflare-proxy-v2"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "nicegui>=3.9,<4",
    "fastapi>=0.135",
    "httpx>=0.28",
    "docker>=7.1",
    "pydantic>=2.9",
    "pydantic-settings>=2.13",
    "aiofiles>=25.1",
]

[project.optional-dependencies]
dev = [
    "ruff>=0.9",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py313"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ANN"]
ignore = ["ANN101", "ANN102"]

[tool.ruff.format]
quote-style = "double"
```

---

## Dockerfile (app)

```dockerfile
FROM python:3.13-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency manifest first for layer caching
COPY pyproject.toml .

# Install all dependencies (no dev extras in production image)
RUN uv sync --no-dev

# Copy application source
COPY core/ core/
COPY ui/ ui/
COPY main.py .

# Data directory for persistent JSON config (can be volume-mounted)
RUN mkdir -p /data

EXPOSE 8080

CMD ["uv", "run", "python", "main.py"]
```

---

## Dockerfile.caddy

Custom Caddy build that includes the Cloudflare DNS plugin required for DNS-01 challenges.
The standard `caddy` Docker image does NOT include third-party DNS providers. xcaddy
compiles the plugin into the binary at build time.

```dockerfile
FROM caddy:builder AS builder

RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare

FROM caddy:latest

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

> **Why xcaddy**: Caddy has no runtime plugin loading. DNS-01 with Cloudflare requires the
> `caddy-dns/cloudflare` module compiled in.

---

## docker-compose.yml

```yaml
services:
  caddy:
    build:
      context: .
      dockerfile: Dockerfile.caddy
    container_name: caddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - caddy_data:/data
      - caddy_config:/config
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:2019/config/"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    networks:
      - proxy_net

  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: caddy-proxy-app
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - app_data:/data
      - /var/run/docker.sock:/var/run/docker.sock:ro
    env_file:
      - .env
    depends_on:
      caddy:
        condition: service_healthy
    networks:
      - proxy_net

volumes:
  caddy_data:
  caddy_config:
  app_data:

networks:
  proxy_net:
    driver: bridge
```

> **Ports**: Users remap ports in `docker-compose.yml` as needed. The app container
> always listens on 8080 internally; publish it to whatever host port you prefer.

---

## GitHub Actions Workflow

`.github/workflows/docker-build.yml` — triggers on push to `main`, builds both images
and pushes to GitHub Container Registry (GHCR).

```yaml
name: Build & Push Docker Images

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE_APP: ghcr.io/${{ github.repository }}/app
  IMAGE_CADDY: ghcr.io/${{ github.repository }}/caddy

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --dev
      - run: uv run ruff check .
      - run: uv run ruff format --check .

  build-and-push:
    runs-on: ubuntu-latest
    needs: lint
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Log in to GHCR
        if: github.event_name == 'push'
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build & push app image
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile
          push: ${{ github.event_name == 'push' }}
          tags: ${{ env.IMAGE_APP }}:latest

      - name: Build & push Caddy image
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile.caddy
          push: ${{ github.event_name == 'push' }}
          tags: ${{ env.IMAGE_CADDY }}:latest
```

> **PR builds**: On pull requests, images are built but NOT pushed (no `push: true`).
> This validates the build without needing credentials.

---

## .env.example

```dotenv
# Required — Cloudflare API token with Zone:DNS:Edit permission
CF_API_TOKEN=your_cloudflare_api_token_here

# Required — Tailscale API key
TS_API_KEY=tskey-api-xxxxxxxxxxxxxxxxxxxxx

# Required — Tailscale tailnet (e.g. "myuser.github" or "example.com")
TS_TAILNET=your-tailnet-name

# Required — Email for ACME certificate registration
ACME_EMAIL=admin@example.com

# Optional — Tailscale hostname of THIS machine (the Caddy host) in the tailnet.
# Used to look up its Tailscale IP for source-IP=Tailscale proxy entries.
# If not set, Tailscale source IP will not be available as an option.
# TS_HOST_NAME=my-server

# Optional — Override detected public IP. If not set, auto-detected via api4.ipify.org.
# PUBLIC_IP=1.2.3.4

# Optional — Enable verbose debug logging (secrets are never logged regardless)
DEBUG=false
```

---

## .gitignore

```
__pycache__/
*.pyc
*.pyo
.venv/
.env
data/
*.log
dist/
.ruff_cache/
```

> **`uv.lock`**: Commit the lock file once dependencies stabilise — it enables
> reproducible builds in CI. It is excluded here during scaffolding only.

---

## Stub files

Every module file starts with:

```python
from __future__ import annotations
"""<module purpose> — implemented in Plan XX."""
```

`main.py` stub:

```python
from __future__ import annotations
"""Application entry point — implemented in Plan 10."""

if __name__ == "__mp_main__":
    pass
```

---

## Verification Steps (no Docker required)

1. `uv sync` — must complete without errors, creating `.venv/`
2. `uv run ruff check . --fix && uv run ruff format .` — must pass on all stubs
3. `uv run python -c "import nicegui, fastapi, httpx, docker, pydantic, aiofiles; print('OK')"`
   — all packages must import cleanly
4. Verify `.github/workflows/docker-build.yml` is valid YAML (use `python -c "import yaml; yaml.safe_load(open('.github/workflows/docker-build.yml'))"`)
