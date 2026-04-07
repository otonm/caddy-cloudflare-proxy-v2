"""General-purpose utilities used across the application.

Contains helpers that are not tied to any specific external service.
"""

from __future__ import annotations

import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


async def detect_public_ip() -> str:
    """Return the public IPv4 address of this host.

    Uses settings.public_ip if set (manual override). Otherwise queries
    api4.ipify.org — the '4' subdomain forces IPv4 on dual-stack hosts,
    which is required since A records must be IPv4.
    Raises RuntimeError if detection fails.
    """
    if settings.public_ip:
        logger.info(f"Using configured PUBLIC_IP: {settings.public_ip}")
        return settings.public_ip
    logger.info("Detecting public IP via api4.ipify.org")
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        try:
            response = await client.get("https://api4.ipify.org?format=json")
            response.raise_for_status()
            ip: str = response.json()["ip"]
            logger.info(f"Detected public IP: {ip}")
            return ip
        except Exception as exc:
            raise RuntimeError(f"Failed to detect public IP: {exc}") from exc
