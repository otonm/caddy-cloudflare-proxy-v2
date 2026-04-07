# Plan 10 — UI: Add/Edit Form & Application Entry Point

## Goal

Implement `ui/form_page.py` (the add/edit form with dynamic SSL constraint enforcement
and domain autocomplete) and `main.py` (the application entry point). After this plan,
the application is fully functional end-to-end.

---

## Dependencies on Previous Plans

- All previous plans must be complete.
- Plan 08: all `proxy_service` functions
- Plan 03: `ProxyEntry`, `TargetType`, `SourceIPType`, `SSLMethod`, `DomainExistsError`, `ProxyTarget`, `CloudflareZone`
- Plan 08: `proxy_service.get_available_zones()` — returns `list[CloudflareZone]` sorted by name

---

## File: `ui/form_page.py`

### Page Routes

- `/entry/new` — create a new entry
- `/entry/{entry_id}` — edit an existing entry (domain is read-only)

Both routes render the same `_render_form` function with different initial state.

### Form Fields

```
┌─────────────────────────────────────────────────┐
│  New Proxy Entry (or: Edit: app.example.com)    │
├──────────────────────────────────────────────────┤
│ Cloudflare Zone *  [▼ example.com              ] │
│                    (single zone auto-selected)   │
│                                                  │
│ Domain *           [app.example.com____________] │
│                    OR [▼ choose existing domain] │
│                                                  │
│ Target Type *      ● Docker  ○ Tailscale  ○ Custom │
│                                                  │
│ [If Docker]                                      │
│ Container *        [▼ my-app                   ] │
│ Port *             [▼ 8080/tcp                 ] │
│                                                  │
│ [If Tailscale]                                   │
│ Device *           [▼ my-server [100.64.0.1]   ] │
│ Port *             [8080______________________ ] │
│                                                  │
│ [If Custom]                                      │
│ Host:Port *        [192.168.1.10:8080__________] │
│                                                  │
│ Source IP *        ● Public IP  ○ Tailscale IP   │
│                    (Tailscale IP disabled if not available) │
│                                                  │
│ SSL *              ● None  ○ HTTP-01  ○ DNS-01   │
│                    (HTTP-01 disabled for Tailscale source) │
│                                                  │
│ Notes (optional)   [________________________________] │
│                                                  │
│               [Cancel]  [Save Entry]             │
└──────────────────────────────────────────────────┘
```

### Cloudflare Zone Selector

Loaded asynchronously on page open via `proxy_service.get_available_zones()`.

- If **one zone**: rendered as a read-only label (no dropdown needed).
- If **multiple zones**: rendered as a `ui.select` dropdown, first zone alphabetically pre-selected.
- If **zero zones or error**: show an error notification and disable the Save button.

The selected zone's `id` is stored as `zone_id` in the submitted `ProxyEntry`.

### Domain Input with Autocomplete

The domain field has two interaction modes:

1. **Free text input**: user types a new FQDN.
2. **Existing domain picker**: a dropdown (or autocomplete) lists domains from all
   existing entries. If the user selects an existing domain, it navigates to the
   edit page for that entry.

When the user types a domain that already exists (and submits):
- `proxy_service.create_entry()` raises `DomainExistsError`
- The UI catches it and shows a warning card:
  ```
  ⚠ Domain 'app.example.com' already has a proxy entry.
  [Edit existing entry]   [Cancel]
  ```
  Clicking "Edit existing entry" navigates to `/entry/{existing_id}`.

In **edit mode**, the domain field is rendered as a read-only label (not an input),
since domain changes are not supported in v1.

### Dynamic Behaviour

1. **Target type selection** shows/hides the relevant sub-fields using `ui.conditional`
   or by clearing and re-rendering a container.

2. **Source IP change** immediately updates SSL options:
   - PUBLIC → all three SSL methods enabled
   - TAILSCALE → HTTP-01 option is disabled + tooltip: "HTTP-01 requires public reachability"
   - If HTTP-01 was selected when switching to TAILSCALE, reset SSL to NONE

3. **Target dropdowns** load asynchronously on page open. While loading: show
   spinner. If a source is unavailable (Docker socket not found, Tailscale API error):
   disable that target type option with a tooltip explaining why.

4. **TAILSCALE source IP** option is disabled if `proxy_service.get_tailscale_ip()` returns
   None, with tooltip: "Set TS_HOST_NAME env var to enable".

### Target Value Composition

Before submitting, compose `target_value` ("host:port") from the form inputs:

```python
def _compose_target_value(
    target_type: TargetType,
    container_name: str,
    container_port: str,   # e.g. "8080/tcp"
    ts_hostname: str,
    ts_port: str,
    custom_value: str,
) -> str:
    """Compose the target_value string in host:port format."""
    if target_type == TargetType.DOCKER:
        port_num = container_port.split("/")[0]  # "8080/tcp" → "8080"
        return f"{container_name}:{port_num}"
    if target_type == TargetType.TAILSCALE:
        return f"{ts_hostname}:{ts_port.strip()}"
    if target_type == TargetType.CUSTOM:
        return custom_value.strip()
    raise ValueError(f"Unknown target type: {target_type}")
```

