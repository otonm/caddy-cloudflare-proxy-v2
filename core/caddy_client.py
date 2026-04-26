"""Caddy Admin API client.

Manages the Caddy reverse proxy via its JSON Admin API. Responsibilities:
1. Health-check Caddy at startup.
2. Build a complete Caddy JSON config from the current list of ProxyEntry objects.
3. Apply that config atomically by POSTing to /load.

The "rebuild full config from scratch" approach guarantees Caddy is always
perfectly in sync with the store — no incremental patch logic needed.
"""

from __future__ import annotations

import asyncio
import logging
import ssl as _ssl

import httpx

from core.config import CADDY_ADMIN_URL, settings
from core.models import ProxyEntry, SSLMethod, UpstreamTLS

logger = logging.getLogger(__name__)


class CaddyError(Exception):
    """Raised when a Caddy Admin API call fails."""


async def health_check() -> bool:
    """Return True if Caddy Admin API is reachable, False otherwise.

    Uses GET /config/ as the probe — it returns 200 when Caddy is up and
    the admin API is accepting requests.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        try:
            response = await client.get(f"{CADDY_ADMIN_URL}/config/")
            return response.status_code == 200
        except httpx.HTTPError:
            return False


async def check_cert_ready(domain: str) -> bool:
    """Return True when Caddy has a valid TLS cert for the domain; False otherwise.

    Connects directly to caddy:443 on the internal Docker network with SNI set
    to the domain name. This bypasses external DNS routing and firewalls — the
    check is purely about whether Caddy has provisioned the cert yet.

    Returns True:  TLS handshake succeeded → Caddy is serving a trusted cert.
    Returns False: Any SSL error → cert not yet provisioned or still pending.
    Returns False: Connection refused / OS error → Caddy HTTPS server not yet
                   started for this domain (cert acquisition still in progress).
    Returns False: Timeout → Caddy unreachable on internal network (not started).
    """
    ssl_ctx = _ssl.create_default_context()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("caddy", 443, ssl=ssl_ctx, server_hostname=domain),
            timeout=10.0,
        )
        writer.close()
        await writer.wait_closed()
        logger.debug(f"check_cert_ready: cert valid for {domain}")
        return True
    except _ssl.SSLError:
        logger.debug(f"check_cert_ready: cert not yet valid for {domain}")
        return False
    except (TimeoutError, OSError) as exc:
        logger.debug(f"check_cert_ready: HTTPS not ready for {domain}: {exc}")
        return False
    except Exception as exc:
        logger.debug(f"check_cert_ready: unexpected error for {domain}: {exc}")
        return False


def _build_config(
    entries: list[ProxyEntry],
    cf_token: str,
    acme_email: str,
) -> dict:
    """Build the complete Caddy JSON configuration dict from proxy entries.

    CF token is injected here for DNS-01 TLS policies. It is never logged
    or written to disk — it exists only in this in-memory dict, which is
    sent over the internal Docker network to the Caddy Admin API.

    Two virtual servers are used:
    - http_server (:80): plain reverse proxy for NONE entries; HTTP→HTTPS
      redirects for HTTP01/DNS01 entries.
    - https_server (:443): reverse proxy for HTTP01 and DNS01 entries.

    TLS automation policies are added for DNS-01 entries. For HTTP-01 entries,
    a catch-all policy (no subjects) sets the ACME email so Caddy's default
    issuer uses the correct email address.
    """
    http_routes: list[dict] = []
    https_routes: list[dict] = []
    tls_policies: list[dict] = []
    has_http01 = False

    for entry in entries:
        # Build the reverse_proxy handler; add a transport block when the
        # upstream connection should use TLS (verified or skip-verify).
        reverse_proxy_handler: dict = {
            "handler": "reverse_proxy",
            "upstreams": [{"dial": entry.target_value}],
        }
        if entry.upstream_tls == UpstreamTLS.TLS:
            reverse_proxy_handler["transport"] = {"protocol": "http", "tls": {}}
        elif entry.upstream_tls == UpstreamTLS.TLS_SKIP_VERIFY:
            reverse_proxy_handler["transport"] = {"protocol": "http", "tls": {"insecure_skip_verify": True}}

        proxy_route: dict = {
            "@id": f"entry-{entry.id}",
            "match": [{"host": [entry.domain]}],
            "handle": [reverse_proxy_handler],
        }
        redirect_route: dict = {
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
            has_http01 = True
            http_routes.append(redirect_route)
            https_routes.append(proxy_route)

        elif entry.ssl_method == SSLMethod.DNS01:
            http_routes.append(redirect_route)
            https_routes.append(proxy_route)
            tls_policies.append(
                {
                    "subjects": [entry.domain],
                    "issuers": [
                        {
                            "module": "acme",
                            "email": acme_email,
                            "challenges": {
                                "dns": {
                                    "provider": {
                                        "name": "cloudflare",
                                        "api_token": cf_token,
                                    }
                                }
                            },
                        }
                    ],
                }
            )

    # Caddy's default ACME issuer needs the email injected via a catch-all
    # policy (no subjects field) when any HTTP-01 entries are present.
    # Appended last so domain-specific DNS-01 policies (which come first in
    # the list) are matched before the catch-all — Caddy uses first-match-wins.
    if has_http01:
        catchall_policy: dict = {"issuers": [{"module": "acme", "email": acme_email}]}
        tls_policies.append(catchall_policy)

    servers: dict = {}
    if http_routes:
        servers["http_server"] = {"listen": [":80"], "routes": http_routes}
    if https_routes:
        servers["https_server"] = {"listen": [":443"], "routes": https_routes}

    # The admin block must be included on every /load so Caddy keeps its
    # admin endpoint on 0.0.0.0:2019.  Without it, Caddy reverts to the
    # default localhost:2019 on every config reload, making the endpoint
    # unreachable from the app container on subsequent calls.
    config: dict = {"admin": {"listen": "0.0.0.0:2019"}, "apps": {}}
    if servers:
        config["apps"]["http"] = {"servers": servers}
    if tls_policies:
        config["apps"]["tls"] = {"automation": {"policies": tls_policies}}

    return config


async def apply_config(
    entries: list[ProxyEntry],
    tailscale_ip: str | None,
    public_ip: str,
) -> None:
    """Build and apply the full Caddy JSON config from the current proxy entries.

    tailscale_ip and public_ip are accepted but not used directly in config
    building — they are used upstream by proxy_service to set Cloudflare DNS.
    Included in signature for future use (e.g. bind address hints).

    The CF_API_TOKEN is retrieved here with get_secret_value() and injected
    into the JSON payload. It is never written to disk or logged.

    Raises CaddyError if Caddy rejects the config or is unreachable.
    """
    cf_token = settings.cf_api_token.get_secret_value()
    config = _build_config(entries, cf_token, settings.acme_email)
    logger.debug(f"Built Caddy config: {config}")

    logger.info(f"Applying Caddy config with {len(entries)} entries")
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
