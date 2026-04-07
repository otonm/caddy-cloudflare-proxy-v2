# Plan 05 — Tailscale Client

## Goal

Implement `core/tailscale_client.py`: an async httpx client that fetches the list of
devices from the Tailscale API. This serves two purposes:
1. Populating the "target" dropdown when `target_type = TAILSCALE`.
2. Resolving the Caddy host's Tailscale IP (for `source_ip_type = TAILSCALE` A records).

---

## Dependencies on Previous Plans

- Plan 02: uses `settings.ts_api_key`, `settings.ts_tailnet`, `settings.ts_host_name`.
- Plan 03: uses `TailscaleDevice` from `core/models.py`.
- Plan 01: `httpx>=0.27` in `pyproject.toml`.

---

## API Reference

**Always verify against https://tailscale.com/api before implementing.**

Endpoint: `GET https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices`

Authentication: `Authorization: Bearer {TS_API_KEY}`

Expected response shape (from API docs):
```json
{
  "devices": [
    {
      "id": "...",
      "name": "my-server.example.com",
      "hostname": "my-server",
      "addresses": ["100.64.0.1", "fd7a::1"],
      "os": "linux",
      "lastSeen": "2024-01-01T00:00:00Z",
      "online": true
    }
  ]
}
```

Use only: `name`, `hostname`, `addresses`.
The first entry in `addresses` is the IPv4 Tailscale address (100.x.x.x range).

---

## File: `core/tailscale_client.py`

### Public API

```python
async def list_devices() -> list[TailscaleDevice]:
    """Fetch all devices in the configured tailnet.

    Returns an empty list if the API call fails — callers must handle this gracefully.
    """

async def get_caddy_host_ip() -> str | None:
    """Return the Tailscale IPv4 address of the machine running Caddy.

    Looks up TS_HOST_NAME in the device list. Returns None if:
    - TS_HOST_NAME is not configured
    - No device matches TS_HOST_NAME
    - The Tailscale API is unreachable
    """
```

### Implementation Notes

**Client lifecycle**: Use `httpx.AsyncClient` with a context manager per-request OR
create a module-level client with `base_url` set. Given these calls happen infrequently
(on page load, not in a hot path), using a fresh client per call is acceptable and
simpler. Use explicit timeout: `httpx.Timeout(10.0)`.

```python
_BASE_URL = "https://api.tailscale.com/api/v2"

async def list_devices() -> list[TailscaleDevice]:
    token = settings.ts_api_key.get_secret_value()
    url = f"{_BASE_URL}/tailnet/{settings.ts_tailnet}/devices"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Tailscale API request failed: %s", exc)
        return []

    data = response.json()
    devices = []
    for raw in data.get("devices", []):
        ip = _first_ipv4(raw.get("addresses", []))
        if ip is None:
            logger.warning("Tailscale device %r has no IPv4 address", raw.get("hostname"))
            continue
        devices.append(TailscaleDevice(
            name=raw["name"],
            hostname=raw["hostname"],
            ip=ip,
        ))
    logger.debug("Found %d Tailscale devices", len(devices))
    return devices
```

**IPv4 extraction**:
```python
import ipaddress

def _first_ipv4(addresses: list[str]) -> str | None:
    """Return the first IPv4 address from a list of IP strings."""
    for addr in addresses:
        try:
            if isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address):
                return addr
        except ValueError:
            continue
    return None
```

> **Why use `ipaddress` module**: Tailscale returns both IPv4 (100.x.x.x) and IPv6
> (fd7a::...) addresses. The spec says "use first IPv4 in addresses" — the `ipaddress`
> module is the correct tool to distinguish them rather than string matching.

**`get_caddy_host_ip`**:
```python
async def get_caddy_host_ip() -> str | None:
    if settings.ts_host_name is None:
        logger.warning("TS_HOST_NAME not configured; cannot determine Caddy Tailscale IP")
        return None
    devices = await list_devices()
    target_name = settings.ts_host_name.lower()
    for device in devices:
        if device.hostname.lower() == target_name or device.name.lower().startswith(target_name + "."):
            logger.debug("Found Caddy host in tailnet: %s → %s", device.hostname, device.ip)
            return device.ip
    logger.warning("TS_HOST_NAME=%r not found in tailnet devices", settings.ts_host_name)
    return None
```

> **Matching logic**: Compare against both `hostname` (short name like `my-server`) and
> the beginning of `name` (FQDN like `my-server.example.com`). This handles both the
> case where the user sets `TS_HOST_NAME=my-server` and where the device's FQDN differs.

### Secret Handling

The `TS_API_KEY` token must never appear in logs. Correct:
```python
logger.info("Fetching Tailscale devices for tailnet %s", settings.ts_tailnet)
```
Wrong:
```python
logger.debug("Using token %s", token)  # NEVER
```

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| HTTP 401 Unauthorized | Log ERROR "Tailscale API auth failed — check TS_API_KEY", return `[]` |
| HTTP 403 Forbidden | Log ERROR "Tailscale API insufficient permissions", return `[]` |
| Network timeout | Log ERROR with timeout details, return `[]` |
| Invalid JSON response | Log ERROR, return `[]` |
| Device has no IPv4 | Log WARNING, skip device |

---

## Verification Steps

1. With valid credentials in `.env`, run:
   ```bash
   uv run python -c "import asyncio; from core.tailscale_client import list_devices; print(asyncio.run(list_devices()))"
   ```
   Must return a list of `TailscaleDevice` objects.
2. With `TS_HOST_NAME` set to a known device hostname, verify `get_caddy_host_ip()` returns the correct IP.
3. `uv run ruff check core/tailscale_client.py --fix` — must pass clean.

---

## Open Questions

- **`TS_HOST_NAME` matching**: Should we match on `hostname` only, `name` (FQDN) only,
  or both? Proposed: both (try hostname first, fall back to FQDN prefix). Confirm.
- **Caching**: Should `list_devices()` cache results for a short period (e.g., 60s)?
  Tailscale device lists don't change frequently. For the initial implementation, no
  caching — call on demand. Can add later if perf is a concern.
