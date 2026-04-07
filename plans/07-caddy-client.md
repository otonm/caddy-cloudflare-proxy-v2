# Plan 07 — Caddy Client

## Goal

Implement `core/caddy_client.py`: an async httpx client that manages the Caddy reverse
proxy via its Admin API. Responsibilities:
1. Health-check Caddy at startup.
2. Build a complete Caddy JSON config object from the list of `ProxyEntry` objects.
3. Apply that config by POSTing to `/load`.

The "rebuild full config from scratch" approach is chosen over incremental route updates:
simpler, less error-prone, and guarantees Caddy is always perfectly in sync with the store.

---

## Dependencies on Previous Plans

- Plan 02: uses `CADDY_ADMIN_URL`, `settings.cf_api_token`, `settings.acme_email`.
- Plan 03: uses `ProxyEntry`, `SourceIPType`, `SSLMethod` from `core/models.py`.
- Plan 01: `httpx>=0.28` in `pyproject.toml`.

---

## API Reference

**Always verify against https://caddyserver.com/docs/api before implementing.**

Key endpoints:
- `GET /config/` — read current config (also used as health check)
- `POST /load` — replace full config atomically; Content-Type: `application/json`

Admin API base: `http://caddy:2019` (the `CADDY_ADMIN_URL` constant)

---

## File: `core/caddy_client.py`

### Public API

```python
async def health_check() -> bool:
    """Return True if Caddy Admin API is reachable, False otherwise."""

async def apply_config(
    entries: list[ProxyEntry],
    tailscale_ip: str | None,
    public_ip: str,
) -> None:
    """Build and apply the full Caddy JSON config from the current proxy entries.

    The CF_API_TOKEN is injected into the JSON payload at call time — it is never
    written to disk.

    Raises CaddyError if Caddy rejects the config or is unreachable.
    """
```

### Custom Exception

```python
class CaddyError(Exception):
    """Raised when a Caddy Admin API call fails."""
```

---

## Caddy JSON Config Structure

**Always verify the current schema at https://caddyserver.com/docs/json/ before
implementing — endpoint paths and payload shapes may have changed.**

### Two-server design

We use two separate virtual servers:

- **`http_server`** (`:80`): handles all entries — SSL entries get a redirect to HTTPS,
  non-SSL entries get the actual reverse proxy.
- **`https_server`** (`:443`): handles only SSL entries (HTTP-01 and DNS-01).

This way, HTTP→HTTPS redirects work automatically for SSL entries while non-SSL entries
are served correctly over HTTP.

### Config skeleton

```json
{
  "apps": {
    "http": {
      "servers": {
        "http_server": {
          "listen": [":80"],
          "routes": [
            "... reverse_proxy routes for ssl_method=NONE entries ...",
            "... redirect-to-HTTPS routes for ssl_method=HTTP01 or DNS01 entries ..."
          ]
        },
        "https_server": {
          "listen": [":443"],
          "routes": [
            "... reverse_proxy routes for ssl_method=HTTP01 and DNS01 entries ..."
          ]
        }
      }
    },
    "tls": {
      "automation": {
        "policies": [
          "... one policy per DNS-01 entry ..."
        ]
      }
    }
  }
}
```

### Route types

**Reverse proxy route (used in both servers)**:
```json
{
  "@id": "entry-{uuid}",
  "match": [{"host": ["app.example.com"]}],
  "handle": [
    {
      "handler": "reverse_proxy",
      "upstreams": [{"dial": "container-name:8080"}]
    }
  ]
}
```

**HTTP→HTTPS redirect route (HTTP server, SSL entries only)**:

> **IMPORTANT**: Verify the correct `static_response` handler shape against current
> Caddy docs before implementing. The Location header uses Caddy's placeholder syntax.

```json
{
  "match": [{"host": ["app.example.com"]}],
  "handle": [
    {
      "handler": "static_response",
      "status_code": 301,
      "headers": {
        "Location": ["https://{http.request.host}{http.request.uri}"]
      }
    }
  ]
}
```

### TLS automation policy (DNS-01 entries only)

```json
{
  "subjects": ["app.example.com"],
  "issuers": [
    {
      "module": "acme",
      "email": "admin@example.com",
      "challenges": {
        "dns": {
          "provider": {
            "name": "cloudflare",
            "api_token": "<CF_API_TOKEN value injected at runtime>"
          }
        }
      }
    }
  ]
}
```

For **HTTP-01** entries: no explicit TLS policy is needed. Caddy's default ACME issuer
handles certificate issuance automatically when a route is on the HTTPS server.

> **On CF_API_TOKEN injection**: The raw token value is placed directly inside the JSON
> payload sent over the internal Docker network to `http://caddy:2019`. It is retrieved
> with `.get_secret_value()` only at this call site. It never touches the filesystem or
> any log output.

