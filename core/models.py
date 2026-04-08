"""Pydantic data models for the caddy-cloudflare-proxy application.

Defines enums for all categorical fields, the persisted ProxyEntry/ProxyConfig
models, and runtime-only helper types used by the UI layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class TargetType(StrEnum):
    """The kind of resource being proxied."""

    DOCKER = "docker"  # a running Docker container
    TAILSCALE = "tailscale"  # a Tailscale network device
    CUSTOM = "custom"  # a user-supplied host:port


class SourceIPType(StrEnum):
    """Which IP address the Cloudflare A record should point to."""

    PUBLIC = "public"  # public/external IP of the Caddy host
    TAILSCALE = "tailscale"  # Tailscale IP of the Caddy host


class SSLMethod(StrEnum):
    """How (or whether) to obtain a TLS certificate for this entry."""

    NONE = "none"  # HTTP only, no certificate
    HTTP01 = "http01"  # ACME HTTP-01 challenge (requires public reachability on port 80)
    DNS01 = "dns01"  # ACME DNS-01 challenge via Cloudflare (works behind NAT/Tailscale)


class ProxyEntry(BaseModel):
    """A single reverse-proxy rule managed by this application.

    The domain field is the natural unique key — uniqueness is enforced by the
    store layer, not here, so the model stays a pure value object.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    domain: str  # fully-qualified, e.g. "app.example.com"
    zone_id: str  # Cloudflare zone ID — selected by the user in the form
    target_type: TargetType
    target_value: str  # always "host:port" format
    source_ip_type: SourceIPType
    ssl_method: SSLMethod
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        """Normalise and sanity-check the domain name."""
        v = v.strip().lower()
        if not v or "." not in v:
            raise ValueError(f"Domain must be a valid FQDN, got: {v!r}")
        if v.startswith("*"):
            raise ValueError("Wildcard domains are not supported")
        return v

    @field_validator("target_value")
    @classmethod
    def validate_target_value(cls, v: str) -> str:
        """Ensure target_value is in host:port format with a numeric port."""
        v = v.strip()
        if not v:
            raise ValueError("target_value must not be empty")
        if ":" not in v:
            raise ValueError(f"target_value must be 'host:port', got: {v!r}")
        host, _, port = v.rpartition(":")
        if not host:
            raise ValueError(f"target_value has empty host: {v!r}")
        if not port.isdigit():
            raise ValueError(f"target_value port must be numeric, got: {port!r}")
        return v

    @model_validator(mode="after")
    def validate_ssl_compatibility(self) -> ProxyEntry:
        """Enforce the SSL/source-IP compatibility matrix from the spec.

        HTTP-01 requires the domain to be publicly reachable on port 80,
        which is impossible when the A record points to a Tailscale IP.
        """
        if self.source_ip_type == SourceIPType.TAILSCALE and self.ssl_method == SSLMethod.HTTP01:
            raise ValueError("HTTP-01 SSL is incompatible with Tailscale source IP. Use DNS-01 or None.")
        return self


class ProxyConfig(BaseModel):
    """Top-level container serialised to /data/proxy_config.json."""

    version: int = 1  # schema version, reserved for future migrations
    entries: list[ProxyEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Runtime-only helper types — used by the UI and service layers, never persisted
# ---------------------------------------------------------------------------


class CloudflareZone(BaseModel):
    """A Cloudflare DNS zone available for A-record management (UI use only)."""

    id: str  # Cloudflare zone ID
    name: str  # zone apex, e.g. "example.com"


class ContainerInfo(BaseModel):
    """A running Docker container available as a proxy target."""

    name: str
    id: str  # 12-char short ID
    image: str
    ports: list[str]  # e.g. ["8080/tcp", "443/tcp"]


class TailscaleDevice(BaseModel):
    """A Tailscale device available as a proxy target or source."""

    name: str  # FQDN, e.g. "my-server.example.com"
    hostname: str  # short name, e.g. "my-server"
    ip: str  # first IPv4 address (100.x.x.x)


class ProxyTarget(BaseModel):
    """Unified representation of any available proxy target (UI use only)."""

    label: str  # human-readable display name
    value: str  # stored as ProxyEntry.target_value ("host:port")
    target_type: TargetType


class CfARecord(BaseModel):
    """A Cloudflare DNS A record — runtime-only, never persisted.

    Used by the main page to display A records that exist in Cloudflare but
    are not managed by this application (i.e. have no matching ProxyEntry).
    """

    record_id: str  # Cloudflare DNS record ID
    name: str  # FQDN, e.g. "app.example.com"
    content: str  # IP address the record points to
    proxied: bool  # whether Cloudflare's proxy is enabled
    zone_id: str  # parent Cloudflare zone ID
    zone_name: str  # parent zone apex, e.g. "example.com"
