"""Main page — displays managed proxy entries and unmanaged Cloudflare A records.

Layout:
  - "Proxies" section: all ProxyEntry rows managed by Caddy.
  - "Unmanaged Domains" section: Cloudflare A records that have no matching
    ProxyEntry, grouped by zone, with a delete action to remove them from CF.
"""

from __future__ import annotations

import asyncio
import logging

from nicegui import ui

import core.proxy_service as proxy_service
from core.models import CfARecord, CloudflareZone, ProxyEntry, SourceIPType, SSLMethod, TargetType
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
    """Root page: managed proxies and unmanaged Cloudflare A records."""
    apply_theme()
    entries: list[ProxyEntry] = []
    unmanaged: list[tuple[CloudflareZone, list[CfARecord]]] = []
    ts_labels: dict[str, str] = {}

    # ---- nested helpers ---------------------------------------------------
    # All helpers close over `entries`, `unmanaged`, and `content`.  `content`
    # is assigned below; by the time any helper is *called* (asynchronously,
    # after the page coroutine yields), `content` is fully initialised.

    async def load_data() -> None:
        """Fetch entries, unmanaged records, and Tailscale IP labels, then re-render."""
        nonlocal entries, unmanaged, ts_labels
        try:
            entries, unmanaged = await asyncio.gather(
                proxy_service.list_entries(),
                proxy_service.get_unmanaged_records(),
            )
        except Exception as exc:
            logger.error(f"Failed to load page data: {exc}")
            _render_error(str(exc))
            return
        # Tailscale label map is best-effort — a failure must not block the page.
        try:
            ts_labels = await proxy_service.get_tailscale_ip_to_label()
        except Exception as exc:
            logger.warning(f"Could not load Tailscale IP labels: {exc}")
            ts_labels = {}
        _render_all()

    def _render_error(message: str) -> None:
        """Replace content area with an error card and a retry button."""
        content.clear()
        with content, ui.card().classes("w-full"):
            ui.label(f"Error loading data: {message}").classes("text-negative")
            ui.button(
                "Retry",
                icon="refresh",
                on_click=lambda: asyncio.create_task(load_data()),
            ).props("flat")

    def _render_all() -> None:
        """Rebuild the entire content area."""
        content.clear()
        with content:
            _render_proxies_section()
            _render_unmanaged_section()

    # -- Proxies section ----------------------------------------------------

    def _render_proxies_section() -> None:
        """Render the 'Proxies' subtitle and the managed entries table."""
        ui.label("Proxies").classes("text-h6 text-weight-bold mt-2")
        ui.separator()

        if not entries:
            with ui.card().classes("w-full p-6 mt-2"):
                ui.label("No proxy entries yet.").classes("text-subtitle1")
                ui.label("Click '+ Add Entry' to get started.").classes("opacity-60")
            return

        with ui.row().classes("w-full px-4 py-2 proxy-table-header rounded-t items-center text-weight-bold mt-2"):
            ui.label("Domain").classes("flex-1")
            ui.label("Target").classes("flex-1")
            ui.label("Source IP").classes("w-28")
            ui.label("SSL").classes("w-24")
            ui.label("Created").classes("w-28")
            ui.label("Actions").classes("w-24 text-center")

        ui.separator()

        for entry in entries:
            _render_proxy_row(entry)

    def _render_proxy_row(entry: ProxyEntry) -> None:
        """Render a single managed-proxy row with an inline confirmation dialog.

        The dialog is built here while the NiceGUI slot context is active.
        The delete button just calls dialog.open() — no asyncio.create_task needed.
        """
        # Pre-build the confirmation dialog in the current slot context.
        dialog = ui.dialog()
        with dialog, ui.card().classes("w-80"):
            ui.label(f"Delete '{entry.domain}'?").classes("text-subtitle1 font-bold")
            ui.label("The Caddy route and Cloudflare A record will be permanently removed.").classes(
                "text-grey text-sm"
            )
            with ui.row().classes("justify-end w-full gap-2 mt-4"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                async def do_delete(e: ProxyEntry = entry) -> None:
                    dialog.close()
                    try:
                        await proxy_service.delete_entry(e.id)
                        ui.notify(f"Deleted: {e.domain}", type="positive")
                        await load_data()
                    except Exception as exc:
                        logger.error(f"Delete failed for {e.domain}: {exc}")
                        ui.notify(f"Error: {exc}", type="negative")

                ui.button("Delete", color="negative", on_click=do_delete)

        with ui.row().classes("w-full px-4 py-2 items-center border-b proxy-table-row"):
            with ui.element("div").classes("flex-1"):
                ui.link(
                    entry.domain,
                    target=f"https://{entry.domain}",
                    new_tab=True,
                )

            with ui.row().classes("flex-1 items-center gap-1"):
                if entry.target_type == TargetType.TAILSCALE:
                    host, _, port = entry.target_value.partition(":")
                    display_host = ts_labels.get(host, host)
                    target_display = f"{display_host}:{port}" if port else display_host
                    ui.label(target_display).classes("text-body2")
                else:
                    ui.label(entry.target_value).classes("text-body2")
                ui.chip(
                    _TARGET_LABELS[entry.target_type],
                    color=_TARGET_COLORS[entry.target_type],
                ).props("dense outline")

            with ui.element("div").classes("w-28"):
                ui.chip(
                    _SOURCE_IP_LABELS[entry.source_ip_type],
                    color=_SOURCE_IP_COLORS[entry.source_ip_type],
                ).props("dense")

            ssl_cell = ui.element("div").classes("w-24")
            with ssl_cell:
                if entry.ssl_method == SSLMethod.NONE:
                    ui.chip(
                        _SSL_LABELS[entry.ssl_method],
                        color=_SSL_COLORS[entry.ssl_method],
                    ).props("dense")
                else:
                    # Show spinner until cert check resolves; replaced by chip if cert is ready.
                    ui.spinner("dots", size="sm", color=_SSL_COLORS[entry.ssl_method])

            if entry.ssl_method != SSLMethod.NONE:

                async def _update_ssl_cell(e: ProxyEntry = entry, cell: ui.element = ssl_cell) -> None:
                    ready = await proxy_service.check_cert_ready(e.domain)
                    if ready:
                        cell.clear()
                        with cell:
                            ui.chip(
                                _SSL_LABELS[e.ssl_method],
                                color=_SSL_COLORS[e.ssl_method],
                            ).props("dense")

                asyncio.create_task(_update_ssl_cell())

            ui.label(entry.created_at.strftime("%Y-%m-%d")).classes("w-28 text-body2")

            with ui.row().classes("w-24 justify-center gap-1"):
                ui.button(
                    icon="edit",
                    color="primary",
                    on_click=lambda e=entry: ui.navigate.to(f"/entry/{e.id}"),
                ).props("dense flat round")
                ui.button(
                    icon="delete",
                    color="negative",
                    on_click=dialog.open,
                ).props("dense flat round")

    # -- Unmanaged Domains section ------------------------------------------

    def _render_unmanaged_section() -> None:
        """Render the 'Unmanaged Domains' subtitle and per-zone sub-sections."""
        if not unmanaged:
            return

        ui.label("Unmanaged Domains").classes("text-h6 text-weight-bold mt-6")
        ui.separator()

        for zone, records in unmanaged:
            _render_zone_block(zone, records)

    def _render_zone_block(zone: CloudflareZone, records: list[CfARecord]) -> None:
        """Render the sub-subtitle and record rows for one Cloudflare zone."""
        ui.label(zone.name).classes("text-subtitle1 text-weight-medium mt-4")

        with ui.row().classes("w-full px-4 py-2 proxy-table-header rounded-t items-center text-weight-bold"):
            ui.label("Domain").classes("flex-1")
            ui.label("Target").classes("flex-1")
            ui.label("Proxied").classes("w-24")
            ui.label("Actions").classes("w-24 text-center")

        ui.separator()

        for record in records:
            _render_unmanaged_row(record)

    def _render_unmanaged_row(record: CfARecord) -> None:
        """Render a single unmanaged A record row with an inline confirmation dialog.

        The dialog is built here while the NiceGUI slot context is active.
        The delete button just calls dialog.open() — no asyncio.create_task needed.
        """
        # Pre-build the confirmation dialog in the current slot context.
        dialog = ui.dialog()
        with dialog, ui.card().classes("w-80"):
            ui.label(f"Delete '{record.name}'?").classes("text-subtitle1 font-bold")
            ui.label(f"The Cloudflare A record pointing to {record.content} will be permanently deleted.").classes(
                "text-grey text-sm"
            )
            with ui.row().classes("justify-end w-full gap-2 mt-4"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                async def do_delete_cf(r: CfARecord = record) -> None:
                    dialog.close()
                    try:
                        await proxy_service.delete_cloudflare_record(r.zone_id, r.record_id)
                        ui.notify(f"Deleted: {r.name}", type="positive")
                        await load_data()
                    except Exception as exc:
                        logger.error(f"Delete failed for CF record {r.name}: {exc}")
                        ui.notify(f"Error: {exc}", type="negative")

                ui.button("Delete", color="negative", on_click=do_delete_cf)

        with ui.row().classes("w-full px-4 py-2 items-center border-b proxy-table-row"):
            with ui.element("div").classes("flex-1"):
                ui.link(
                    record.name,
                    target=f"https://{record.name}",
                    new_tab=True,
                )

            ui.label(record.content).classes("flex-1 text-body2 font-mono")

            with ui.element("div").classes("w-24"):
                if record.proxied:
                    ui.chip("Proxied", color="orange").props("dense")
                else:
                    ui.chip("DNS only", color="grey").props("dense")

            with ui.row().classes("w-24 justify-center"):
                ui.button(
                    icon="delete",
                    color="negative",
                    on_click=dialog.open,
                ).props("dense flat round")

    # ---- page structure ---------------------------------------------------

    with ui.header().classes("items-center justify-between px-4"):
        ui.label("Caddy Proxy Manager").classes("text-h5 text-white")
        ui.button(
            "Add Entry",
            icon="add",
            on_click=lambda: ui.navigate.to("/entry/new"),
        )

    ui.separator()

    content = ui.column().classes("w-full p-4 gap-0")

    asyncio.create_task(load_data())