---

## Config Builder Implementation

```python
def _build_config(
    entries: list[ProxyEntry],
    cf_token: str,
    acme_email: str,
) -> dict:
    """Build the complete Caddy JSON configuration dict.

    CF token is injected here for DNS-01 policies. Never logged.
    """
    http_routes: list[dict] = []
    https_routes: list[dict] = []
    tls_policies: list[dict] = []

    for entry in entries:
        proxy_route = {
            "@id": f"entry-{entry.id}",
            "match": [{"host": [entry.domain]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": entry.target_value}]}],
        }
        redirect_route = {
            "match": [{"host": [entry.domain]}],
            "handle": [
                {
                    "handler": "static_response",
                    "status_code": 301,
                    "headers": {"Location": ["https://{http.request.host}{http.request.uri}"]},
                }
            ],
        }

        if entry.ssl_method == SSLMethod.NONE:
            http_routes.append(proxy_route)

        elif entry.ssl_method == SSLMethod.HTTP01:
            http_routes.append(redirect_route)
            https_routes.append(proxy_route)

        elif entry.ssl_method == SSLMethod.DNS01:
            http_routes.append(redirect_route)
            https_routes.append(proxy_route)
            tls_policies.append({
                "subjects": [entry.domain],
                "issuers": [{
                    "module": "acme",
                    "email": acme_email,
                    "challenges": {
                        "dns": {"provider": {"name": "cloudflare", "api_token": cf_token}}
                    },
                }],
            })

    servers: dict = {}
    if http_routes:
        servers["http_server"] = {"listen": [":80"], "routes": http_routes}
    if https_routes:
        servers["https_server"] = {"listen": [":443"], "routes": https_routes}

    config: dict = {"apps": {}}
    if servers:
        config["apps"]["http"] = {"servers": servers}
    if tls_policies:
        config["apps"]["tls"] = {"automation": {"policies": tls_policies}}

    return config
```

### `apply_config` Implementation

```python
async def apply_config(
    entries: list[ProxyEntry],
    tailscale_ip: str | None,
    public_ip: str,
) -> None:
    """Apply the full Caddy config derived from the current entries.

    tailscale_ip and public_ip are accepted but not used directly in config
    building — they are used upstream by proxy_service to set Cloudflare DNS.
    Included in signature for future use (e.g. bind address hints).
    """
    cf_token = settings.cf_api_token.get_secret_value()
    config = _build_config(entries, cf_token, settings.acme_email)

    logger.info("Applying Caddy config with %d entries", len(entries))
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        try:
            response = await client.post(
                f"{CADDY_ADMIN_URL}/load",
                json=config,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CaddyError(f"Failed to apply Caddy config: {exc}") from exc

    logger.info("Caddy config applied successfully")
```

### Empty Config Behaviour

If `entries` is empty, post `{"apps": {}}`. This clears all routes without breaking Caddy.

---

## Health Check

```python
async def health_check() -> bool:
    """Return True if Caddy Admin API responds on GET /config/."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        try:
            response = await client.get(f"{CADDY_ADMIN_URL}/config/")
            return response.status_code == 200
        except httpx.HTTPError:
            return False
```

---

## Verification Steps

1. Start Caddy (`docker compose up caddy` on a machine with Docker), then:
   ```bash
   uv run python -c "import asyncio; from core.caddy_client import health_check; print(asyncio.run(health_check()))"
   ```
   Must print `True`.
2. Apply a config with one HTTP-only (`ssl_method=NONE`) entry. Query `GET /config/` and
   verify the route appears under `apps.http.servers.http_server.routes`.
3. Apply a config with a DNS-01 entry. Verify:
   - The redirect route appears in `http_server`
   - The proxy route appears in `https_server`
   - The TLS policy with Cloudflare provider appears in `apps.tls`
4. Verify the CF_API_TOKEN value does NOT appear in any log output.
5. `uv run ruff check core/caddy_client.py --fix` — must pass clean.

---

## Open Questions

- **`static_response` for redirects**: The redirect handler shape shown above must be
  verified against the current Caddy JSON docs before implementation. The placeholder
  `{http.request.host}` and `{http.request.uri}` are standard Caddy variables but
  confirm they are still the correct syntax in the current version.
- **HTTP-01 and ACME email in JSON config**: With no explicit TLS policy for HTTP-01
  entries, Caddy uses its built-in default issuer. Confirm whether a global
  `apps.tls.automation.on_demand_tls` or default policy needs to be set to inject the
  ACME email for HTTP-01. If yes, add a "catch-all" policy with the email but no
  specific subjects.
