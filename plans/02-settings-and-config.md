# Plan 02 — Settings & Configuration

## Goal

Implement `core/config.py`: a `pydantic-settings` `Settings` class that loads and
validates all environment variables at startup, configures the logging system, and
exposes a singleton `settings` instance imported everywhere. After this plan, every
other module can do `from core.config import settings` and rely on validated values.

Intentionally minimal — only genuinely configurable values are env vars. Infrastructure
constants (`/data`, `http://caddy:2019`, port `8080`) are hardcoded; users remap
them at the Docker/compose layer, not at the application layer.

---

## Dependencies on Previous Plans

- Plan 01 must be complete (`pyproject.toml` includes `pydantic-settings>=2.13`).

---

## File: `core/config.py`

### Environment Variables

| Variable       | Type        | Required | Default | Description                                      |
|----------------|-------------|----------|---------|--------------------------------------------------|
| `CF_API_TOKEN` | `SecretStr` | Yes      | —       | Cloudflare token — DNS management and Caddy DNS-01 |
| `TS_API_KEY`   | `SecretStr` | Yes      | —       | Tailscale API key                                |
| `TS_TAILNET`   | `str`       | Yes      | —       | Tailscale tailnet identifier                     |
| `ACME_EMAIL`   | `str`       | Yes      | —       | ACME certificate registration email             |
| `TS_HOST_NAME` | `str\|None` | No       | `None`  | Tailscale hostname of this host; enables Tailscale source IP |
| `PUBLIC_IP`    | `str\|None` | No       | `None`  | Override for public IP; if None, detected at runtime via ipify |
| `DEBUG`        | `bool`      | No       | `False` | Enable verbose logging                           |

### Infrastructure Constants (NOT env vars)

These are fixed values that never need to change at runtime. Document them as module-level
constants so they are easy to find, but do NOT expose them as env vars:

```python
CADDY_ADMIN_URL: Final[str] = "http://caddy:2019"
DATA_DIR: Final[pathlib.Path] = pathlib.Path("/data")
APP_PORT: Final[int] = 8080
CONFIG_FILE: Final[pathlib.Path] = DATA_DIR / "proxy_config.json"
```

> **Why not configurable**: `CADDY_ADMIN_URL` is always `http://caddy:2019` on the
> internal Docker network — that's the whole point of the compose setup. `DATA_DIR` is
> `/data` by convention and users volume-mount the folder. `APP_PORT` is always `8080`
> inside the container; the host-side port is mapped in `docker-compose.yml`. Exposing
> these as env vars adds complexity with zero practical benefit.

### Implementation Notes

- Use `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")`
  so the app loads from `.env` during local development but uses real env vars in Docker.
- All secret fields use `pydantic.SecretStr`. Access the raw value only at the call site
  where it's passed to an external API. Never store `.get_secret_value()` in a variable
  that might be logged or propagated.
- `ACME_EMAIL` validated via `@field_validator` checking for `@` and `.` — avoids adding
  the `email-validator` dependency.
- The `settings` singleton is instantiated at module level — NOT inside a function.

---

## Code

```python
from __future__ import annotations
"""Application settings loaded from environment variables.

This module is the single source of truth for all configuration.
Import `settings` from here; never instantiate Settings elsewhere.
Infrastructure constants (DATA_DIR, CADDY_ADMIN_URL, APP_PORT) are also
defined here as module-level constants — they are not configurable via env.
"""

import logging
import pathlib
from typing import ClassVar, Final

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Infrastructure constants — fixed by the Docker/compose setup.
# Volume-mount /data to persist config across container restarts.
CADDY_ADMIN_URL: Final[str] = "http://caddy:2019"
DATA_DIR: Final[pathlib.Path] = pathlib.Path("/data")
APP_PORT: Final[int] = 8080
CONFIG_FILE: Final[pathlib.Path] = DATA_DIR / "proxy_config.json"


class Settings(BaseSettings):
    """All application settings sourced from environment variables.

    Do not add infrastructure constants here — see module-level Finals above.
    """

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Required secrets — never log these
    cf_api_token: SecretStr
    ts_api_key: SecretStr

    # Required plain strings
    ts_tailnet: str
    acme_email: str

    # Optional — source IP resolution
    ts_host_name: str | None = None
    public_ip: str | None = None

    # Optional — logging verbosity
    debug: bool = False

    @field_validator("acme_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Basic format check — avoid adding email-validator as a dependency."""
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"Invalid ACME email address: {v!r}")
        return v

    @model_validator(mode="after")
    def log_optional_warnings(self) -> Settings:
        """Emit informational warnings for optional vars that limit functionality."""
        # Can't use logger here — logging not yet configured when Settings is instantiated.
        # Warnings are emitted later by proxy_service.initialize() after logging is set up.
        return self


def configure_logging(debug: bool = False) -> None:
    """Configure root logger and silence noisy third-party loggers.

    Must be called once at application startup before any I/O begins.
    Does NOT silence the application's own loggers (core.*, ui.*).
    """
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Silence noisy third-party loggers regardless of DEBUG flag
    for name in ("httpx", "httpcore", "docker", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("nicegui").setLevel(logging.INFO)


# Module-level singleton — import this everywhere.
# Instantiated once; any ValidationError here means misconfigured environment.
settings: Settings = Settings()
```

---

## Verification Steps

1. With a valid `.env`, run:
   ```bash
   uv run python -c "from core.config import settings, CADDY_ADMIN_URL, DATA_DIR; print(settings.ts_tailnet, CADDY_ADMIN_URL, DATA_DIR)"
   ```
   Must print without raising.

2. Remove a required var from `.env` and verify `ValidationError` is raised with a
   clear message identifying the missing field.

3. Set `ACME_EMAIL=notanemail` and verify `ValueError: Invalid ACME email`.

4. ```bash
   uv run ruff check core/config.py --fix && uv run ruff format core/config.py
   ```
   Must pass clean.
