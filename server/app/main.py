"""Einstiegspunkt des Servers.

Der Container ist der zentrale Punkt: Datenhaltung, Ablauflogik, Web-Interface
und Geraetekopplung liegen alle hier. Das ESP32 ist ab jetzt ein Terminal.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
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

# Adressteil, der sich mit jeder Auslieferung aendert. Siehe _asset_version().
ASSET_PREFIX = "/static/v/"


def _asset_version() -> str:
    """Kennung der ausgelieferten Dateien - Teil ihrer Adresse.

    `Cache-Control: no-cache` allein genuegt nicht. Es wirkt nur auf
    Antworten, die der Server ab jetzt schickt - wer die alte Datei schon im
    Zwischenspeicher hat, fragt gar nicht erst nach. Genau diese Browser sind
    aber die kaputten: sie haben das neue `index.html` und noch das alte
    `app.js`. Sie blieben es, bis ihre geschaetzte Frischedauer ablaeuft, und
    die kann Stunden dauern.

    Die Kennung steht deshalb im *Pfad* und nicht als Abfrageteil. Das ist
    nicht Geschmack: `app.js` importiert `./api.js` relativ. Haenge man die
    Kennung nur an `app.js` an, loeste der Import weiterhin auf die alte,
    unversionierte Adresse auf - und `api.js` bliebe genauso alt haengen.
    Im Pfad wandert die Kennung mit: aus `/static/v/abc123/js/app.js` wird
    `/static/v/abc123/js/api.js`.

    Im Abbild ist es der Commit, beim Selberbauen der juengste Zeitstempel der
    Dateien - damit auch im Betrieb beim Entwickeln jede Aenderung durchkommt.
    """
    commit = os.getenv("APP_COMMIT", "")
    if commit:
        return commit[:12]
    gebaut = os.getenv("APP_BUILT", "")
    if gebaut:
        return "".join(c for c in gebaut if c.isalnum())[:14] or "dev"
    try:
        juengste = max(
            datei.stat().st_mtime
            for muster in ("js/*.js", "css/*.css")
            for datei in WEB_DIR.glob(muster)
        )
        return f"dev{int(juengste)}"
    except (OSError, ValueError):
        return "dev"


class _Oberflaeche(StaticFiles):
    """Statische Dateien unter ihrer unversionierten Adresse.

    Die bleibt bestehen, damit alte Lesezeichen und der Service Worker nichts
    verlieren. `no-cache` heisst nicht "nicht zwischenspeichern", sondern "vor
    jeder Benutzung nachfragen" - mit dem ETag antwortet der Server dann meist
    mit einem leeren 304. Es kostet eine Anfrage, keine Uebertragung.
    """

    def file_response(self, *args, **kwargs):
        antwort = super().file_response(*args, **kwargs)
        antwort.headers["Cache-Control"] = "no-cache"
        return antwort


def _seite(datei: str, medientyp: str = "text/html; charset=utf-8") -> Response:
    """Eine Seite ausliefern und ihre Dateiadressen mit der Kennung versehen.

    Die Seite selbst wird nie zwischengespeichert - sie ist der Einstieg, und
    aus ihr erfaehrt der Browser die neuen Adressen.
    """
    text = (WEB_DIR / datei).read_text("utf-8")
    text = text.replace("/static/", f"{ASSET_PREFIX}{_asset_version()}/")
    return Response(
        text,
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

    # Die versionierte Adresse muss *vor* der Einhaengung stehen: Starlette
    # nimmt die erste passende Route, und eine Einhaengung auf /static wuerde
    # /static/v/... schlucken und dort eine Datei "v/..." suchen.
    @app.get(ASSET_PREFIX + "{version}/{pfad:path}", include_in_schema=False)
    async def versionierte_datei(version: str, pfad: str):
        """Datei unter ihrer versionierten Adresse.

        Die Kennung im Pfad wird nicht geprueft - sie ist kein Schluessel,
        sondern nur dazu da, die Adresse zu veraendern. Wer eine alte Kennung
        anfragt, bekommt die heutige Datei; das ist richtig so, denn es gibt
        keine alten Dateien mehr.

        Dafuer darf sie unbegrenzt zwischengespeichert werden: unter *dieser*
        Adresse aendert sich nichts mehr. Genau das ist der Sinn der Sache -
        die naechste Auslieferung hat eine andere Adresse.
        """
        ziel = (WEB_DIR / pfad).resolve()
        if not str(ziel).startswith(str(WEB_DIR.resolve()) + os.sep) or not ziel.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Nicht gefunden")
        return FileResponse(
            ziel,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    app.mount("/static", _Oberflaeche(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index(_: None = Depends(require_ui_auth)):
        return _seite("index.html")

    @app.get("/mobile", include_in_schema=False)
    async def mobile(_: None = Depends(require_ui_auth)):
        return _seite("mobile.html")

    @app.get("/manifest.json", include_in_schema=False)
    async def manifest():
        return _seite("manifest.json", "application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return _seite("sw.js", "application/javascript")
