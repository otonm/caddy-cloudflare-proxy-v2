## Project Description

Web UI to manage Caddy reverse proxy entries by combining Caddy, Docker, Tailscale, and Cloudflare DNS.

A proxy entry is created by:
1. Choosing a target — a running Docker container, a Tailscale node, or a custom host/IP
2. Choosing a source IP — public/external or the Tailscale IP of the Caddy host
3. Optionally requesting SSL

Then the app creates/updates a Cloudflare A record and configures Caddy via its Admin API.

### SSL Rules

| Source IP | Available SSL methods |
|---|---|
| Public IP | None, HTTP-01 (automatic), DNS-01 via Cloudflare |
| Tailscale IP | None, DNS-01 via Cloudflare only |

The UI must enforce this — incompatible combinations must not be selectable.

---

## Stack

NiceGUI · FastAPI (via NiceGUI) · httpx · docker SDK · Pydantic v2 + pydantic-settings · aiofiles · uv · ruff

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `CF_API_TOKEN` | Yes | Cloudflare token — DNS management and Caddy DNS-01. Treat as secret (`SecretStr`). |
| `TS_API_KEY` | Yes | Tailscale API key (`SecretStr`) |
| `TS_TAILNET` | Yes | Tailscale tailnet name |
| `ACME_EMAIL` | Yes | Email for ACME certificate registration |
| `DEBUG` | No | Enables verbose logging if `true`. Never log secrets regardless. |

Load with `pydantic-settings`. Instantiate `Settings` once at startup, import everywhere.
Never write secrets to disk, logs, or `config.json`. Access raw secret values only when passing to an external API call.

`CF_API_TOKEN` is passed to Caddy at runtime by injecting it into the JSON config payload
sent to the Caddy Admin API — it is never stored in a file.

---

## Infrastructure

Caddy and the app run as separate containers in the same Compose network.
The app depends on Caddy being healthy before starting.
The Caddy Admin API is always reached at `http://caddy:2019` — never `localhost`.
The Docker socket is mounted into the app container for the Docker SDK.

---

## API References

Always look up current docs before implementing or modifying any integration. Use Context7
or web search. Do not rely on training data for endpoint paths, payloads, or auth details.

- **Caddy Admin API** — https://caddyserver.com/docs/api — use JSON config API only, not Caddyfile. For DNS-01, verify the exact Cloudflare provider JSON schema from current caddy-dns/cloudflare docs.
- **Docker SDK (Python)** — https://docker-py.readthedocs.io/en/stable/
- **Tailscale API** — https://tailscale.com/api — `GET /tailnet/{tailnet}/devices`, use first IPv4 in `addresses`
- **Cloudflare API** — https://developers.cloudflare.com/api — always check for an existing A record before creating; update if found. Set `proxied: false`.

---

## Code Standards

### Type System

All code must be fully typed. There are no exceptions.

- Every function and method must have type annotations on all parameters and return values
- Use `from __future__ import annotations` at the top of every file
- Use `typing` and `collections.abc` for complex types (`Sequence`, `Mapping`, `Callable`, etc.)
- Prefer `X | None` over `Optional[X]` (Python 3.10+ union syntax)
- Pydantic models count as typed — no need to re-annotate their fields
- `Any` is forbidden unless wrapping an external library that provides no types, and must be accompanied by a comment explaining why

Example:
```python
from __future__ import annotations

async def get_container_ip(container_name: str) -> str | None:
    ...
```

Before submitting any code, verify types are complete and consistent across all touched files.

### Async

All I/O-bound functions must be `async`. Never use blocking calls
(`open`, `requests`, `time.sleep`, synchronous docker SDK calls, etc.) in async
context. Use `aiofiles` for file I/O, `httpx.AsyncClient` for HTTP, and
`asyncio.sleep` for delays. If a blocking call is unavoidable, wrap it in
`asyncio.get_event_loop().run_in_executor()`.

### Logging

Use Python's standard `logging` module. Never use `print()` for
diagnostics. Every module gets its own logger:
```python
import logging
logger = logging.getLogger(__name__)
```

Use levels correctly:
- `DEBUG` — internal state, variable values, step-by-step flow. Only emitted when `DEBUG=true`.
- `INFO` — normal lifecycle events (app started, proxy created, DNS record updated).
- `WARNING` — recoverable issues or unexpected but handled conditions (container has no ports, record already up to date).
- `ERROR` — failures that affect the user or require action (API call failed, Caddy unreachable).

Always use f-strings to construct the log messages.

Never log secret values at any level. Log the intent and outcome, not the credential.
Good: `logger.info(f"Updating Cloudflare A record for {domain}")`
Bad: `logger.debug(f"Using token {token}")`

### Comments

Comment the *why*, not the *what*. Every module, class, and public
function gets a docstring. Inline comments are required for non-obvious logic,
external API quirks, and any workaround or assumption. Leave no ambiguity about
intent for future readers.

### Linting

After every change run:
```bash
uv run ruff check . --fix
uv run ruff format .
```
Both must pass clean. No `# noqa` suppressions without explanation.

### Project Management — uv

This project uses [uv](https://github.com/astral-sh/uv) for dependency and environment management.
Do not use pip, pipenv, or poetry.
```bash
uv sync                  # install all dependencies from pyproject.toml
uv add <package>         # add a new dependency
uv add --dev <package>   # add a dev dependency (e.g. ruff)
uv run python main.py    # run the app
uv run ruff check .      # lint
uv run ruff format .     # format
```

All dependencies must be declared in `pyproject.toml`. Never manually edit `uv.lock`.

### Architecture

Clear separation between the frontend and the backend.
`core/` has no UI imports. `ui/` has no business logic. All HTTP calls use
`httpx.AsyncClient` with explicit timeouts, always closed properly.

### Secrets

`SecretStr` for all secret env vars. Never log or include secret values in
exceptions.