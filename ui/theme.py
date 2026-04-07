"""Central theme configuration for the Caddy Proxy Manager UI.

Applies the Quasar/NiceGUI colour palette, enables system-preference dark mode,
and injects custom CSS for elements that cannot be styled via Quasar's colour
system alone (card shadows, dark-mode-aware table rows, warning cards).

Call ``apply_theme()`` once at the top of every ``@ui.page`` handler before
any components are rendered.
"""

from __future__ import annotations

import logging

from nicegui import ui

logger = logging.getLogger(__name__)

# CSS injected into every page that calls apply_theme().
# Uses Quasar's .body--dark selector for dark-mode variants so styles adapt
# automatically when the user's OS preference changes.
_THEME_CSS = """
/* Header: indigo gradient (overrides Quasar's flat primary colour) */
.q-header {
    background: linear-gradient(135deg, #4338ca 0%, #6366f1 100%);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
}

/* Cards: subtle depth instead of Quasar's flat default */
.q-card {
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12), 0 2px 8px rgba(0, 0, 0, 0.08);
    border-radius: 12px;
}
.body--dark .q-card {
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4), 0 2px 8px rgba(0, 0, 0, 0.3);
}

/* Table header row — replaces hardcoded bg-grey-3 */
.proxy-table-header {
    background: rgba(99, 102, 241, 0.08);
    border-bottom: 1px solid rgba(99, 102, 241, 0.2);
}
.body--dark .proxy-table-header {
    background: rgba(99, 102, 241, 0.15);
    border-bottom: 1px solid rgba(99, 102, 241, 0.3);
}

/* Table row hover — replaces hardcoded hover:bg-grey-1 */
.proxy-table-row:hover {
    background: rgba(0, 0, 0, 0.04);
}
.body--dark .proxy-table-row:hover {
    background: rgba(255, 255, 255, 0.06);
}

/* Warning card — replaces hardcoded bg-orange-50 border-orange-300 */
.warning-card {
    background: rgba(245, 158, 11, 0.08);
    border: 1px solid rgba(245, 158, 11, 0.4);
    border-radius: 12px;
}
.body--dark .warning-card {
    background: rgba(245, 158, 11, 0.12);
    border: 1px solid rgba(245, 158, 11, 0.35);
}
"""


def apply_theme() -> None:
    """Apply the app-wide visual theme to the current page.

    Sets Quasar semantic colours, enables auto dark mode (follows OS
    preference), and injects custom CSS for dark-mode-aware components.
    Must be called before any UI components are created on the page.
    """
    ui.colors(
        primary="#6366f1",  # indigo-500
        secondary="#8b5cf6",  # violet-500
        accent="#ec4899",  # pink-500
        dark="#1e1b4b",  # indigo-950 — dark surface colour
        dark_page="#0f0e1a",  # near-black page background
        positive="#22c55e",  # green-500
        negative="#ef4444",  # red-500
        info="#3b82f6",  # blue-500
        warning="#f59e0b",  # amber-500
    )
    ui.dark_mode().auto()
    ui.add_css(_THEME_CSS)