### Submit Validation (client-side, before service call)

Run these checks on submit and show inline error labels:

| Check | Field | Error |
|---|---|---|
| Domain non-empty, contains `.` | domain | "Enter a valid domain (e.g. app.example.com)" |
| Docker: container selected | container | "Select a container" |
| Docker: port selected | port | "Select a port" |
| Tailscale: device selected | device | "Select a device" |
| Tailscale: port is numeric | ts_port | "Port must be a number" |
| Custom: contains `:` with numeric port | custom | "Enter host:port (e.g. 192.168.1.10:8080)" |
| SSL compatible with source IP | ssl | "HTTP-01 requires Public source IP" |

### Error Handling

- `DomainExistsError` → show warning card with "Edit existing entry" link
- `CloudflareError` → `ui.notify("Cloudflare error: {msg}", type="negative")`
- `CaddyError` → `ui.notify("Caddy error: {msg}", type="negative")`
- Other errors → `ui.notify(f"Error: {exc}", type="negative")`

After successful save/edit: navigate back to `/` and the main page will auto-reload.

---

## File: `main.py`

### Key Requirement: NiceGUI 3.x Startup

NiceGUI 3.x has **known conflicts** with FastAPI's `lifespan` context manager when using
`ui.run_with()`. The correct pattern is to use NiceGUI's own `app.on_startup` hook.

```python
from nicegui import app as nicegui_app

@nicegui_app.on_startup
async def startup() -> None:
    await proxy_service.startup()
```

This runs after NiceGUI initialises but before the server starts accepting requests.

### Implementation

```python
from __future__ import annotations
"""Application entry point.

Wires FastAPI, NiceGUI, and the proxy service startup sequence.
NiceGUI 3.x note: use app.on_startup from nicegui (not FastAPI lifespan)
to avoid known event handling conflicts when using ui.run_with().
"""

import logging

from fastapi import FastAPI
from nicegui import app as nicegui_app
from nicegui import ui

from core.config import configure_logging, settings
from core import proxy_service

# Import UI modules — the @ui.page decorators register routes as a side effect
import ui.main_page  # noqa: F401
import ui.form_page  # noqa: F401

logger = logging.getLogger(__name__)


@nicegui_app.on_startup
async def startup() -> None:
    """Run the proxy service startup sequence before serving requests."""
    logger.info("Starting Caddy Proxy Manager")
    try:
        await proxy_service.startup()
    except Exception as exc:
        # Log but don't crash — the UI will show degraded state
        logger.error("Startup error (app still running): %s", exc)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    configure_logging(settings.debug)
    return FastAPI(title="Caddy Proxy Manager")


if __name__ in {"__main__", "__mp_main__"}:
    app = create_app()
    ui.run_with(
        app,
        port=settings.app_port,   # wait — APP_PORT is a module-level constant, not in settings
        title="Caddy Proxy Manager",
        storage_secret="change-me",  # TODO Plan 10: move to APP_SECRET env var
    )
```

> **`APP_PORT` from config**: Since `APP_PORT` is a module-level constant (not a settings
> field), import it directly:
> ```python
> from core.config import APP_PORT
> ui.run_with(app, port=APP_PORT, ...)
> ```

> **`storage_secret`**: NiceGUI uses this for browser session storage encryption. For v1,
> a hardcoded default is used. To harden: add `APP_SECRET: SecretStr` to `Settings` and
> use `settings.app_secret.get_secret_value()`. Tracked as a follow-up.

> **`__mp_main__` check**: NiceGUI uses multiprocessing in some reload modes. The check
> `__name__ in {"__main__", "__mp_main__"}` ensures correct startup in all modes.

---

## `proxy_service` additions required by this plan

Plan 08 is missing two functions needed by the form page. Add them to `proxy_service.py`:

```python
def get_tailscale_ip() -> str | None:
    """Return the cached Tailscale IP of the Caddy host (set during initialize())."""
    return _tailscale_ip

def get_public_ip() -> str | None:
    """Return the cached public IP (set during initialize())."""
    return _public_ip

async def get_entry_by_id(entry_id: uuid.UUID) -> ProxyEntry | None:
    """Return a single entry by ID, or None."""
    return await store.get_entry(entry_id)
```

---

## Verification Steps (full end-to-end)

1. `uv run python main.py` — must start, logging must show startup messages.
2. Open `http://localhost:8080/` — main page renders.
3. Click "+ Add Entry" — form renders with all fields.
4. Select Docker → container dropdown loads (or shows "unavailable" gracefully).
5. Select TAILSCALE source IP → HTTP-01 SSL option becomes disabled.
6. Submit with an invalid domain → inline error shown, no API call made.
7. Submit valid form → entry appears in list, success notification shown.
8. Submit same domain again → warning card with "Edit existing entry" link appears.
9. Edit entry → form pre-populated, domain field read-only.
10. Delete entry → confirmation dialog → confirm → entry removed, DNS deleted, Caddy updated.
11. `uv run ruff check ui/form_page.py main.py --fix` — must pass clean.
