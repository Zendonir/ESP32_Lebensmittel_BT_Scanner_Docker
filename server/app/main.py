"""Einstiegspunkt des Servers.

Der Container ist der zentrale Punkt: Datenhaltung, Ablauflogik, Web-Interface
und Geraetekopplung liegen alle hier. Das ESP32 ist ab jetzt ein Terminal.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .api import catalog, firmware, haushalte as haushalte_api, inventory, labels, system
from .config import settings
from .db import init_db, session_scope
from .device import routes as device_routes
from .services import anmeldung, haushalte, scheduler, seed, updates

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


def _seite(datei: str, medientyp: str = "text/html; charset=utf-8",
           haushalt: tuple[int, str] | None = None) -> Response:
    """Eine Seite ausliefern und ihre Dateiadressen mit der Kennung versehen.

    Die Seite selbst wird nie zwischengespeichert - sie ist der Einstieg, und
    aus ihr erfaehrt der Browser die neuen Adressen.

    Mit `haushalt` liegt sie unter /h/<id>/: Verweise auf die Handy- und die
    Desktop-Seite und das Manifest bleiben in diesem Haushalt. Als Web-App
    gespeichert startet sie deshalb immer dort.
    """
    text = (WEB_DIR / datei).read_text("utf-8")
    text = text.replace("/static/", f"{ASSET_PREFIX}{_asset_version()}/")
    if anmeldung.aktiv() and datei.endswith(".html"):
        # Die Oberflaeche zeigt dann "Abmelden".
        text = text.replace("<head>", '<head>\n  <meta name="anmeldung" content="1">', 1)
    if haushalt:
        hid, name = haushalt
        basis = f"/h/{hid}"
        for alt, neu in (('href="/manifest.json"', f'href="{basis}/manifest.json"'),
                         ("location.replace('/mobile')", f"location.replace('{basis}/mobile')"),
                         ('href="/mobile"', f'href="{basis}/mobile"'),
                         ('href="/?desktop=1"', f'href="{basis}/?desktop=1"')):
            text = text.replace(alt, neu)
        sicher = html.escape(name, quote=True)
        text = text.replace(
            "<head>", f'<head>\n  <meta name="haushalt" content="{hid}" data-name="{sicher}">', 1)
        text = re.sub(r"<title>(.*?)</title>", lambda m: f"<title>{sicher} · {m.group(1)}</title>",
                      text, count=1)
    return Response(
        text,
        media_type=medientyp,
        headers={"Cache-Control": "no-cache"},
    )


def _auswahl(liste) -> Response:
    """Startseite bei mehreren Haushalten: einen waehlen."""
    knoepfe = "".join(
        f'<a class="haus" href="/h/{h.id}/">{html.escape(h.name)}</a>' for h in liste)
    seite = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Haushalt wählen · Lebensmittel-Scanner</title>
<link rel="stylesheet" href="{ASSET_PREFIX}{_asset_version()}/css/app.css">
<style>
 body{{display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
 .wahl{{max-width:420px;width:100%;padding:24px}}
 .haus{{display:block;padding:18px 20px;margin:10px 0;border-radius:14px;font-size:20px;
   text-decoration:none;background:var(--surface);color:var(--text);border:1px solid var(--border);box-shadow:var(--shadow)}}
 .haus:hover{{border-color:var(--primary)}}
 .alle{{display:block;margin-top:18px;text-align:center;opacity:.7}}
</style></head><body><div class="wahl">
<h1 style="margin:0 0 6px">Welcher Haushalt?</h1>
<p class="muted" style="margin:0 0 14px">Danach als Web-App speichern – sie startet dann
immer direkt in diesem Haushalt.</p>
{knoepfe}
<a class="alle" href="/?alle=1">Verwaltung – alle Haushalte</a>
</div></body></html>"""
    return Response(seite, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with session_scope() as session:
        await seed.run(session)
        await haushalte.einrichten(session)
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
# Optionaler Passwortschutz fuer das Web-Interface (UI_PASSWORD).
# Die Geraeteverbindung authentifiziert sich getrennt ueber DEVICE_TOKEN, damit
# ein UI-Passwortwechsel nicht alle Terminals aussperrt.
#
# Angemeldet wird ueber die Seite /login; das Cookie haelt ein Jahr (siehe
# services/anmeldung.py). Die API antwortet ohne Anmeldung mit 401 *ohne*
# WWW-Authenticate - sonst oeffnete der Browser wieder sein eigenes Fenster
# statt der Login-Seite. Seiten leiten auf /login um.
# --------------------------------------------------------------------------
async def require_ui_auth(request: Request):
    if not anmeldung.angemeldet(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Anmeldung erforderlich")


async def require_ui_page(request: Request):
    if not anmeldung.angemeldet(request):
        ziel = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(status.HTTP_303_SEE_OTHER, "Anmeldung erforderlich",
                            headers={"Location": f"/login?weiter={quote(ziel, safe='')}"})


_guard = [Depends(require_ui_auth)]


def _login_seite(weiter: str, fehler: str = "", code: int = 200, benutzer: str = "") -> Response:
    """Die Anmeldeseite. Ein Benutzerfeld nur, wenn UI_USER gesetzt ist."""
    feld_user = (
        '<label class="feld"><span>Benutzer</span>'
        f'<input name="benutzer" value="{html.escape(benutzer, quote=True)}" '
        'autocomplete="username" autocapitalize="off" spellcheck="false" required '
        f'{"" if benutzer else "autofocus"}></label>'
    ) if settings.ui_user else ""
    fokus_pw = "" if settings.ui_user and not benutzer else "autofocus"
    meldung = f'<div class="fehler" role="alert">{html.escape(fehler)}</div>' if fehler else ""
    seite = f"""<!doctype html><html lang="de" data-theme=""><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#1e88e5">
<title>Anmelden · Lebensmittel-Scanner</title>
<link rel="manifest" href="/manifest.json">
<link rel="stylesheet" href="{ASSET_PREFIX}{_asset_version()}/css/app.css">
<script>try{{var t=localStorage.getItem('ls-theme');if(t&&t!=='auto')document.documentElement.dataset.theme=t;}}catch(e){{}}</script>
<style>
 html,body{{height:100%}}
 body{{margin:0;display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box;
   background:radial-gradient(1200px 600px at 50% -10%,color-mix(in srgb,var(--primary) 22%,transparent),transparent),var(--bg)}}
 .karte{{width:100%;max-width:380px;background:var(--surface);border:1px solid var(--border);
   border-radius:20px;box-shadow:var(--shadow);padding:32px 28px 26px}}
 .logo{{width:64px;height:64px;border-radius:18px;margin:0 auto 14px;display:flex;align-items:center;
   justify-content:center;font-size:34px;background:color-mix(in srgb,var(--primary) 16%,var(--surface))}}
 h1{{font-size:22px;margin:0;text-align:center}}
 .unter{{text-align:center;color:var(--muted);margin:4px 0 22px}}
 .feld{{display:block;margin:0 0 14px}}
 .feld span{{display:block;font-size:13px;color:var(--muted);margin:0 0 6px}}
 .feld input{{width:100%;box-sizing:border-box;font-size:17px;padding:12px 14px;border-radius:12px}}
 .pw{{position:relative}}
 .pw input{{padding-right:48px}}
 .zeigen{{position:absolute;right:6px;top:50%;transform:translateY(-50%);border:0;background:none;
   width:38px;height:38px;font-size:18px;cursor:pointer;color:var(--muted);padding:0}}
 .bleiben{{display:flex;gap:10px;align-items:center;margin:4px 0 20px;cursor:pointer;user-select:none}}
 .bleiben input{{width:18px;height:18px;margin:0}}
 .los{{width:100%;font-size:17px;padding:13px;border-radius:12px}}
 .fehler{{background:color-mix(in srgb,var(--danger) 14%,var(--surface));color:var(--danger);
   border:1px solid color-mix(in srgb,var(--danger) 40%,transparent);border-radius:12px;padding:10px 12px;
   margin:0 0 16px;font-size:14px;animation:wackeln .35s}}
 @keyframes wackeln{{20%,60%{{transform:translateX(-6px)}}40%,80%{{transform:translateX(6px)}}}}
 .fuss{{text-align:center;color:var(--muted);font-size:12px;margin-top:18px}}
</style></head><body>
<form class="karte" method="post" action="/login">
 <div class="logo" aria-hidden="true">🥫</div>
 <h1>Lebensmittel-Scanner</h1>
 <p class="unter">Bitte anmelden, um fortzufahren.</p>
 {meldung}
 <input type="hidden" name="weiter" value="{html.escape(weiter, quote=True)}">
 {feld_user}
 <label class="feld"><span>Passwort</span>
  <div class="pw"><input id="pw" type="password" name="passwort" autocomplete="current-password" required {fokus_pw}>
  <button class="zeigen" type="button" aria-label="Passwort anzeigen"
   onclick="var f=document.getElementById('pw');f.type=f.type==='password'?'text':'password';f.focus()">👁</button></div>
 </label>
 <label class="bleiben"><input type="checkbox" name="bleiben" value="1" checked> Angemeldet bleiben</label>
 <button class="primary los" type="submit">Anmelden</button>
 <div class="fuss">Die Anmeldung gilt auf diesem Gerät ein Jahr.</div>
</form></body></html>"""
    return Response(seite, status_code=code, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
async def login_form(request: Request, weiter: str = "/"):
    weiter = anmeldung.sicheres_ziel(weiter)
    if anmeldung.angemeldet(request):
        return RedirectResponse(weiter, status_code=303)
    return _login_seite(weiter)


@app.post("/login", include_in_schema=False)
async def login(request: Request):
    felder = parse_qs((await request.body()).decode("utf-8", "replace"))
    wert = lambda name: (felder.get(name) or [""])[0]  # noqa: E731
    weiter = anmeldung.sicheres_ziel(wert("weiter"))
    if not anmeldung.aktiv():
        return RedirectResponse(weiter, status_code=303)

    wer = anmeldung.adresse(request)
    if anmeldung.gesperrt(wer):
        log.warning("Anmeldung gesperrt fuer %s (zu viele Fehlversuche)", wer)
        return _login_seite(weiter, "Zu viele Fehlversuche – bitte in 15 Minuten erneut.", 429,
                            benutzer=wert("benutzer"))
    if not anmeldung.zugangsdaten_stimmen(wert("benutzer"), wert("passwort")):
        anmeldung.fehlversuch(wer)
        log.warning("Fehlgeschlagene Anmeldung von %s", wer)
        text = "Benutzer oder Passwort falsch." if settings.ui_user else "Passwort falsch."
        return _login_seite(weiter, text, 401, benutzer=wert("benutzer"))

    anmeldung.erfolg(wer)
    bleiben = wert("bleiben") == "1"
    antwort = RedirectResponse(weiter, status_code=303)
    antwort.set_cookie(
        anmeldung.COOKIE,
        anmeldung.cookie_wert(anmeldung.BLEIBEN_S if bleiben else anmeldung.KURZ_S),
        max_age=anmeldung.BLEIBEN_S if bleiben else None,
        httponly=True,
        samesite="lax",
        secure=anmeldung.per_https(request),
        path="/",
    )
    return antwort


@app.post("/logout", include_in_schema=False)
@app.get("/logout", include_in_schema=False)
async def logout():
    antwort = RedirectResponse("/login", status_code=303)
    antwort.delete_cookie(anmeldung.COOKIE, path="/")
    return antwort

app.include_router(inventory.router, dependencies=_guard)
app.include_router(catalog.router, dependencies=_guard)
app.include_router(labels.router, dependencies=_guard)
app.include_router(system.router, dependencies=_guard)
app.include_router(firmware.router, dependencies=_guard)
app.include_router(haushalte_api.router, dependencies=_guard)
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

    async def _haushalt_oder_404(hid: int) -> tuple[int, str]:
        from .models import Household

        async with session_scope() as session:
            row = await session.get(Household, hid)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Haushalt unbekannt")
        return row.id, row.name

    @app.get("/", include_in_schema=False)
    async def index(request: Request, _: None = Depends(require_ui_page)):
        # Mehrere Haushalte: erst waehlen. `?alle` ist die Verwaltung ueber
        # alle Haushalte hinweg (und der Weg zurueck fuer alte Lesezeichen).
        if "alle" not in request.query_params and "desktop" not in request.query_params:
            async with session_scope() as session:
                liste = await haushalte.alle(session)
            if len(liste) > 1:
                return _auswahl(liste)
        return _seite("index.html")

    @app.get("/h/{hid}", include_in_schema=False)
    @app.get("/h/{hid}/", include_in_schema=False)
    async def haushalt_index(hid: int, _: None = Depends(require_ui_page)):
        return _seite("index.html", haushalt=await _haushalt_oder_404(hid))

    @app.get("/h/{hid}/mobile", include_in_schema=False)
    async def haushalt_mobile(hid: int, _: None = Depends(require_ui_page)):
        return _seite("mobile.html", haushalt=await _haushalt_oder_404(hid))

    @app.get("/h/{hid}/manifest.json", include_in_schema=False)
    async def haushalt_manifest(hid: int):
        hid, name = await _haushalt_oder_404(hid)
        daten = json.loads((WEB_DIR / "manifest.json").read_text("utf-8"))
        daten.update({
            "id": f"/h/{hid}/",
            "name": f"{name} · Lebensmittel-Scanner",
            "short_name": name[:12],
            "start_url": f"/h/{hid}/mobile",
            "scope": f"/h/{hid}/",
        })
        return JSONResponse(daten, media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/mobile", include_in_schema=False)
    async def mobile(request: Request, _: None = Depends(require_ui_page)):
        if "alle" not in request.query_params:
            async with session_scope() as session:
                liste = await haushalte.alle(session)
            if len(liste) > 1:
                return _auswahl(liste)
        return _seite("mobile.html")

    @app.get("/manifest.json", include_in_schema=False)
    async def manifest():
        return _seite("manifest.json", "application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return _seite("sw.js", "application/javascript")
