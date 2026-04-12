"""Application settings loaded from environment variables.

This module is the single source of truth for all configuration.
Import `settings` from here; never instantiate Settings elsewhere.
Infrastructure constants (DATA_DIR, CADDY_ADMIN_URL, APP_PORT) are also
defined here as module-level constants — they are not configurable via env.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Final

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Infrastructure constants — fixed by the Docker/compose setup.
# Volume-mount /data to persist config across container restarts.
CADDY_ADMIN_URL: Final[str] = "http://caddy:2019"
DATA_DIR: Final[pathlib.Path] = pathlib.Path("/data")
APP_PORT: Final[int] = 8088
CONFIG_FILE: Final[pathlib.Path] = DATA_DIR / "proxy_config.json"


class Settings(BaseSettings):
    """All application settings sourced from environment variables.

    Do not add infrastructure constants here — see module-level Finals above.
    """

    model_config = SettingsConfigDict(
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
    # Empty string is treated as unset so that `TS_HOST_NAME=` in an env file
    # doesn't accidentally suppress the local-socket auto-detection path.
    ts_host_name: str | None = None
    public_ip: str | None = None

    @field_validator("ts_host_name", "public_ip", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        """Convert empty string env vars to None, preserving Optional semantics."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    # Optional — periodic refresh interval for the main page (seconds).
    # Must be at least 30 to avoid hammering the Cloudflare API.
    refresh_interval: int = 60

    @field_validator("refresh_interval")
    @classmethod
    def validate_refresh_interval(cls, v: int) -> int:
        """Clamp refresh interval to a safe minimum."""
        if v < 30:
            raise ValueError(f"REFRESH_INTERVAL must be >= 30 seconds, got {v}")
        return v

    # Optional — logging verbosity
    debug: bool = False

    @field_validator("acme_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Basic format check — avoid adding email-validator as a dependency."""
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"Invalid ACME email address: {v!r}")
        return v


def configure_logging(debug: bool = False) -> None:
    """Configure root logger and silence noisy third-party loggers.

    Must be called once at application startup before any I/O begins.
    Does NOT silence the application's own loggers (core.*, ui.*).
    """
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s"))
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
