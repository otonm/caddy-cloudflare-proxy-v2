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
SECRET_FILE: Final[pathlib.Path] = DATA_DIR / "app_secret.txt"


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

    # Optional — encrypts NiceGUI browser session storage.
    # If not set, a secret is auto-generated and persisted in SECRET_FILE.
    app_secret: SecretStr | None = None

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


def resolve_app_secret() -> str:
    """Return the NiceGUI storage secret, generating and persisting one if needed.

    Resolution order:
    1. APP_SECRET env var — explicit, highest priority.
    2. Persisted file at SECRET_FILE — survives container restarts.
    3. Auto-generated on first boot — written to SECRET_FILE and logged so the
       operator can capture the value and promote it to an env var if desired.

    This function is intentionally synchronous: it runs before the uvicorn
    event loop starts, so blocking file I/O is safe here.
    """
    import secrets as _secrets

    if settings.app_secret is not None:
        return settings.app_secret.get_secret_value()

    if SECRET_FILE.exists():
        secret = SECRET_FILE.read_text(encoding="utf-8").strip()
        if secret:
            logger.debug(f"Loaded APP_SECRET from {SECRET_FILE}")
            return secret

    # First boot: generate, persist, and announce.
    secret = _secrets.token_hex(32)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_text(secret, encoding="utf-8")
    logger.warning(
        f"APP_SECRET not set — generated a new secret and saved to {SECRET_FILE}. "
        "To use a stable value across reinstalls, add it to your .env:\n"
        f"  APP_SECRET={secret}"
    )
    return secret


# Module-level singleton — import this everywhere.
# Instantiated once; any ValidationError here means misconfigured environment.
settings: Settings = Settings()
