"""Application entry point.

Wires FastAPI, NiceGUI, and the proxy service startup sequence together.

NiceGUI 3.x note: use ``app.on_startup`` from ``nicegui`` rather than
FastAPI's ``lifespan`` context manager.  The two have known conflicts when
using ``ui.run_with()``.  NiceGUI's hook runs after NiceGUI initialises but
before the server starts accepting requests — the correct place to run the
proxy service startup sequence.

The ``__mp_main__`` guard: NiceGUI uses multiprocessing in some reload modes
(e.g. ``reload=True``).  Checking for both ``__main__`` and ``__mp_main__``
ensures the server starts correctly in all modes.
"""

from __future__ import annotations

import importlib
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from nicegui import app as nicegui_app
from nicegui import ui

from core import proxy_service
from core.config import APP_PORT, configure_logging, resolve_app_secret, settings

# Register @ui.page routes defined in each page module as a side effect of importing.
# importlib.import_module is used instead of `import ui.form_page` to avoid binding
# the name `ui` to the local package, which would shadow NiceGUI's `ui` imported above.
importlib.import_module("ui.form_page")
importlib.import_module("ui.main_page")

logger = logging.getLogger(__name__)


@nicegui_app.on_startup
async def _startup() -> None:
    """Run the proxy service startup sequence before the server accepts requests.

    Errors are logged but do not abort startup — the UI will reflect the
    degraded state (e.g. Caddy unreachable) rather than refusing to start.
    """
    logger.info("Starting Caddy Proxy Manager")
    try:
        await proxy_service.startup()
    except Exception as exc:
        logger.error(f"Startup error (app continuing in degraded state): {exc}")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Called once at startup.  Configures logging first so all subsequent
    output uses the correct format and level.
    """
    configure_logging(settings.debug)
    fastapi_app = FastAPI(title="Caddy Proxy Manager")

    @fastapi_app.get("/health")
    async def health() -> JSONResponse:
        """Liveness probe — returns 200 once the server is accepting requests."""
        return JSONResponse({"status": "ok"})

    return fastapi_app


if __name__ in {"__main__", "__mp_main__"}:
    app = create_app()
    # run_with mounts NiceGUI onto FastAPI; port/title are uvicorn/server concerns.
    # storage_secret encrypts NiceGUI's browser session storage.
    # Resolved from APP_SECRET env var, persisted file, or auto-generated at first boot.
    ui.run_with(app, storage_secret=resolve_app_secret())
    # log_config=None prevents uvicorn from overriding our configure_logging() setup.
    uvicorn.run(app, host="0.0.0.0", port=APP_PORT, log_config=None)
