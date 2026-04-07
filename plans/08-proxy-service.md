# Plan 08 — Proxy Service (Orchestrator)

## Goal

Implement `core/proxy_service.py`: the orchestration layer that coordinates the store,
Cloudflare client, Caddy client, Docker client, and Tailscale client into complete
business operations. The UI calls only this module — it never calls lower-level clients
directly.

After this plan, all backend logic is complete. Plans 09 and 10 wire up the UI.

---

## Dependencies on Previous Plans

- Plan 02: `settings`
- Plan 03: `ProxyEntry`, `ProxyConfig`, `ProxyTarget`, `ContainerInfo`, `TailscaleDevice`, `TargetType`, store functions
- Plan 04: `docker_client.list_running_containers`
- Plan 05: `tailscale_client.list_devices`, `tailscale_client.get_caddy_host_ip`
- Plan 06: `cloudflare_client.get_zone_id`, `cloudflare_client.upsert_a_record`, `cloudflare_client.delete_a_record`, `cloudflare_client.detect_public_ip`
- Plan 07: `caddy_client.apply_config`, `caddy_client.health_check`, `caddy_client.CaddyError`

---

## Runtime State

The proxy service holds two pieces of state that are resolved at startup and then cached:

```python
_public_ip: str | None = None
_tailscale_ip: str | None = None  # Caddy host's Tailscale IP
```

These are set by `initialize()` at startup and must be set before any `create_entry()`
call. Using module-level state is appropriate here since the app is single-process.

---

## File: `core/proxy_service.py`

### Public API

```python
async def initialize() -> None:
    """Resolve and cache the public IP and Tailscale IP.

    Must be called once at startup, before any proxy entry operations.
    Logs warnings for unavailable optional features (e.g., Tailscale IP not found).
    Raises RuntimeError if the public IP cannot be determined (hard requirement).
    """

async def sync_caddy_config() -> None:
    """Reload all entries from store and apply to Caddy.

    Called at startup after initialize() and after any entry CRUD.
    Raises CaddyError if Caddy rejects the config.
    """

async def create_entry(entry: ProxyEntry) -> ProxyEntry:
    """Create a new proxy entry end-to-end:
    1. Resolve the source IP (public or Tailscale).
    2. Upsert the Cloudflare A record.
    3. Persist the entry to the store.
    4. Apply the updated Caddy config.
    Returns the saved entry.
    Raises ValueError if source IP type is unavailable.
    Raises CloudflareError on DNS failure.
    Raises CaddyError on Caddy failure.
    """

async def delete_entry(entry_id: uuid.UUID) -> ProxyEntry:
    """Delete a proxy entry:
    1. Remove from store.
    2. Apply the updated Caddy config (entry is no longer proxied).
    3. Delete the Cloudflare A record.
    Returns the deleted entry.
    """

async def update_entry(entry: ProxyEntry) -> ProxyEntry:
    """Update an existing proxy entry:
    1. Resolve and upsert the Cloudflare A record (IP may have changed if source type changed).
    2. Update in store.
    3. Apply the updated Caddy config.
    Returns the updated entry.
    """

async def list_entries() -> list[ProxyEntry]:
    """Return all proxy entries from the store, ordered by created_at."""

async def get_available_targets() -> list[ProxyTarget]:
    """Return all available proxy targets from Docker and Tailscale.

    Combines:
    - Running Docker containers (with all exposed ports)
    - Tailscale devices (each as a single target requiring user-specified port)
    Returns empty-ish list if services are unavailable — never raises.
    """

async def get_available_ssl_methods(source_ip_type: SourceIPType) -> list[SSLMethod]:
    """Return the SSL methods available for a given source IP type.

    Enforces the compatibility matrix from the spec.
    This is used by the UI to dynamically enable/disable SSL options.
    """
```

---

## `initialize()` Implementation

```python
async def initialize() -> None:
    global _public_ip, _tailscale_ip

    logger.info("Initializing proxy service")

    # Detect public IP — required
    try:
        _public_ip = await detect_public_ip()
        logger.info("Public IP resolved: %s", _public_ip)
    except RuntimeError as exc:
        logger.error("Cannot determine public IP: %s", exc)
        raise RuntimeError("Public IP is required but could not be determined") from exc

    # Resolve Tailscale IP — optional
    _tailscale_ip = await get_caddy_host_ip()
    if _tailscale_ip:
        logger.info("Caddy Tailscale IP resolved: %s", _tailscale_ip)
    else:
        logger.warning(
            "Caddy Tailscale IP not available — "
            "proxy entries with source_ip_type=TAILSCALE will be rejected"
        )
```

---

## `create_entry()` — Source IP Resolution

