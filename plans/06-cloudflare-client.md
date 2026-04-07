# Plan 06 — Cloudflare Client

## Goal

Implement `core/cloudflare_client.py`: an async httpx client that manages Cloudflare DNS
A records. Responsibilities:
1. Look up the zone ID for a given domain.
2. Check whether an A record already exists for a hostname.
3. Create or update (upsert) an A record.
4. Delete an A record (always called on entry deletion).
5. Detect the host's public IP (used when `source_ip_type = PUBLIC`).

---

## Dependencies on Previous Plans

- Plan 02: uses `settings.cf_api_token`.
- Plan 01: `httpx>=0.27` in `pyproject.toml`.

---

## API Reference

**Always verify against https://developers.cloudflare.com/api before implementing.**

Base URL: `https://api.cloudflare.com/client/v4`

Auth header: `Authorization: Bearer {CF_API_TOKEN}`

Key endpoints:
- `GET /zones?name={zone_name}` — find zone ID by name
- `GET /zones/{zone_id}/dns_records?type=A&name={record_name}` — check existing record
- `POST /zones/{zone_id}/dns_records` — create new A record
- `PATCH /zones/{zone_id}/dns_records/{record_id}` — update existing A record
- `DELETE /zones/{zone_id}/dns_records/{record_id}` — delete a record

All responses have shape `{"success": bool, "result": ..., "errors": [...]}`.

---

## File: `core/cloudflare_client.py`

### Public API

```python
async def get_zone_id(domain: str) -> str:
    """Return the Cloudflare zone ID for the zone containing `domain`.

    Derives the zone name from the domain's last two labels (e.g., "app.example.com"
    → zone "example.com"). Raises CloudflareError if the zone is not found.
    """

async def upsert_a_record(zone_id: str, name: str, ip: str) -> str:
    """Create or update an A record. Returns the DNS record ID.

    Always sets proxied=False (direct DNS, required for Tailscale IPs and
    for Caddy's own TLS certificate issuance).
    Always sets TTL=1 (automatic).
    If a record already exists for this name, update its IP.
    If no record exists, create one.
    """

async def delete_a_record(zone_id: str, record_id: str) -> None:
    """Delete a DNS A record by record ID.

    Raises CloudflareError if the record does not exist or deletion fails.
    """

async def get_a_record(zone_id: str, name: str) -> tuple[str, str] | None:
    """Look up an existing A record. Returns (record_id, ip) or None if not found."""

async def detect_public_ip() -> str:
    """Return the public IPv4 address of this host.

    Uses settings.public_ip if set (override). Otherwise calls api4.ipify.org.
    Raises RuntimeError if detection fails.
    """
```

### Custom Exception

```python
class CloudflareError(Exception):
    """Raised when a Cloudflare API call fails in an unrecoverable way."""
```

### Zone Derivation

Given a domain like `app.staging.example.com`, the zone is `example.com` (last two labels).
This covers most use cases. For second-level TLDs (e.g., `app.example.co.uk`), this
would return `co.uk` which is wrong — but supporting SLDs is out of scope. Document this
limitation clearly.

```python
def _derive_zone_name(domain: str) -> str:
    """Extract the registrable zone from a domain name.

    Warning: does not handle second-level TLDs (e.g., .co.uk).
    """
    parts = domain.rstrip(".").split(".")
    if len(parts) < 2:
        raise ValueError(f"Cannot derive zone from domain: {domain!r}")
    return ".".join(parts[-2:])
```

### Client Helper

Use a shared `httpx.AsyncClient` with the auth header pre-configured.
Use a context manager at the call site OR a module-level client. Given that Cloudflare
calls happen only during entry CRUD (not continuously), a fresh client per public function
call is acceptable.

```python
import contextlib
import httpx

_CF_BASE = "https://api.cloudflare.com/client/v4"

@contextlib.asynccontextmanager
async def _cf_client() -> httpx.AsyncClient:
    token = settings.cf_api_token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        base_url=_CF_BASE,
        headers=headers,
        timeout=httpx.Timeout(15.0),
    ) as client:
        yield client
```

### `get_zone_id` Implementation

