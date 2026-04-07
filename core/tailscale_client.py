"""Async Tailscale API client for device discovery and host IP resolution.

Serves two purposes:
1. Listing all tailnet devices for the target dropdown (target_type = TAILSCALE).
2. Resolving the Caddy host's Tailscale IP for source_ip_type = TAILSCALE A records.

Host IP detection priority (see get_caddy_host_ip):
  1. TS_HOST_NAME env var — explicit config, matched against the Tailscale API device list.
  2. Local daemon socket — queries the Tailscale daemon directly via Unix socket if mounted.
     Tries /run/tailscale/tailscaled.sock (Linux) then /var/run/tailscale/tailscaled.sock
     (macOS / alternative Linux paths).  Requires the socket to be volume-mounted into the
     container; no additional configuration is needed.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Any

import httpx

from core.config import settings
from core.models import TailscaleDevice

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.tailscale.com/api/v2"

# Candidate socket paths tried in order when TS_HOST_NAME is not configured.
# The daemon socket exposes the Tailscale local API without requiring an API key.
_TAILSCALE_SOCKET_PATHS = (
    "/run/tailscale/tailscaled.sock",  # standard Linux path
    "/var/run/tailscale/tailscaled.sock",  # macOS / alternative Linux path
)


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


async def _get_ip_from_local_socket(socket_path: str) -> str | None:
    """Return the local device's Tailscale IPv4 by querying the daemon socket.

    The Tailscale local API at /localapi/v0/status returns the running device's own
    TailscaleIPs list without requiring an API key — authentication is handled by
    filesystem permissions on the socket file.

    Returns None if the socket does not exist, is not accessible, or the response
    cannot be parsed.  All errors are logged at DEBUG level so startup remains quiet
    when the socket is simply not mounted.
    """
    if not os.path.exists(socket_path):
        logger.debug(f"Tailscale socket not found: {socket_path}")
        return None
    try:
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0)) as client:
            # The URL host is irrelevant when using a Unix socket; only the path matters.
            response = await client.get("http://local/localapi/v0/status?peers=false")
            response.raise_for_status()
            data: dict[str, Any] = response.json()  # untyped external JSON, Any is justified
        ips: list[str] = data.get("TailscaleIPs") or []
        ip = _first_ipv4(ips)
        if ip:
            logger.info(f"Resolved Caddy Tailscale IP via local socket {socket_path}: {ip}")
        else:
            # Log response keys to help diagnose unexpected response shapes.
            logger.warning(
                f"Local socket {socket_path} returned no IPv4 — TailscaleIPs={ips!r}, response keys={list(data.keys())}"
            )
        return ip
    except Exception as exc:
        # Broad catch: httpx errors, OSError (permission/connection), JSON parse errors, etc.
        # All logged at WARNING so they are visible without DEBUG=true.
        logger.warning(f"Tailscale socket {socket_path} failed ({type(exc).__name__}): {exc}")
        return None


async def get_caddy_host_ip() -> str | None:
    """Return the Tailscale IPv4 address of the machine running Caddy.

    Detection priority:
    1. TS_HOST_NAME — if set, matched against the device list from the Tailscale API.
       Supports short hostname (e.g. "my-server") and FQDN prefix matching.
    2. Local daemon socket — tries each path in _TAILSCALE_SOCKET_PATHS in order.
       Requires the socket to be volume-mounted into the container; returns the
       first IPv4 reported by the daemon for this machine.
    3. Returns None if all methods fail, logging a warning.
    """
    if settings.ts_host_name:  # empty string treated as unset — fall through to socket
        # Explicit config: match against the cloud API device list.
        devices = await list_devices()
        target = settings.ts_host_name.lower()
        for device in devices:
            if device.hostname.lower() == target or device.name.lower().startswith(target + "."):
                logger.debug(f"Found Caddy host in tailnet: {device.hostname} → {device.ip}")
                return device.ip
        logger.warning(f"TS_HOST_NAME={settings.ts_host_name!r} not found in tailnet devices")
        return None

    # No explicit config: try the local daemon socket.
    for socket_path in _TAILSCALE_SOCKET_PATHS:
        ip = await _get_ip_from_local_socket(socket_path)
        if ip:
            return ip

    logger.warning(
        "Could not determine Caddy Tailscale IP: set TS_HOST_NAME or mount the "
        "Tailscale socket at /run/tailscale/tailscaled.sock"
    )
    return None
