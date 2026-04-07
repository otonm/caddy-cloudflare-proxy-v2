"""Proxy orchestration service — coordinates all backend clients.

This is the single entry point for all business operations. The UI layer calls
only this module; it never calls lower-level clients (Caddy, Cloudflare, Docker,
Tailscale) directly. This enforces the clean separation described in CLAUDE.md.

Startup sequence:
  1. initialize() — resolve public IP and Caddy's Tailscale IP.
  2. health_check() — verify Caddy Admin API is reachable.
  3. sync_caddy_config() — replay the persisted entries into Caddy.

All three steps are wrapped in startup(), which main.py calls via on_startup.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import core.store as store
from core.caddy_client import CaddyError, apply_config, health_check
from core.cloudflare_client import (
    CloudflareError,
    delete_a_record,
    get_a_record,
    list_zones,
    upsert_a_record,
)
from core.docker_client import list_running_containers
from core.models import CloudflareZone, ProxyEntry, ProxyTarget, SourceIPType, SSLMethod, TargetType
from core.tailscale_client import get_caddy_host_ip, list_devices
from core.utils import detect_public_ip

logger = logging.getLogger(__name__)

# Module-level state — set once by initialize() at startup.
# Single-process app: module globals are safe; no locking needed for reads after startup.
_public_ip: str | None = None
_tailscale_ip: str | None = None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_ip(source_ip_type: SourceIPType) -> str:
    """Return the cached IP for the given source type.

    Raises ValueError if the required IP was not resolved at startup.
    Shared by create_entry() and update_entry() to avoid duplication.
    """
    if source_ip_type == SourceIPType.PUBLIC:
        if _public_ip is None:
            raise ValueError("Public IP not available — initialization may have failed")
        return _public_ip
    elif source_ip_type == SourceIPType.TAILSCALE:
        if _tailscale_ip is None:
            raise ValueError("Tailscale source IP not available. Set TS_HOST_NAME to enable Tailscale source IP.")
        return _tailscale_ip
    else:
        # Defensive: SourceIPType is an enum, this branch should never be reached.
        raise ValueError(f"Unknown source IP type: {source_ip_type!r}")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def initialize() -> None:
    """Resolve and cache the public IP and Caddy host's Tailscale IP.

    Must be called once at startup, before any proxy entry operations.
    Raises RuntimeError if the public IP cannot be determined — it is a hard
    requirement because every PUBLIC source_ip_type entry depends on it.
    Tailscale IP is optional; a warning is logged if unavailable.
    """
    global _public_ip, _tailscale_ip

    logger.info("Initializing proxy service")

    # Detect public IP — required; all PUBLIC entries depend on it.
    try:
        _public_ip = await detect_public_ip()
        logger.info(f"Public IP resolved: {_public_ip}")
    except RuntimeError as exc:
        logger.error(f"Cannot determine public IP: {exc}")
        raise RuntimeError("Public IP is required but could not be determined") from exc

    # Resolve Caddy host Tailscale IP — optional; TAILSCALE entries depend on it.
    _tailscale_ip = await get_caddy_host_ip()
    if _tailscale_ip:
        logger.info(f"Caddy Tailscale IP resolved: {_tailscale_ip}")
    else:
        logger.warning(
            "Caddy Tailscale IP not available — proxy entries with source_ip_type=TAILSCALE will be rejected"
        )


async def sync_caddy_config() -> None:
    """Reload all entries from the store and apply to Caddy.

    Called at startup (after initialize) and after every entry CRUD operation.
    This "rebuild from scratch" approach guarantees Caddy is always in sync
    with the store — no incremental patch logic required.

    Raises CaddyError if Caddy rejects the config or is unreachable.
    """
    entries = await store.list_entries()
    await apply_config(entries, _tailscale_ip, _public_ip or "")
    logger.debug(f"Caddy config synced with {len(entries)} entries")


async def startup() -> None:
    """Full startup sequence called from main.py via app.on_startup.

    Runs initialize(), health-checks Caddy, then syncs the config.
    If Caddy is unreachable at startup, logs an error but does not raise —
    the app starts anyway, and Caddy may recover (e.g. container restart race).
    """
    await initialize()

    healthy = await health_check()
    if not healthy:
        logger.error(
            "Caddy Admin API is not reachable at startup — check Caddy container. "
            "Config will not be synced until Caddy is available."
        )
        return

    await sync_caddy_config()
    logger.info("Proxy service started and Caddy config synchronized")


# ---------------------------------------------------------------------------
# Entry CRUD
# ---------------------------------------------------------------------------


async def create_entry(entry: ProxyEntry) -> ProxyEntry:
    """Create a new proxy entry end-to-end.

    Steps:
    1. Resolve the source IP (public or Tailscale).
    2. Upsert the Cloudflare A record.
    3. Persist the entry to the store.
    4. Apply the updated Caddy config.

    If step 4 (Caddy) fails, the entry is removed from the store (partial rollback).
    The DNS record is NOT rolled back — DNS changes may have propagated, and removing
    the record is more disruptive than leaving a stale one.

    Raises:
        ValueError: source IP type is unavailable.
        DomainExistsError: domain already has an entry (from store).
        CloudflareError: DNS upsert failed.
        CaddyError: Caddy rejected the config (entry rolled back from store).
    """
    ip = _resolve_ip(entry.source_ip_type)

    logger.info(f"Creating proxy entry: {entry.domain} → {entry.target_value}")

    await upsert_a_record(entry.zone_id, entry.domain, ip)

    saved_entry = await store.add_entry(entry)

    try:
        await sync_caddy_config()
    except CaddyError:
        # Rollback: remove from store so the app stays consistent.
        # DNS record is intentionally left in place — see docstring.
        logger.error(f"Caddy config failed after storing {entry.domain} — rolling back store entry")
        await store.delete_entry(saved_entry.id)
        raise

    logger.info(f"Proxy entry created: {entry.domain} → {entry.target_value}")
    return saved_entry


async def update_entry(entry: ProxyEntry) -> ProxyEntry:
    """Update an existing proxy entry end-to-end.

    Steps:
    1. Resolve and upsert the Cloudflare A record (IP may differ if source type changed).
    2. Update the entry in the store.
    3. Apply the updated Caddy config.

    Domain is read-only on edit (enforced by the UI). If domain change support is
    added later, old DNS record cleanup and new record creation must be handled here.

    Raises:
        ValueError: source IP type is unavailable.
        KeyError: no entry with the given ID (from store).
        CloudflareError: DNS upsert failed.
        CaddyError: Caddy rejected the config.
    """
    ip = _resolve_ip(entry.source_ip_type)

    logger.info(f"Updating proxy entry: {entry.domain} → {entry.target_value}")

    await upsert_a_record(entry.zone_id, entry.domain, ip)

    updated_entry = await store.update_entry(entry)
    await sync_caddy_config()

    logger.info(f"Proxy entry updated: {entry.domain} → {entry.target_value}")
    return updated_entry


async def delete_entry(entry_id: uuid.UUID) -> ProxyEntry:
    """Delete a proxy entry end-to-end.

    Steps:
    1. Remove from store.
    2. Apply the updated Caddy config (entry is no longer proxied).
    3. Delete the Cloudflare A record.

    DNS deletion failure is non-fatal — a warning is logged but the operation
    succeeds. The proxy config is the primary concern; a stale DNS record is
    a minor side-effect that the user can clean up manually.

    Raises:
        KeyError: no entry with the given ID (from store).
        CaddyError: Caddy rejected the updated config.
    """
    deleted = await store.delete_entry(entry_id)
    logger.info(f"Removed proxy entry from store: {deleted.domain}")

    await sync_caddy_config()

    # DNS cleanup — non-blocking on failure.
    # zone_id is stored on the entry, no derivation needed.
    try:
        record = await get_a_record(deleted.zone_id, deleted.domain)
        if record:
            record_id, _ = record
            await delete_a_record(deleted.zone_id, record_id)
            logger.info(f"Deleted Cloudflare A record for {deleted.domain}")
        else:
            logger.warning(f"No A record found for {deleted.domain} — nothing to delete")
    except CloudflareError as exc:
        logger.warning(
            f"Failed to delete Cloudflare A record for {deleted.domain}: {exc}. Record may need manual cleanup."
        )

    logger.info(f"Proxy entry deleted: {deleted.domain}")
    return deleted


async def list_entries() -> list[ProxyEntry]:
    """Return all proxy entries from the store, ordered by created_at."""
    return await store.list_entries()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


async def get_available_zones() -> list[CloudflareZone]:
    """Return all Cloudflare zones accessible with the configured token, sorted by name.

    The UI uses this to populate the zone selector. The first zone alphabetically
    is the default. The selected zone's id is stored on ProxyEntry as zone_id.

    Raises CloudflareError if the API call fails.
    """
    return await list_zones()


async def get_available_targets() -> list[ProxyTarget]:
    """Return all available proxy targets from Docker and Tailscale.

    Fetches Docker containers and Tailscale devices concurrently.
    If either source fails, the other's results are still returned —
    return_exceptions=True prevents one failure from masking the other.

    Note: Tailscale target values contain only the hostname. The form (Plan 10)
    must append ':port' before constructing a ProxyEntry, since ProxyEntry
    requires host:port format.
    """
    results = await asyncio.gather(
        list_running_containers(),
        list_devices(),
        return_exceptions=True,
    )
    docker_result, ts_result = results

    targets: list[ProxyTarget] = []

    if isinstance(docker_result, list):
        for container in docker_result:
            for port_spec in container.ports:
                port_num = port_spec.split("/")[0]  # "8080/tcp" → "8080"
                targets.append(
                    ProxyTarget(
                        label=f"{container.name}:{port_num} (Docker)",
                        value=f"{container.name}:{port_num}",
                        target_type=TargetType.DOCKER,
                    )
                )
    else:
        logger.warning(f"Docker target discovery failed: {docker_result}")

    if isinstance(ts_result, list):
        for device in ts_result:
            targets.append(
                ProxyTarget(
                    label=f"{device.hostname} [{device.ip}] (Tailscale)",
                    value=device.hostname,  # UI must append ':port' before use
                    target_type=TargetType.TAILSCALE,
                )
            )
    else:
        logger.warning(f"Tailscale target discovery failed: {ts_result}")

    return targets


def get_available_ssl_methods(source_ip_type: SourceIPType) -> list[SSLMethod]:
    """Return SSL methods valid for the given source IP type.

    Enforces the compatibility matrix from the spec:
      PUBLIC   → None, HTTP-01, DNS-01
      TAILSCALE → None, DNS-01 (HTTP-01 requires public port 80 reachability)

    Pure function — no I/O, intentionally synchronous.
    """
    if source_ip_type == SourceIPType.PUBLIC:
        return [SSLMethod.NONE, SSLMethod.HTTP01, SSLMethod.DNS01]
    elif source_ip_type == SourceIPType.TAILSCALE:
        return [SSLMethod.NONE, SSLMethod.DNS01]
    # Defensive: should never be reached with a valid SourceIPType.
    return [SSLMethod.NONE]


def get_public_ip() -> str | None:
    """Return the cached public IP, or None if not yet initialized."""
    return _public_ip


def get_tailscale_ip() -> str | None:
    """Return the cached Caddy host Tailscale IP, or None if unavailable."""
    return _tailscale_ip


async def get_entry_by_id(entry_id: uuid.UUID) -> ProxyEntry | None:
    """Return a single proxy entry by ID, or None if not found."""
    return await store.get_entry(entry_id)
