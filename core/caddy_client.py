"""Caddy Admin API client.

Manages the Caddy reverse proxy via its JSON Admin API. Responsibilities:
1. Health-check Caddy at startup.
2. Build a complete Caddy JSON config from the current list of ProxyEntry objects.
3. Apply that config atomically by POSTing to /load.

The "rebuild full config from scratch" approach guarantees Caddy is always
perfectly in sync with the store — no incremental patch logic needed.
"""

from __future__ import annotations

import logging

import httpx

from core.config import CADDY_ADMIN_URL, settings
from core.models import ProxyEntry, SSLMethod

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
    """Return True when the domain's TLS cert is valid; False when it is pending/invalid.

    Makes a HEAD request over HTTPS with certificate verification enabled.
    Returns True on any HTTP response (cert is valid and trusted).
    Returns False only on SSL certificate verification errors (cert pending or invalid).
    Fail-safe: returns True for all other errors (connectivity failures, DNS errors,
    timeouts) to avoid perpetual spinners for unreachable or firewalled domains.
    """
    import ssl as _ssl  # stdlib — imported locally to keep module-level imports minimal

    url = f"https://{domain}"
    timeout = httpx.Timeout(connect=5.0, read=3.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=True) as client:
        try:
            await client.head(url)
            return True
        except httpx.ConnectError as exc:
            # httpx wraps SSL handshake failures as ConnectError; inspect the cause.
            cause = exc.__context__ or exc.__cause__
            if isinstance(cause, _ssl.SSLCertVerificationError):
                logger.debug(f"check_cert_ready: cert not yet valid for {domain}")
                return False
            # Network-level error (firewall, DNS, connection refused) — fail-safe.
            logger.debug(f"check_cert_ready: ConnectError (non-SSL) for {domain} — treating as ready")
            return True
        except (httpx.TimeoutException, httpx.RemoteProtocolError):
            logger.debug(f"check_cert_ready: timeout/protocol error for {domain} — treating as ready")
            return True
        except Exception as exc:
            logger.debug(f"check_cert_ready: unexpected error for {domain}: {exc} — treating as ready")
            return True


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
        proxy_route: dict = {
            "@id": f"entry-{entry.id}",
            "match": [{"host": [entry.domain]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": entry.target_value}],
                }
            ],
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
    if has_http01:
        catchall_policy: dict = {"issuers": [{"module": "acme", "email": acme_email}]}
        # Prepend so domain-specific policies (DNS-01) take precedence.
        tls_policies.insert(0, catchall_policy)

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
