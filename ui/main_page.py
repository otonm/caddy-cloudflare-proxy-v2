"""Main page — displays the list of proxy entries.

Shows all configured proxy entries in a table-like layout with coloured badges
for source IP type, SSL method, and target type.  Provides edit navigation and
a confirmation-gated delete flow that removes the Caddy route and Cloudflare
A record via the proxy service.
"""

from __future__ import annotations

import asyncio
import logging

from nicegui import ui

import core.proxy_service as proxy_service
from core.models import ProxyEntry, SourceIPType, SSLMethod, TargetType
from ui.theme import apply_theme

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Badge colour and label mappings (module-level constants, never change)
# ---------------------------------------------------------------------------

_SOURCE_IP_COLORS: dict[SourceIPType, str] = {
    SourceIPType.PUBLIC: "blue",
    SourceIPType.TAILSCALE: "green",
}
_SOURCE_IP_LABELS: dict[SourceIPType, str] = {
    SourceIPType.PUBLIC: "Public",
    SourceIPType.TAILSCALE: "Tailscale",
}

_SSL_COLORS: dict[SSLMethod, str] = {
    SSLMethod.NONE: "grey",
    SSLMethod.HTTP01: "orange",
    SSLMethod.DNS01: "purple",
}
_SSL_LABELS: dict[SSLMethod, str] = {
    SSLMethod.NONE: "No SSL",
    SSLMethod.HTTP01: "HTTPS – Auto",
    SSLMethod.DNS01: "HTTPS – DNS",
}

_TARGET_COLORS: dict[TargetType, str] = {
    TargetType.DOCKER: "teal",
    TargetType.TAILSCALE: "green",
    TargetType.CUSTOM: "grey",
}
_TARGET_LABELS: dict[TargetType, str] = {
    TargetType.DOCKER: "Docker",
    TargetType.TAILSCALE: "Tailscale",
    TargetType.CUSTOM: "Custom",
}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@ui.page("/")
async def main_page() -> None:
    """Root page: entry list with edit and delete actions."""
    apply_theme()
    entries: list[ProxyEntry] = []

    # ---- nested helpers ---------------------------------------------------
    # All helpers close over `entries` and `content`.  `content` is assigned
    # below the helpers in the page-build section; by the time any helper is
    # *called* (asynchronously, after the page coroutine yields), `content`
    # is fully initialised.

    async def load_entries() -> None:
        """Fetch entries from the service and re-render the table."""
        nonlocal entries  # rebinds the name, so nonlocal is required
        try:
            entries = await proxy_service.list_entries()
        except Exception as exc:
            logger.error(f"Failed to load proxy entries: {exc}")
            _render_error(str(exc))
            return
        _render_table()

    def _render_error(message: str) -> None:
        """Replace table area with an error card and a retry button."""
        content.clear()
        with content, ui.card().classes("w-full"):
            ui.label(f"Error loading entries: {message}").classes("text-negative")
            ui.button(
                "Retry",
                icon="refresh",
                on_click=lambda: asyncio.create_task(load_entries()),
            ).props("flat")

    def _render_table() -> None:
        """Rebuild the entire table area from the current `entries` list."""
        content.clear()
        with content:
            if not entries:
                with ui.card().classes("w-full p-6"):
                    ui.label("No proxy entries yet.").classes("text-h6")
                    ui.label("Click '+ Add Entry' to get started.").classes("opacity-60")
                return

            # Column header row
            with ui.row().classes("w-full px-4 py-2 proxy-table-header rounded-t items-center text-weight-bold"):
                ui.label("Domain").classes("flex-1")
                ui.label("Target").classes("flex-1")
                ui.label("Source IP").classes("w-28")
                ui.label("SSL").classes("w-24")
                ui.label("Created").classes("w-28")
                ui.label("Actions").classes("w-24 text-center")

            ui.separator()

            for entry in entries:
                _render_row(entry)

    def _render_row(entry: ProxyEntry) -> None:
        """Render a single data row.

        Lambdas inside close over this function's `entry` parameter, which is
        a distinct binding per call — no loop-closure gotcha.
        """
        with ui.row().classes("w-full px-4 py-2 items-center border-b proxy-table-row"):
            # Domain — clickable link that opens the proxied URL in a new tab
            with ui.element("div").classes("flex-1"):
                ui.link(
                    entry.domain,
                    target=f"https://{entry.domain}",
                    new_tab=True,
                )

            # Target value + type badge (e.g. "my-app:8080  [Docker]")
            with ui.row().classes("flex-1 items-center gap-1"):
                ui.label(entry.target_value).classes("text-body2")
                ui.chip(
                    _TARGET_LABELS[entry.target_type],
                    color=_TARGET_COLORS[entry.target_type],
                ).props("dense outline")

            # Source IP badge
            with ui.element("div").classes("w-28"):
                ui.chip(
                    _SOURCE_IP_LABELS[entry.source_ip_type],
                    color=_SOURCE_IP_COLORS[entry.source_ip_type],
                ).props("dense")

            # SSL badge
            with ui.element("div").classes("w-24"):
                ui.chip(
                    _SSL_LABELS[entry.ssl_method],
                    color=_SSL_COLORS[entry.ssl_method],
                ).props("dense")

            # Created date (date portion only)
            ui.label(entry.created_at.strftime("%Y-%m-%d")).classes("w-28 text-body2")

            # Edit / delete action buttons
            with ui.row().classes("w-24 justify-center gap-1"):
                ui.button(
                    icon="edit",
                    color="primary",
                    on_click=lambda e=entry: ui.navigate.to(f"/entry/{e.id}"),
                ).props("dense flat round")
                ui.button(
                    icon="delete",
                    color="negative",
                    on_click=lambda e=entry: asyncio.create_task(confirm_delete(e)),
                ).props("dense flat round")

    async def confirm_delete(entry: ProxyEntry) -> None:
        """Show a confirmation dialog; on confirm, delete the entry and refresh."""
        # Create a fresh dialog each time so there is no stale UI state.
        dialog = ui.dialog()
        with dialog, ui.card().classes("w-80"):
            ui.label(f"Delete '{entry.domain}'?").classes("text-subtitle1 font-bold")
            ui.label("The Caddy route and Cloudflare A record will be permanently removed.").classes(
                "text-grey text-sm"
            )

            with ui.row().classes("justify-end w-full gap-2 mt-4"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                async def do_delete() -> None:
                    """Execute the deletion and handle success / failure."""
                    dialog.close()
                    try:
                        await proxy_service.delete_entry(entry.id)
                        ui.notify(f"Deleted: {entry.domain}", type="positive")
                        await load_entries()
                    except Exception as exc:
                        logger.error(f"Delete failed for {entry.domain}: {exc}")
                        ui.notify(f"Error: {exc}", type="negative")

                ui.button("Delete", color="negative", on_click=do_delete)

        dialog.open()

    # ---- page structure ---------------------------------------------------

    with ui.header().classes("items-center justify-between px-4"):
        ui.label("Caddy Proxy Manager").classes("text-h5 text-white")
        ui.button(
            "+ Add Entry",
            icon="add",
            on_click=lambda: ui.navigate.to("/entry/new"),
        )

    ui.separator()

    # `content` is placed in the page flow here; helpers reference it by
    # closure and are only called after this coroutine yields.
    content = ui.column().classes("w-full p-4 gap-0")

    asyncio.create_task(load_entries())
