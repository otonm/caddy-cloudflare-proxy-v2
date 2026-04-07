"""Async Tailscale API client for device discovery and host IP resolution.

Serves two purposes:
1. Listing all tailnet devices for the target dropdown (target_type = TAILSCALE).
2. Resolving the Caddy host's Tailscale IP for source_ip_type = TAILSCALE A records.

Host IP detection priority (see get_caddy_host_ip):
  1. TS_HOST_NAME env var — explicit config, matched against the Tailscale API device list.
  2. Docker host name — reads the host machine's hostname via the mounted Docker socket
     (docker info → Name), then matches it against the device list.  Requires no extra
     mounts or configuration beyond the Docker socket already used for container discovery.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import httpx

from core.config import settings
from core.docker_client import get_docker_host_name
from core.models import TailscaleDevice

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.tailscale.com/api/v2"


def _first_ipv4(addresses: list[str]) -> str | None:
    """Return the first IPv4 address from a mixed IPv4/IPv6 list.

    Tailscale returns both 100.x.x.x (IPv4) and fd7a::... (IPv6) addresses.
    The ipaddress module is the correct tool to distinguish them — not string matching.
    """
    for addr in addresses:
        try:
            if isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address):
                return addr
        except ValueError:
            continue
    return None


async def list_devices() -> list[TailscaleDevice]:
    """Fetch all devices in the configured tailnet.

    Returns an empty list if the API call fails — callers must handle this gracefully.
    """
    token = settings.ts_api_key.get_secret_value()
    url = f"{_BASE_URL}/tailnet/{settings.ts_tailnet}/devices"
    logger.info(f"Fetching Tailscale devices for tailnet {settings.ts_tailnet}")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            data: dict[str, Any] = response.json()  # untyped external JSON, Any is justified
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            logger.error("Tailscale API auth failed — check TS_API_KEY")
        elif status == 403:
            logger.error("Tailscale API insufficient permissions — check TS_API_KEY scopes")
        else:
            logger.error(f"Tailscale API returned HTTP {status}")
        return []
    except httpx.HTTPError as exc:
        logger.error(f"Tailscale API request failed: {exc}")
        return []
    except ValueError as exc:
        # json.JSONDecodeError is a subclass of ValueError
        logger.error(f"Tailscale API returned invalid JSON: {exc}")
        return []

    devices: list[TailscaleDevice] = []
    for raw in data.get("devices", []):
        ip = _first_ipv4(raw.get("addresses", []))
        if ip is None:
            logger.warning(f"Tailscale device {raw.get('hostname')!r} has no IPv4 address, skipping")
            continue
        devices.append(
            TailscaleDevice(
                name=raw["name"],
                hostname=raw["hostname"],
                ip=ip,
            )
        )
    logger.debug(f"Found {len(devices)} Tailscale devices")
    return devices


async def get_caddy_host_ip() -> str | None:
    """Return the Tailscale IPv4 address of the machine running Caddy.

    Detection priority:
    1. TS_HOST_NAME env var — explicit config takes precedence.
    2. Docker host name — reads the host machine's hostname via the mounted Docker
       socket (docker info → Name) and matches it against the device list.
    Supports short hostname (e.g. "my-server") and FQDN prefix matching.
    Returns None if no hostname can be determined or it is not found in the tailnet.
    """
    target = settings.ts_host_name or await get_docker_host_name()
    if not target:
        logger.warning("Could not determine Caddy host name — set TS_HOST_NAME or mount the Docker socket")
        return None

    devices = await list_devices()
    target_lower = target.lower()
    for device in devices:
        if device.hostname.lower() == target_lower or device.name.lower().startswith(target_lower + "."):
            logger.info(f"Resolved Caddy Tailscale IP: {device.hostname} → {device.ip}")
            return device.ip

    logger.warning(f"Host {target!r} not found in tailnet devices")
    return None
