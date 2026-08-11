"""Einstiegspunkt des Servers.

Der Container ist der zentrale Punkt: Datenhaltung, Ablauflogik, Web-Interface
und Geraetekopplung liegen alle hier. Das ESP32 ist ab jetzt ein Terminal.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .api import catalog, firmware, inventory, labels, system
from .config import settings
from .db import init_db, session_scope
from .device import routes as device_routes
from .services import scheduler, seed

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("lebensmittel")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with session_scope() as session:
        await seed.run(session)
    scheduler.start()
    if settings.device_token == "change-me":
        log.warning(
            "DEVICE_TOKEN steht auf dem Standardwert - bitte in der .env aendern!"
        )
    log.info("Server bereit auf %s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(
    title="Lebensmittel-Scanner",
    description="Zentrale Datenverwaltung fuer ESP32-BLE-Scanner-Terminals",
    version="2.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# --------------------------------------------------------------------------
# Optionaler Passwortschutz fuer das Web-Interface.
# Die Geraeteverbindung authentifiziert sich getrennt ueber DEVICE_TOKEN, damit
# ein UI-Passwortwechsel nicht alle Terminals aussperrt.
# --------------------------------------------------------------------------
_basic = HTTPBasic(auto_error=False)


async def require_ui_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)):
    if not settings.ui_password:
        return
    if credentials is None or not secrets.compare_digest(
        credentials.password, settings.ui_password
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Anmeldung erforderlich",
            headers={"WWW-Authenticate": "Basic"},
        )


_guard = [Depends(require_ui_auth)]

app.include_router(inventory.router, dependencies=_guard)
app.include_router(catalog.router, dependencies=_guard)
app.include_router(labels.router, dependencies=_guard)
app.include_router(system.router, dependencies=_guard)
app.include_router(firmware.router, dependencies=_guard)
# Beide ohne _guard - sie melden sich ueber DEVICE_TOKEN an, nicht ueber
# das Web-Passwort, das die Terminals gar nicht kennen.
app.include_router(firmware.public_router)
app.include_router(device_routes.router)  # WebSockets, eigene Authentifizierung


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Eine unerwartete Ausnahme darf den Dienst nie beenden."""
    log.exception("Unbehandelter Fehler bei %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Interner Serverfehler"}, status_code=500)


# --------------------------------------------------------------------------
# Web-Interface
# --------------------------------------------------------------------------
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index(_: None = Depends(require_ui_auth)):
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/mobile", include_in_schema=False)
    async def mobile(_: None = Depends(require_ui_auth)):
        return FileResponse(WEB_DIR / "mobile.html")

    @app.get("/manifest.json", include_in_schema=False)
    async def manifest():
        return FileResponse(WEB_DIR / "manifest.json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")
