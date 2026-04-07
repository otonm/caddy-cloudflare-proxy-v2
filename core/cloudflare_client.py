"""Async Cloudflare API client for DNS A-record management.

Responsibilities:
- Look up zone IDs (cached — immutable).
- Check, create, update, and delete A records.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_CF_BASE = "https://api.cloudflare.com/client/v4"

# Zone IDs are immutable — cache by zone name to avoid a lookup on every entry operation.
_zone_cache: dict[str, str] = {}


class CloudflareError(Exception):
    """Raised when a Cloudflare API call fails in an unrecoverable way."""


def _derive_zone_name(domain: str) -> str:
    """Extract the registrable zone from a domain name (last two labels).

    Warning: does not handle second-level TLDs (e.g. .co.uk) — zone
    derivation for those requires a public suffix list, which is out of scope.
    """
    parts = domain.rstrip(".").split(".")
    if len(parts) < 2:
        raise ValueError(f"Cannot derive zone from domain: {domain!r}")
    return ".".join(parts[-2:])


@contextlib.asynccontextmanager
async def _cf_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield a pre-configured httpx client for the Cloudflare API."""
    token = settings.cf_api_token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        base_url=_CF_BASE,
        headers=headers,
        timeout=httpx.Timeout(15.0),
    ) as client:
        yield client


def _check_response(response: httpx.Response, operation: str) -> None:
    """Raise CloudflareError if the API response indicates failure.

    Cloudflare can return HTTP 200 with "success": false in the body,
    so both the HTTP status and the success field must be checked.
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CloudflareError(f"Cloudflare API error during {operation}: HTTP {response.status_code}") from exc
    data = response.json()
    if not data.get("success"):
        errors = data.get("errors", [])
        raise CloudflareError(f"Cloudflare API {operation} failed: {errors}")


async def get_zone_id(domain: str) -> str:
    """Return the Cloudflare zone ID for the zone containing `domain`.

    Derives the zone name from the domain's last two labels (e.g., "app.example.com"
    → zone "example.com"). Result is cached — zone IDs are immutable.
    Raises CloudflareError if the zone is not found.
    """
    zone_name = _derive_zone_name(domain)
    if zone_name in _zone_cache:
        return _zone_cache[zone_name]
    logger.info(f"Looking up Cloudflare zone for {zone_name}")
    async with _cf_client() as client:
        response = await client.get("/zones", params={"name": zone_name})
        _check_response(response, f"get zone for {zone_name}")
        data = response.json()
    if not data.get("result"):
        raise CloudflareError(f"Zone not found for domain {domain!r} (zone={zone_name!r})")
    zone_id: str = data["result"][0]["id"]
    _zone_cache[zone_name] = zone_id
    return zone_id


async def get_a_record(zone_id: str, name: str) -> tuple[str, str] | None:
    """Look up an existing A record. Returns (record_id, ip) or None if not found."""
    async with _cf_client() as client:
        response = await client.get(
            f"/zones/{zone_id}/dns_records",
            params={"type": "A", "name": name},
        )
        _check_response(response, f"get A record for {name}")
        data = response.json()
    if not data.get("result"):
        return None
    record = data["result"][0]
    return record["id"], record["content"]


async def upsert_a_record(zone_id: str, name: str, ip: str) -> str:
    """Create or update an A record. Returns the DNS record ID.

    Always sets proxied=False — the Cloudflare proxy is disabled because Caddy
    needs the real IP for TLS certificate issuance, and Tailscale IPs cannot be
    proxied by Cloudflare. TTL=1 means automatic.
    """
    logger.info(f"Upserting Cloudflare A record: {name} → {ip}")
    existing = await get_a_record(zone_id, name)
    if existing:
        record_id, current_ip = existing
        if current_ip == ip:
            logger.info(f"A record for {name} already correct ({ip}), no update needed")
            return record_id
        logger.info(f"Updating A record for {name}: {current_ip} → {ip}")
        async with _cf_client() as client:
            response = await client.patch(
                f"/zones/{zone_id}/dns_records/{record_id}",
                json={"content": ip},
            )
            _check_response(response, f"update A record for {name}")
        return record_id
    logger.info(f"Creating A record for {name} → {ip}")
    async with _cf_client() as client:
        response = await client.post(
            f"/zones/{zone_id}/dns_records",
            json={"type": "A", "name": name, "content": ip, "ttl": 1, "proxied": False},
        )
        _check_response(response, f"create A record for {name}")
        return response.json()["result"]["id"]


async def delete_a_record(zone_id: str, record_id: str) -> None:
    """Delete a DNS A record by record ID.

    Raises CloudflareError if the record does not exist or deletion fails.
    """
    logger.info(f"Deleting Cloudflare A record {record_id}")
    async with _cf_client() as client:
        response = await client.delete(f"/zones/{zone_id}/dns_records/{record_id}")
        _check_response(response, f"delete record {record_id}")
