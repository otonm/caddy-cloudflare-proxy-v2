# Plan 03 — Data Models & Persistence Store

## Goal

Define all Pydantic data models (`core/models.py`) and implement async JSON-file
persistence (`core/store.py`). After this plan, the rest of the app has a type-safe
contract for proxy entries and a reliable way to read/write them.

---

## Dependencies on Previous Plans

- Plan 02: uses `CONFIG_FILE` constant from `core/config.py`.

---

## File: `core/models.py`

### Design Decisions

- Use Python `Enum` for all categorical fields — never raw strings.
- Enforce the SSL/source-IP compatibility rules inside the model via `@model_validator`
  so that an invalid combination can never be persisted or passed around.
- Use `uuid.UUID` as the entry ID to avoid collisions.
- `ProxyEntry.domain` is the natural unique key — no two entries may share a domain.
  However, the uniqueness is enforced at the **store layer** (not the model), so that
  the model itself remains a pure value object.

### Enums

```python
class TargetType(str, Enum):
    DOCKER    = "docker"      # a running Docker container
    TAILSCALE = "tailscale"   # a Tailscale network device
    CUSTOM    = "custom"      # a user-supplied host:port

class SourceIPType(str, Enum):
    PUBLIC    = "public"      # public/external IP of the Caddy host
    TAILSCALE = "tailscale"   # Tailscale IP of the Caddy host

class SSLMethod(str, Enum):
    NONE   = "none"           # HTTP only, no certificate
    HTTP01 = "http01"         # ACME HTTP-01 challenge (Let's Encrypt)
    DNS01  = "dns01"          # ACME DNS-01 challenge via Cloudflare
```

### SSL Compatibility Rules (from CLAUDE.md)

| SourceIPType | Allowed SSLMethods              |
|--------------|----------------------------------|
| PUBLIC       | NONE, HTTP01, DNS01              |
| TAILSCALE    | NONE, DNS01 (NOT HTTP01)         |

HTTP-01 requires the domain to be publicly reachable on port 80 — impossible when the
A record points to a Tailscale IP (private network).

### `ProxyEntry` Model

```python
class ProxyEntry(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    domain: str                    # fully-qualified, e.g. "app.example.com"
    target_type: TargetType
    target_value: str              # always "host:port" format
    source_ip_type: SourceIPType
    ssl_method: SSLMethod
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "." not in v:
            raise ValueError(f"Domain must be a valid FQDN, got: {v!r}")
        if v.startswith("*"):
            raise ValueError("Wildcard domains are not supported")
        return v

    @field_validator("target_value")
    @classmethod
    def validate_target_value(cls, v: str) -> str:
        """Ensure target_value is in host:port format."""
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
        """Enforce the SSL/source-IP compatibility matrix from the spec."""
        if self.source_ip_type == SourceIPType.TAILSCALE and self.ssl_method == SSLMethod.HTTP01:
            raise ValueError(
                "HTTP-01 SSL is incompatible with Tailscale source IP. "
                "Use DNS-01 or None."
            )
        return self
```

### `ProxyConfig` Model

Top-level container for the JSON file:

```python
class ProxyConfig(BaseModel):
    version: int = 1               # schema version for future migrations
    entries: list[ProxyEntry] = Field(default_factory=list)
```

### Runtime-Only Helper Types (not persisted)

```python
class ContainerInfo(BaseModel):
    """A running Docker container available as a proxy target."""
    name: str
    id: str          # 12-char short ID
    image: str
    ports: list[str] # e.g. ["8080/tcp", "443/tcp"]

class TailscaleDevice(BaseModel):
    """A Tailscale device available as a proxy target or source."""
    name: str        # FQDN, e.g. "my-server.example.com"
    hostname: str    # short name, e.g. "my-server"
    ip: str          # first IPv4 address (100.x.x.x)

class ProxyTarget(BaseModel):
    """Unified representation of any available proxy target (UI use only)."""
    label: str           # human-readable display name
    value: str           # stored as ProxyEntry.target_value ("host:port")
    target_type: TargetType
```

