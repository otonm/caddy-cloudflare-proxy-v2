# Plan 09 — UI: Main Page (Entry List)

## Goal

Implement `ui/main_page.py`: the NiceGUI page that displays the list of proxy entries,
allows navigation to the add/edit form, and deletes entries (including the Cloudflare
A record) with a confirmation dialog.

---

## Dependencies on Previous Plans

- Plan 03: `ProxyEntry`, `SourceIPType`, `SSLMethod`, `TargetType`
- Plan 08: `proxy_service.list_entries()`, `proxy_service.delete_entry()`
- Plan 01: `nicegui>=3.9` in `pyproject.toml`

---

## NiceGUI 3.x Notes

**Always verify the current NiceGUI 3.x docs at https://nicegui.io/documentation
before implementing — APIs changed significantly from 2.x to 3.x.**

Key 3.x patterns:
- Pages use `@ui.page("/path")` decorator on an `async def` function.
- Startup hooks use `app.on_startup(coroutine)` from `nicegui` (NOT FastAPI lifespan).
- Navigation: `ui.navigate.to("/path")`.
- `asyncio.create_task()` is safe inside page handlers (they run in NiceGUI's loop).

---

## File: `ui/main_page.py`

### Page Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Caddy Proxy Manager                          [+ Add Entry]  │
├──────────┬───────────────┬───────────┬──────────┬───────────┤
│ Domain   │ Target        │ Source IP │ SSL      │ Actions   │
├──────────┼───────────────┼───────────┼──────────┼───────────┤
│ app.x.co │ my-app:8080   │ Public    │ HTTP-01  │ [Edit][✕] │
│ vpn.x.co │ server:443    │ Tailscale │ DNS-01   │ [Edit][✕] │
└──────────┴───────────────┴───────────┴──────────┴───────────┘
```

### Table Columns

| Column    | Value                                           | Notes                         |
|-----------|-------------------------------------------------|-------------------------------|
| Domain    | `entry.domain`                                  | Plain link that opens the URL |
| Target    | `entry.target_value` + `entry.target_type` badge| e.g. "my-app:8080 (Docker)"   |
| Source IP | `entry.source_ip_type`                          | Coloured badge                |
| SSL       | `entry.ssl_method`                              | Coloured badge                |
| Created   | `entry.created_at` formatted as date            |                               |
| Actions   | Edit button + Delete button                     |                               |

Badge colours:
- Source IP: PUBLIC = blue, TAILSCALE = green
- SSL: NONE = grey, HTTP01 = orange, DNS01 = purple
- Target type: DOCKER = teal, TAILSCALE = green, CUSTOM = grey

### Page Skeleton

```python
@ui.page("/")
async def main_page() -> None:
    entries: list[ProxyEntry] = []
    table_container = ui.element("div").classes("w-full")

    async def load_entries() -> None:
        nonlocal entries
        try:
            entries = await proxy_service.list_entries()
        except Exception as exc:
            logger.error("Failed to load entries: %s", exc)
            entries = []
        render_table()

    def render_table() -> None:
        table_container.clear()
        with table_container:
            if not entries:
                with ui.card().classes("w-full"):
                    ui.label("No proxy entries yet.")
                    ui.label("Click '+ Add Entry' to get started.").classes("text-grey")
                return
            with ui.table(...):  # see column definitions below
                for entry in entries:
                    render_row(entry)

    with ui.header().classes("items-center justify-between px-4"):
        ui.label("Caddy Proxy Manager").classes("text-h5 text-white")
        ui.button("Add Entry", icon="add", on_click=lambda: ui.navigate.to("/entry/new"))

    ui.separator()
    table_container

    asyncio.create_task(load_entries())
```

### Delete Flow

When the user clicks delete:
1. A confirmation dialog appears with:
   - Message: `"Delete proxy entry for '{domain}'?"`
   - Sub-text: `"The Caddy route and Cloudflare A record will be permanently removed."`
   - Buttons: **Cancel** and **Delete** (red)
2. On confirm: call `proxy_service.delete_entry(entry.id)` (DNS deletion is now
   always included — no checkbox needed since the answer is always "yes").
3. Success → `ui.notify(f"Deleted: {entry.domain}", type="positive")` + refresh table.
4. Failure → `ui.notify(f"Error: {exc}", type="negative")` + keep table as-is.

```python
async def confirm_delete(entry: ProxyEntry) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-80"):
        ui.label(f"Delete '{entry.domain}'?").classes("text-subtitle1 font-bold")
        ui.label("The Caddy route and Cloudflare A record will be permanently removed.").classes("text-grey text-sm")
        with ui.row().classes("justify-end w-full gap-2 mt-4"):
            ui.button("Cancel", on_click=dialog.close)
            async def do_delete() -> None:
                dialog.close()
                try:
                    await proxy_service.delete_entry(entry.id)
                    ui.notify(f"Deleted: {entry.domain}", type="positive")
                    await load_entries()
                except Exception as exc:
                    logger.error("Delete failed for %s: %s", entry.domain, exc)
                    ui.notify(f"Error: {exc}", type="negative")
            ui.button("Delete", color="negative", on_click=do_delete)
    dialog.open()
```

### Edit Navigation

```python
ui.button(icon="edit", color="primary", on_click=lambda e=entry: ui.navigate.to(f"/entry/{e.id}"))
```

---

## Error States

- If `list_entries()` raises: show an error card with the message and a **Retry** button
  that calls `asyncio.create_task(load_entries())`.
- If delete fails: `ui.notify` error, do not remove the entry from the table.

---

## Verification Steps

1. Navigate to `http://localhost:8080/` — page must load without errors.
2. With entries in the store, verify the table shows all entries with correct data.
3. Delete flow: dialog appears → Cancel → entry stays; Confirm → entry removed,
   `ui.notify` shown, table refreshed.
4. "Edit" button navigates to `/entry/{id}`.
5. "Add Entry" navigates to `/entry/new`.
6. `uv run ruff check ui/main_page.py --fix` — must pass clean.