```python
async def get_zone_id(domain: str) -> str:
    zone_name = _derive_zone_name(domain)
    logger.info("Looking up Cloudflare zone for %s", zone_name)
    async with _cf_client() as client:
        response = await client.get("/zones", params={"name": zone_name})
        response.raise_for_status()
    data = response.json()
    if not data.get("success") or not data.get("result"):
        raise CloudflareError(f"Zone not found for domain {domain!r} (zone={zone_name!r})")
    return data["result"][0]["id"]
```

### `upsert_a_record` Implementation

```python
async def upsert_a_record(zone_id: str, name: str, ip: str) -> str:
    logger.info("Upserting Cloudflare A record: %s → %s", name, ip)
    existing = await get_a_record(zone_id, name)
    if existing:
        record_id, current_ip = existing
        if current_ip == ip:
            logger.info("A record for %s already correct (%s), no update needed", name, ip)
            return record_id
        logger.info("Updating A record for %s: %s → %s", name, current_ip, ip)
        async with _cf_client() as client:
            response = await client.patch(
                f"/zones/{zone_id}/dns_records/{record_id}",
                json={"content": ip},
            )
            _check_response(response, f"update A record for {name}")
        return record_id
    else:
        logger.info("Creating A record for %s → %s", name, ip)
        async with _cf_client() as client:
            response = await client.post(
                f"/zones/{zone_id}/dns_records",
                json={"type": "A", "name": name, "content": ip, "ttl": 1, "proxied": False},
            )
            _check_response(response, f"create A record for {name}")
        return response.json()["result"]["id"]
```

### Response Validation Helper

```python
def _check_response(response: httpx.Response, operation: str) -> None:
    """Raise CloudflareError if the API response indicates failure."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CloudflareError(f"Cloudflare API error during {operation}: HTTP {response.status_code}") from exc
    data = response.json()
    if not data.get("success"):
        errors = data.get("errors", [])
        raise CloudflareError(f"Cloudflare API {operation} failed: {errors}")
```

### `detect_public_ip` Implementation

```python
async def detect_public_ip() -> str:
    if settings.public_ip:
        logger.info("Using configured PUBLIC_IP: %s", settings.public_ip)
        return settings.public_ip
    logger.info("Detecting public IP via api4.ipify.org")
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        try:
            response = await client.get("https://api4.ipify.org?format=json")
            response.raise_for_status()
            ip = response.json()["ip"]
            logger.info("Detected public IP: %s", ip)
            return ip
        except Exception as exc:
            raise RuntimeError(f"Failed to detect public IP: {exc}") from exc
```

> **Why `api4.ipify.org`**: The `4` subdomain forces IPv4 response, avoiding issues on
> dual-stack hosts where we specifically need the public IPv4 for A records.

---

## Caching Strategy

- `detect_public_ip()`: cache the result at app startup in `proxy_service.py` and reuse.
  Don't call ipify on every entry creation.
- `get_zone_id()`: zone IDs don't change — cache in a module-level dict
  `_zone_cache: dict[str, str] = {}` keyed by zone name.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Zone not found | Raise `CloudflareError` with clear message |
| HTTP 401 | Raise `CloudflareError` "check CF_API_TOKEN" |
| HTTP 403 | Raise `CloudflareError` "insufficient permissions on token" |
| Record already up to date | Log INFO, return existing record_id (no-op) |
| Delete non-existent record | Raise `CloudflareError` |

---

## Verification Steps

1. With valid credentials, run:
   ```bash
   uv run python -c "
   import asyncio
   from core.cloudflare_client import get_zone_id, detect_public_ip
   async def test():
       ip = await detect_public_ip()
       print('Public IP:', ip)
   asyncio.run(test())
   "
   ```
2. Test `_derive_zone_name` with various inputs: `app.example.com` → `example.com`,
   `sub.app.example.com` → `example.com`, `example.com` → `example.com`.
3. `uv run ruff check core/cloudflare_client.py --fix` — must pass clean.

---

## Open Questions

- **Delete on entry removal**: When a proxy entry is deleted, should the Cloudflare A
  record also be deleted? Or just removed from Caddy? Safer default: **do not delete**
  the DNS record automatically (user may want it for other purposes). Expose a `delete_a_record`
  function and let the proxy service decide based on a future config option.
- **`proxied: false` assumption**: Cloudflare proxy (orange cloud) is always disabled here
  because Caddy needs the real IP for certificate issuance and reverse proxying. This is
  intentional and must never be changed.
- **SLD domains**: The zone derivation limitation (no `.co.uk` support). Accept and document.