---

## File: `core/store.py`

### Design Decisions

- Single JSON file at `CONFIG_FILE` (`/data/proxy_config.json`).
- All reads/writes are async via `aiofiles`.
- Writes are atomic: write to `.tmp`, then `os.replace()`.
- On first load (file missing), create an empty `ProxyConfig` and write it.
- Domain uniqueness is enforced here — **not** in the model.

### Domain Uniqueness UX

Domains are unique: one domain → one proxy entry. However, the behaviour when a
duplicate domain is attempted differs by context:

- **`add_entry()`**: raises `DomainExistsError` (custom exception, subclass of `ValueError`)
  that carries the **existing entry's ID**. The proxy service catches this and surfaces
  it to the UI, which uses the ID to offer "Edit existing entry" navigation.
- **`update_entry()`**: when the entry being updated already has the same domain (no
  domain change), skip the uniqueness check. Domain is read-only in the edit form
  (Plan 10), so this case doesn't arise in practice — but be defensive.

### Custom Exception

```python
class DomainExistsError(ValueError):
    """Raised when attempting to add an entry for a domain that already exists."""
    def __init__(self, domain: str, existing_id: uuid.UUID) -> None:
        super().__init__(f"Domain {domain!r} already has a proxy entry")
        self.domain = domain
        self.existing_id = existing_id
```

### Public API

```python
async def load_config() -> ProxyConfig:
    """Load the full proxy config from disk. Creates file with empty config if missing."""

async def save_config(config: ProxyConfig) -> None:
    """Atomically write the full proxy config to disk."""

async def get_entry(entry_id: uuid.UUID) -> ProxyEntry | None:
    """Return a single entry by ID, or None if not found."""

async def add_entry(entry: ProxyEntry) -> ProxyEntry:
    """Append a new entry.

    Raises DomainExistsError (carrying the existing entry's ID) if domain already exists.
    """

async def update_entry(entry: ProxyEntry) -> ProxyEntry:
    """Replace an existing entry by ID. Raises KeyError if not found."""

async def delete_entry(entry_id: uuid.UUID) -> ProxyEntry:
    """Remove an entry by ID. Returns the removed entry. Raises KeyError if not found."""

async def list_entries() -> list[ProxyEntry]:
    """Return all entries ordered by created_at ascending."""
```

### Atomic Write Pattern

```python
async def save_config(config: ProxyConfig) -> None:
    tmp_path = CONFIG_FILE.with_suffix(".tmp")
    # Ensure parent directory exists (first run in container)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
        await f.write(config.model_dump_json(indent=2))
    os.replace(tmp_path, CONFIG_FILE)  # atomic on POSIX; near-instant for small files
    logger.debug("Config saved: %d entries", len(config.entries))
```

### `add_entry` with Domain Check

```python
async def add_entry(entry: ProxyEntry) -> ProxyEntry:
    config = await load_config()
    for existing in config.entries:
        if existing.domain == entry.domain:
            raise DomainExistsError(entry.domain, existing.id)
    config.entries.append(entry)
    await save_config(config)
    logger.info("Added entry: %s → %s", entry.domain, entry.target_value)
    return entry
```

---

## Verification Steps

1. Test SSL validator: construct `ProxyEntry` with
   `source_ip_type=TAILSCALE, ssl_method=HTTP01` → must raise `ValidationError`.
2. Test domain validator: `domain="nodot"` → `ValidationError`; `domain="*wildcard.x"` → `ValidationError`.
3. Test `target_value` validator: `"myapp"` (no colon) → `ValidationError`; `"myapp:abc"` (non-numeric port) → `ValidationError`.
4. Test `DomainExistsError`: add the same domain twice → second call raises `DomainExistsError` with correct `existing_id`.
5. Test atomic write: write, interrupt (simulate), verify `.tmp` cleanup.
6. `uv run ruff check core/models.py core/store.py --fix` — must pass clean.
