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
from .services import scheduler, seed, updates

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("lebensmittel")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class _Oberflaeche(StaticFiles):
    """Statische Dateien, die der Browser nach einem Update wirklich neu holt.

    Ohne `Cache-Control` wendet ein Browser die sogenannte heuristische
    Frischedauer an: er behaelt die Datei einfach eine Weile, geschaetzt aus
    ihrem Alter. Nach einem Server-Update bekam man dadurch das neue
    `index.html` (das ist die Seite selbst), aber noch das alte `app.js` -
    und damit eine Oberflaeche, deren Beschriftungen zum Aufbau passen und
    deren Verhalten nicht.

    Das ist nicht theoretisch: genau so ist das entfernte Feld "Nachschub"
    zur Falle geworden. Das alte Skript griff darauf zu, fand es im neuen
    Aufbau nicht mehr, brach mitten im Fuellen der Einstellungen ab - und
    die halbe Etikettenmaske blieb leer, ohne dass irgendwo etwas von einem
    Fehler stand.

    `no-cache` heisst nicht "nicht zwischenspeichern", sondern "vor jeder
    Benutzung nachfragen". Mit dem ETag antwortet der Server dann meist mit
    einem leeren 304 - es kostet also eine Anfrage, keine Uebertragung.
    """

    def file_response(self, *args, **kwargs):
        antwort = super().file_response(*args, **kwargs)
        antwort.headers["Cache-Control"] = "no-cache"
        return antwort


def _seite(datei: str, medientyp: str | None = None) -> FileResponse:
    """Eine der beiden HTML-Seiten - aus demselben Grund ohne Vorratshaltung."""
    return FileResponse(
        WEB_DIR / datei,
        media_type=medientyp,
        headers={"Cache-Control": "no-cache"},
    )


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
    stand = updates.current()
    herkunft = " · ".join(
        teil for teil in (stand["commit_kurz"], stand["gebaut"]) if teil
    )
    log.info(
        "Server bereit auf %s:%s - Fassung %s%s",
        settings.host,
        settings.port,
        stand["version"],
        f" ({herkunft})" if herkunft else "",
    )
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(
    title="Lebensmittel-Scanner",
    description="Zentrale Datenverwaltung fuer ESP32-BLE-Scanner-Terminals",
    # Nicht noch einmal fest eintragen: hier stand "2.0", waehrend das
    # System-Panel APP_VERSION zeigte. Zwei Versionsangaben, die sich
    # widersprechen, sind schlimmer als eine ungenaue.
    version=updates.current()["version"],
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
    app.mount("/static", _Oberflaeche(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index(_: None = Depends(require_ui_auth)):
        return _seite("index.html")

    @app.get("/mobile", include_in_schema=False)
    async def mobile(_: None = Depends(require_ui_auth)):
        return _seite("mobile.html")

    @app.get("/manifest.json", include_in_schema=False)
    async def manifest():
        return _seite("manifest.json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return _seite("sw.js", "application/javascript")
