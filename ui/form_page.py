"""Add / edit proxy entry form page.

Two routes share the same ``_render_form`` implementation:
- ``/entry/new``       — create a new proxy entry
- ``/entry/{entry_id}`` — edit an existing entry (domain read-only)

Dynamic behaviour:
- Target type radio shows/hides the relevant sub-fields.
- Source IP radio updates SSL options: HTTP-01 is excluded when TAILSCALE
  source is selected (it requires public port-80 reachability).
- Tailscale source IP option is excluded if the Caddy host's Tailscale IP
  was not resolved at startup.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from nicegui import ui

import core.proxy_service as proxy_service
from core.caddy_client import CaddyError
from core.cloudflare_client import CloudflareError
from core.models import (
    CloudflareZone,
    ProxyEntry,
    ProxyTarget,
    SourceIPType,
    SSLMethod,
    TargetType,
)
from core.store import DomainExistsError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label / colour constants — duplicated from main_page intentionally to keep
# each module self-contained; they are stable, short, and trivial to maintain.
# ---------------------------------------------------------------------------

_SSL_LABELS: dict[SSLMethod, str] = {
    SSLMethod.NONE: "None",
    SSLMethod.HTTP01: "HTTP-01",
    SSLMethod.DNS01: "DNS-01",
}

_TARGET_LABELS: dict[TargetType, str] = {
    TargetType.DOCKER: "Docker",
    TargetType.TAILSCALE: "Tailscale",
    TargetType.CUSTOM: "Custom",
}

_SOURCE_IP_LABELS: dict[SourceIPType, str] = {
    SourceIPType.PUBLIC: "Public IP",
    SourceIPType.TAILSCALE: "Tailscale IP",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _compose_target_value(
    target_type: TargetType,
    container_name: str,
    container_port: str,
    ts_hostname: str,
    ts_port: str,
    custom_value: str,
) -> str:
    """Compose the ``target_value`` string in ``host:port`` format.

    Each branch corresponds to one TargetType.  The port for Docker targets
    is already a plain number because ``container_port`` comes from the
    two-level select (container → port), not from a raw ``port/proto`` string.
    """
    if target_type == TargetType.DOCKER:
        return f"{container_name}:{container_port}"
    if target_type == TargetType.TAILSCALE:
        return f"{ts_hostname}:{ts_port.strip()}"
    # CUSTOM
    return custom_value.strip()


def _ssl_options(source_ip: SourceIPType) -> dict[SSLMethod, str]:
    """Return SSL method → label dict for the given source IP type.

    HTTP-01 is excluded for TAILSCALE source because it requires public
    port-80 reachability, which a Tailscale IP does not provide.
    """
    methods = proxy_service.get_available_ssl_methods(source_ip)
    return {m: _SSL_LABELS[m] for m in methods}


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@ui.page("/entry/new")
async def new_entry_page() -> None:
    """Create-new-entry page."""
    await _render_form(entry_id=None)


@ui.page("/entry/{entry_id}")
async def edit_entry_page(entry_id: str) -> None:
    """Edit-existing-entry page.  ``entry_id`` is a UUID string from the URL."""
    try:
        eid = uuid.UUID(entry_id)
    except ValueError:
        logger.warning(f"Invalid entry_id in URL: {entry_id!r} — redirecting to list")
        ui.navigate.to("/")
        return
    await _render_form(entry_id=eid)


# ---------------------------------------------------------------------------
# Shared form renderer
# ---------------------------------------------------------------------------


async def _render_form(entry_id: uuid.UUID | None) -> None:  # noqa: PLR0912, PLR0915
    """Build and wire the add/edit form.

    This is a single async function so the UI builds synchronously after the
    initial data load; NiceGUI sees a single coherent page tree.
    """
    is_edit = entry_id is not None

    # ---- header -----------------------------------------------------------
    with ui.header().classes("items-center justify-between px-4"):
        title = "Edit Proxy Entry" if is_edit else "New Proxy Entry"
        ui.label(title).classes("text-h5 text-white")
        ui.button(
            "Back",
            icon="arrow_back",
            on_click=lambda: ui.navigate.to("/"),
        ).props("flat color=white")

    ui.separator()

    # ---- loading state ----------------------------------------------------
    content = ui.column().classes("w-full max-w-2xl mx-auto p-4")
    with content:
        spinner = ui.spinner(size="lg").classes("self-center mt-8")

    # ---- concurrent data load ---------------------------------------------
    zone_result: Any
    target_result: Any
    entries_result: Any
    zone_result, target_result, entries_result = await asyncio.gather(
        proxy_service.get_available_zones(),
        proxy_service.get_available_targets(),
        proxy_service.list_entries(),
        return_exceptions=True,
    )

    zones: list[CloudflareZone] = zone_result if isinstance(zone_result, list) else []
    all_targets: list[ProxyTarget] = target_result if isinstance(target_result, list) else []
    existing_entries: list[ProxyEntry] = entries_result if isinstance(entries_result, list) else []

    zones_failed = isinstance(zone_result, Exception)
    targets_failed = isinstance(target_result, Exception)

    if zones_failed:
        logger.error(f"Failed to load Cloudflare zones: {zone_result}")
    if targets_failed:
        logger.warning(f"Failed to load proxy targets: {target_result}")

    # ---- load entry for edit mode -----------------------------------------
    existing_entry: ProxyEntry | None = None
    if is_edit:
        existing_entry = await proxy_service.get_entry_by_id(entry_id)  # type: ignore[arg-type]
        if existing_entry is None:
            spinner.delete()
            with content:
                ui.label("Entry not found.").classes("text-negative text-h6")
                ui.button("Back to list", on_click=lambda: ui.navigate.to("/"))
            return

    spinner.delete()
    content.clear()

    # ---- group targets by type --------------------------------------------
    docker_targets = [t for t in all_targets if t.target_type == TargetType.DOCKER]
    ts_targets = [t for t in all_targets if t.target_type == TargetType.TAILSCALE]

    # docker_by_container: container_name → [port, ...]  (ports already numeric)
    docker_by_container: dict[str, list[str]] = defaultdict(list)
    for t in docker_targets:
        name, _, port = t.value.partition(":")
        docker_by_container[name].append(port)

    # ts_options: hostname → label
    ts_options: dict[str, str] = {t.value: t.label for t in ts_targets}

    # ---- initial values (from existing entry or defaults) -----------------
    init_target_type: TargetType = existing_entry.target_type if existing_entry else TargetType.DOCKER
    init_source_ip: SourceIPType = existing_entry.source_ip_type if existing_entry else SourceIPType.PUBLIC
    init_ssl: SSLMethod = existing_entry.ssl_method if existing_entry else SSLMethod.NONE
    init_notes: str = existing_entry.notes if existing_entry else ""

    # Parse target_value for sub-field pre-population
    init_container_name = ""
    init_container_port = ""
    init_ts_hostname = ""
    init_ts_port = ""
    init_custom = ""
    if existing_entry:
        host, _, port = existing_entry.target_value.partition(":")
        if existing_entry.target_type == TargetType.DOCKER:
            init_container_name = host
            init_container_port = port
            # Ensure stored container is in options (may not be running now)
            if host not in docker_by_container:
                docker_by_container[host] = [port]
            elif port not in docker_by_container[host]:
                docker_by_container[host].append(port)
        elif existing_entry.target_type == TargetType.TAILSCALE:
            init_ts_hostname = host
            init_ts_port = port
            # Ensure stored device is in options
            if host not in ts_options:
                ts_options[host] = f"{host} (stored)"
        else:
            init_custom = existing_entry.target_value

    # Initial zone selection
    init_zone_id: str | None = None
    if existing_entry:
        init_zone_id = existing_entry.zone_id
    elif zones:
        init_zone_id = zones[0].id

    # ---- tailscale source IP availability ---------------------------------
    ts_ip_available = proxy_service.get_tailscale_ip() is not None

    # ---- source IP options ------------------------------------------------
    source_ip_opts: dict[SourceIPType, str] = {SourceIPType.PUBLIC: _SOURCE_IP_LABELS[SourceIPType.PUBLIC]}
    if ts_ip_available:
        source_ip_opts[SourceIPType.TAILSCALE] = _SOURCE_IP_LABELS[SourceIPType.TAILSCALE]

    # Clamp init_source_ip to available options
    if init_source_ip not in source_ip_opts:
        init_source_ip = SourceIPType.PUBLIC

    # ---- build form -------------------------------------------------------
    with content, ui.card().classes("w-full p-6 gap-4"):
        # -- zone selector --------------------------------------------------
        ui.label("Cloudflare Zone").classes("text-subtitle2 text-weight-bold")
        selected_zone_id: str | None = init_zone_id

        if zones_failed:
            ui.label("⚠ Could not load zones — saving will be disabled.").classes("text-negative text-sm")
        elif not zones:
            ui.label("⚠ No Cloudflare zones found — check CF_API_TOKEN.").classes("text-negative text-sm")
        elif len(zones) == 1:
            ui.label(zones[0].name).classes("text-body1")
            selected_zone_id = zones[0].id
        else:
            zone_options = {z.id: z.name for z in zones}

            def _on_zone_change(e: Any) -> None:  # noqa: ANN401
                nonlocal selected_zone_id
                selected_zone_id = e.value

            ui.select(
                options=zone_options,
                label="Zone",
                value=init_zone_id,
                on_change=_on_zone_change,
            ).classes("w-full")

        zone_err = ui.label("").classes("text-negative text-sm").set_visibility(False)

        ui.separator()

        # -- domain section -------------------------------------------------
        ui.label("Domain").classes("text-subtitle2 text-weight-bold")
        domain_input: ui.input | None = None
        domain_err = ui.label("").classes("text-negative text-sm").set_visibility(False)

        if is_edit and existing_entry:
            # Read-only in edit mode — domain changes are not supported in v1
            ui.label(existing_entry.domain).classes("text-body1 font-mono")
            ui.label("Domain cannot be changed after creation.").classes("text-grey text-sm")
        else:
            domain_input = ui.input(
                label="Domain (e.g. app.example.com)",
                placeholder="app.example.com",
            ).classes("w-full")

            # Jump-to-existing helper
            if existing_entries:
                existing_options: dict[str, str] = {str(e.id): e.domain for e in existing_entries}

                def _on_existing_pick(e: Any) -> None:  # noqa: ANN401
                    if e.value:
                        ui.navigate.to(f"/entry/{e.value}")

                with ui.row().classes("items-center gap-2 mt-1"):
                    ui.label("Or jump to existing:").classes("text-grey text-sm")
                    ui.select(
                        options=existing_options,
                        label="",
                        value=None,
                        on_change=_on_existing_pick,
                        clearable=True,
                    ).classes("flex-1")

        ui.separator()

        # -- target type radio ----------------------------------------------
        ui.label("Target Type").classes("text-subtitle2 text-weight-bold")
        target_type_radio = ui.radio(
            options={t: _TARGET_LABELS[t] for t in TargetType},
            value=init_target_type,
        )

        # -- docker sub-fields ----------------------------------------------
        docker_fields = ui.column().classes("w-full gap-2 pl-4 border-l-2 border-teal-300")
        with docker_fields:
            ui.label("Container").classes("text-caption text-grey-8")
            container_names = list(docker_by_container.keys())
            init_cname = (
                init_container_name
                if init_container_name in container_names
                else (container_names[0] if container_names else "")
            )
            if not container_names:
                ui.label(
                    "No running Docker containers found."
                    if not targets_failed
                    else "Docker unavailable — check socket mount."
                ).classes("text-grey text-sm")
                container_select: ui.select | None = None
                port_select: ui.select | None = None
            else:
                container_select = ui.select(
                    options=container_names,
                    label="Container",
                    value=init_cname or container_names[0],
                ).classes("w-full")

                init_ports = docker_by_container.get(container_select.value, [])
                init_cport = (
                    init_container_port if init_container_port in init_ports else (init_ports[0] if init_ports else "")
                )
                port_select = ui.select(
                    options=init_ports,
                    label="Port",
                    value=init_cport,
                ).classes("w-full")

                def _on_container_change(_e: Any = None) -> None:  # noqa: ANN401
                    """Update port dropdown when container selection changes."""
                    assert container_select is not None  # noqa: S101
                    assert port_select is not None  # noqa: S101
                    ports = docker_by_container.get(container_select.value or "", [])
                    port_select.set_options(ports, value=ports[0] if ports else None)

                container_select.on_change(_on_container_change)

            container_err = ui.label("").classes("text-negative text-sm").set_visibility(False)
            port_err = ui.label("").classes("text-negative text-sm").set_visibility(False)

        # -- tailscale sub-fields -------------------------------------------
        ts_fields = ui.column().classes("w-full gap-2 pl-4 border-l-2 border-green-300")
        with ts_fields:
            ui.label("Tailscale Device").classes("text-caption text-grey-8")
            if not ts_options:
                ui.label(
                    "No Tailscale devices found."
                    if not targets_failed
                    else "Tailscale unavailable — check TS_API_KEY / TS_TAILNET."
                ).classes("text-grey text-sm")
                ts_device_select: ui.select | None = None
            else:
                init_ts = init_ts_hostname if init_ts_hostname in ts_options else None
                ts_device_select = ui.select(
                    options=ts_options,
                    label="Device",
                    value=init_ts,
                ).classes("w-full")

            ts_port_input = ui.input(
                label="Port",
                value=init_ts_port or "443",
                placeholder="443",
            ).classes("w-full")
            ts_device_err = ui.label("").classes("text-negative text-sm").set_visibility(False)
            ts_port_err = ui.label("").classes("text-negative text-sm").set_visibility(False)

        # -- custom sub-fields ----------------------------------------------
        custom_fields = ui.column().classes("w-full gap-2 pl-4 border-l-2 border-grey-400")
        with custom_fields:
            ui.label("Host:Port").classes("text-caption text-grey-8")
            custom_input = ui.input(
                label="Host:Port (e.g. 192.168.1.10:8080)",
                value=init_custom,
                placeholder="192.168.1.10:8080",
            ).classes("w-full")
            custom_err = ui.label("").classes("text-negative text-sm").set_visibility(False)

        # Wire target type visibility
        def _update_target_visibility() -> None:
            tt = target_type_radio.value
            docker_fields.set_visibility(tt == TargetType.DOCKER)
            ts_fields.set_visibility(tt == TargetType.TAILSCALE)
            custom_fields.set_visibility(tt == TargetType.CUSTOM)

        target_type_radio.on_change(lambda: _update_target_visibility())
        _update_target_visibility()

        ui.separator()

        # -- source IP radio ------------------------------------------------
        ui.label("Source IP").classes("text-subtitle2 text-weight-bold")
        source_ip_radio = ui.radio(
            options=source_ip_opts,
            value=init_source_ip,
        )
        if not ts_ip_available:
            ui.label("Tailscale source IP not available — set TS_HOST_NAME env var to enable.").classes(
                "text-grey text-sm"
            )

        ui.separator()

        # -- SSL radio (dynamic) --------------------------------------------
        ui.label("SSL Method").classes("text-subtitle2 text-weight-bold")
        ssl_note = ui.label("").classes("text-grey text-sm").set_visibility(False)

        # Clamp init_ssl to methods allowed for init_source_ip
        allowed_ssl = proxy_service.get_available_ssl_methods(init_source_ip)
        if init_ssl not in allowed_ssl:
            init_ssl = SSLMethod.NONE

        ssl_radio = ui.radio(
            options=_ssl_options(init_source_ip),
            value=init_ssl,
        )
        ssl_err = ui.label("").classes("text-negative text-sm").set_visibility(False)

        def _on_source_ip_change() -> None:
            """Update SSL options and show/hide HTTP-01 note."""
            sip: SourceIPType = source_ip_radio.value
            opts = _ssl_options(sip)
            current_ssl: SSLMethod = ssl_radio.value
            new_ssl = current_ssl if current_ssl in opts else SSLMethod.NONE
            ssl_radio.set_options(opts, value=new_ssl)
            if sip == SourceIPType.TAILSCALE and SSLMethod.HTTP01 not in opts:
                ssl_note.text = "HTTP-01 is not available for Tailscale source IP (requires public port 80)."
                ssl_note.set_visibility(True)
            else:
                ssl_note.set_visibility(False)

        source_ip_radio.on_change(lambda: _on_source_ip_change())

        ui.separator()

        # -- notes ----------------------------------------------------------
        ui.label("Notes (optional)").classes("text-subtitle2 text-weight-bold")
        notes_input = (
            ui.textarea(
                label="Notes",
                value=init_notes,
            )
            .classes("w-full")
            .props("rows=2")
        )

        ui.separator()

        # ---- error / DomainExistsError display ----------------------------
        domain_exists_card = ui.column().classes("w-full").set_visibility(False)

        # ---- action buttons -----------------------------------------------
        zones_ok = not zones_failed and bool(zones)
        save_btn = ui.button(
            "Save Entry",
            icon="save",
        ).props("color=primary")
        if not zones_ok:
            save_btn.disable()
            with save_btn:
                ui.tooltip("Cannot save — no Cloudflare zones available.")

        ui.button(
            "Cancel",
            icon="cancel",
            on_click=lambda: ui.navigate.to("/"),
        ).props("flat")

        # ---- validation helpers -------------------------------------------

        # Collect all error label references by field key for easy clearing
        _error_labels: dict[str, ui.label] = {
            "zone": zone_err,
            "domain": domain_err,
            "container": container_err,
            "container_port": port_err,
            "ts_device": ts_device_err,
            "ts_port": ts_port_err,
            "custom": custom_err,
            "ssl": ssl_err,
        }

        def _clear_errors() -> None:
            for lbl in _error_labels.values():
                lbl.text = ""
                lbl.set_visibility(False)
            domain_exists_card.clear()
            domain_exists_card.set_visibility(False)

        def _show_errors(errors: list[tuple[str, str]]) -> None:
            _clear_errors()
            for field, msg in errors:
                if field in _error_labels:
                    _error_labels[field].text = msg
                    _error_labels[field].set_visibility(True)

        def _show_domain_exists_warning(domain: str, existing_id: uuid.UUID) -> None:
            domain_exists_card.clear()
            with domain_exists_card, ui.card().classes("w-full bg-orange-50 border border-orange-300 p-4"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("warning", color="orange")
                    ui.label(f"Domain '{domain}' already has a proxy entry.").classes("text-weight-bold")
                with ui.row().classes("gap-2 mt-2"):
                    ui.button(
                        "Edit existing entry",
                        icon="edit",
                        on_click=lambda eid=existing_id: ui.navigate.to(f"/entry/{eid}"),
                    ).props("color=warning")
                    ui.button(
                        "Cancel",
                        on_click=lambda: ui.navigate.to("/"),
                    ).props("flat")
            domain_exists_card.set_visibility(True)

        def _validate() -> list[tuple[str, str]]:
            """Run client-side validation; returns (field_key, message) pairs."""
            errors: list[tuple[str, str]] = []
            tt: TargetType = target_type_radio.value
            sip: SourceIPType = source_ip_radio.value
            ssl: SSLMethod = ssl_radio.value

            if not is_edit:
                assert domain_input is not None  # noqa: S101
                d = domain_input.value.strip()
                if not d or "." not in d:
                    errors.append(("domain", "Enter a valid domain (e.g. app.example.com)"))

            if selected_zone_id is None:
                errors.append(("zone", "Select a Cloudflare zone"))

            if tt == TargetType.DOCKER:
                cname = container_select.value if container_select else ""
                cport = port_select.value if port_select else ""
                if not cname:
                    errors.append(("container", "Select a container"))
                if not cport:
                    errors.append(("container_port", "Select a port"))

            elif tt == TargetType.TAILSCALE:
                ts_host = ts_device_select.value if ts_device_select else ""
                ts_p = ts_port_input.value.strip()
                if not ts_host:
                    errors.append(("ts_device", "Select a device"))
                if not ts_p or not ts_p.isdigit():
                    errors.append(("ts_port", "Port must be a number"))

            else:  # CUSTOM
                cv = custom_input.value.strip()
                if ":" not in cv:
                    errors.append(("custom", "Enter host:port (e.g. 192.168.1.10:8080)"))
                else:
                    _, _, p = cv.rpartition(":")
                    if not p.isdigit():
                        errors.append(("custom", "Enter host:port (e.g. 192.168.1.10:8080)"))

            # SSL compatibility (belt-and-suspenders; set_options should prevent this)
            if sip == SourceIPType.TAILSCALE and ssl == SSLMethod.HTTP01:
                errors.append(("ssl", "HTTP-01 requires Public source IP"))

            return errors

        # ---- submit handler -----------------------------------------------
        async def _on_save() -> None:
            _clear_errors()
            save_btn.props("loading")
            try:
                errors = _validate()
                if errors:
                    _show_errors(errors)
                    return

                tt: TargetType = target_type_radio.value
                cname = (container_select.value or "") if container_select else ""
                cport = (port_select.value or "") if port_select else ""
                ts_host = (ts_device_select.value or "") if ts_device_select else ""
                ts_p = ts_port_input.value
                cv = custom_input.value

                target_value = _compose_target_value(tt, cname, cport, ts_host, ts_p, cv)

                domain: str
                created_at: datetime
                if is_edit:
                    assert existing_entry is not None  # noqa: S101
                    domain = existing_entry.domain
                    created_at = existing_entry.created_at
                    entry_uuid = existing_entry.id
                else:
                    assert domain_input is not None  # noqa: S101
                    domain = domain_input.value.strip().lower()
                    created_at = datetime.now(UTC)
                    entry_uuid = uuid.uuid4()

                entry = ProxyEntry(
                    id=entry_uuid,
                    domain=domain,
                    zone_id=selected_zone_id or "",
                    target_type=tt,
                    target_value=target_value,
                    source_ip_type=source_ip_radio.value,
                    ssl_method=ssl_radio.value,
                    notes=notes_input.value.strip(),
                    created_at=created_at,
                )

                if is_edit:
                    await proxy_service.update_entry(entry)
                    ui.notify(f"Updated: {entry.domain}", type="positive")
                else:
                    await proxy_service.create_entry(entry)
                    ui.notify(f"Created: {entry.domain}", type="positive")

                ui.navigate.to("/")

            except DomainExistsError as exc:
                logger.warning(f"Domain exists conflict: {exc.domain}")
                _show_domain_exists_warning(exc.domain, exc.existing_id)
            except (CloudflareError, CaddyError, ValueError) as exc:
                logger.error(f"Save failed: {exc}")
                ui.notify(f"Error: {exc}", type="negative")
            except Exception as exc:
                logger.error(f"Unexpected save error: {exc}")
                ui.notify(f"Error: {exc}", type="negative")
            finally:
                save_btn.props(remove="loading")

        save_btn.on_click(_on_save)