```python
async def create_entry(entry: ProxyEntry) -> ProxyEntry:
    # Resolve the IP for the A record
    if entry.source_ip_type == SourceIPType.PUBLIC:
        ip = _public_ip
        if ip is None:
            raise ValueError("Public IP not available")
    elif entry.source_ip_type == SourceIPType.TAILSCALE:
        ip = _tailscale_ip
        if ip is None:
            raise ValueError(
                "Tailscale source IP not available. "
                "Set TS_HOST_NAME to enable Tailscale source IP."
            )
    else:
        # Should never happen given the enum, but be defensive
        raise ValueError(f"Unknown source IP type: {entry.source_ip_type}")

    # Update Cloudflare DNS
    zone_id = await get_zone_id(entry.domain)
    await upsert_a_record(zone_id, entry.domain, ip)

    # Persist
    saved_entry = await store.add_entry(entry)

    # Sync Caddy
    await sync_caddy_config()

    logger.info("Created proxy entry: %s → %s", entry.domain, entry.target_value)
    return saved_entry
```

---

## Rollback Consideration

`create_entry` has three steps: DNS → store → Caddy. If Caddy fails after DNS is updated
and the entry is stored:
- The entry is persisted (good — we don't lose it)
- Caddy is out of sync (bad — the proxy doesn't work)

A simple mitigation: if `sync_caddy_config` fails, remove the entry from the store and
re-sync (which will not include the new entry). This gives partial rollback.

```python
try:
    await sync_caddy_config()
except CaddyError:
    # Rollback: remove from store
    await store.delete_entry(saved_entry.id)
    # DNS record is left in place — removing DNS is too risky
    raise
```

DNS rollback is explicitly NOT done — DNS changes are external and may have propagated.
Removing the DNS record could break other things. Document this clearly.

---

## `get_available_ssl_methods()`

```python
def get_available_ssl_methods(source_ip_type: SourceIPType) -> list[SSLMethod]:
    if source_ip_type == SourceIPType.PUBLIC:
        return [SSLMethod.NONE, SSLMethod.HTTP01, SSLMethod.DNS01]
    elif source_ip_type == SourceIPType.TAILSCALE:
        return [SSLMethod.NONE, SSLMethod.DNS01]
    return [SSLMethod.NONE]
```

This is a pure function (no I/O) so it does not need to be `async`. Note: the function
is `def`, not `async def`, which is correct per CLAUDE.md (async only for I/O-bound).

---

## `get_available_targets()`

```python
async def get_available_targets() -> list[ProxyTarget]:
    # Fetch Docker and Tailscale concurrently
    docker_containers, ts_devices = await asyncio.gather(
        list_running_containers(),
        list_tailscale_devices(),
        return_exceptions=True,
    )

    targets: list[ProxyTarget] = []

    if isinstance(docker_containers, list):
        for container in docker_containers:
            for port in container.ports:
                port_num = port.split("/")[0]  # "8080/tcp" → "8080"
                targets.append(ProxyTarget(
                    label=f"{container.name}:{port_num} (Docker)",
                    value=f"{container.name}:{port_num}",
                    target_type=TargetType.DOCKER,
                ))

    if isinstance(ts_devices, list):
        for device in ts_devices:
            # Tailscale targets require user to specify port — we use device hostname
            # and the form will ask for the port separately
            targets.append(ProxyTarget(
                label=f"{device.hostname} [{device.ip}] (Tailscale)",
                value=device.hostname,  # Port added by form
                target_type=TargetType.TAILSCALE,
            ))

    return targets
```

> **Note on Tailscale target value**: For Tailscale targets, the `value` here is just
> the hostname. The form (Plan 10) must append `:port` before constructing the `ProxyEntry`.
> This is handled in the UI layer.

---

## Startup Sequence (used by main.py)

```python
async def startup() -> None:
    """Full startup sequence called from main.py via app.on_startup."""
    await initialize()
    healthy = await health_check()
    if not healthy:
        logger.error("Caddy Admin API is not reachable at startup — check Caddy container")
        # Don't raise — let the app start anyway; Caddy may recover
    else:
        await sync_caddy_config()
        logger.info("Caddy config synchronized on startup")
```

---

## Verification Steps

1. Run `startup()` with Caddy running — should log success and show no errors.
2. Create an entry, verify it appears in store and Caddy config.
3. Delete an entry, verify it's removed from store and Caddy config.
4. Test the Caddy rollback: mock `apply_config` to raise, verify the entry is not
   persisted.
5. `uv run ruff check core/proxy_service.py --fix` — must pass clean.

---

## Open Questions

- **Concurrency**: If two users hit "create" simultaneously (unlikely with a single-user
  web UI), the store write is not locked. For v1, accept this limitation and document it.
- **`update_entry` and domain change**: Domain is read-only in the edit form (v1 decision).
  If domain change support is added later, we'd need to upsert the new DNS record, delete
  the old one, and rebuild the Caddy config. Deferred to v2.
- **`delete_entry` DNS failure**: If `delete_a_record` fails (e.g. record already gone),
  should we still proceed with removing from store and Caddy? Recommendation: yes —
  log a warning but don't block the deletion. The proxy is more important than DNS cleanup.
