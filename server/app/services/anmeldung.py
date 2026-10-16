"""Anmeldung an der Web-Oberflaeche - mit Login-Seite und Cookie.

Vorher schuetzte HTTP Basic: der Browser zeigte sein eigenes Fenster, und
ob er sich das Passwort merkte, entschied er selbst - am Handy als Web-App
meist gar nicht, jeder Start fragte neu. Hinter einem Reverse Proxy, also
aus dem Netz erreichbar, soll es eine richtige Anmeldung sein, die bleibt.

Das Cookie ist kein Sitzungsschluessel aus einer Tabelle, sondern traegt
sein Ablaufdatum und eine Unterschrift (HMAC). Der Schluessel dafuer
leitet sich aus UI_PASSWORD und DEVICE_TOKEN ab: ein Neustart meldet
niemanden ab, ein neues Passwort dagegen alle - genau das, was man nach
einem Passwortwechsel will. Ohne die beiden Geheimnisse laesst sich kein
gueltiges Cookie bauen.

HTTP Basic gilt weiter, fuer Skripte (curl -u) und alte Lesezeichen.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time

from fastapi import Request

from ..config import settings

COOKIE = "lm_anmeldung"
# "Angemeldet bleiben": ein Jahr. Ohne den Haken gilt das Cookie, bis der
# Browser geschlossen wird - die Unterschrift laeuft trotzdem nach einem
# Tag ab, damit ein vergessener Rechner nicht ewig offen bleibt.
BLEIBEN_S = 365 * 24 * 3600
KURZ_S = 24 * 3600

# Gegen Durchprobieren: so viele Fehlversuche je Adresse in FENSTER_S,
# danach ist bis zum Ende des Fensters Schluss.
MAX_FEHLVERSUCHE = 5
FENSTER_S = 15 * 60
_fehlversuche: dict[str, list[float]] = {}


def aktiv() -> bool:
    return bool(settings.ui_password)


def _schluessel() -> bytes:
    return hashlib.sha256(
        f"lebensmittel-anmeldung\0{settings.ui_password}\0{settings.device_token}".encode()
    ).digest()


def _unterschrift(ablauf: int) -> str:
    return hmac.new(_schluessel(), str(ablauf).encode(), hashlib.sha256).hexdigest()


def cookie_wert(dauer_s: int) -> str:
    ablauf = int(time.time()) + dauer_s
    return f"{ablauf}.{_unterschrift(ablauf)}"


def cookie_gueltig(wert: str | None) -> bool:
    if not wert or "." not in wert:
        return False
    ablauf_text, sig = wert.split(".", 1)
    try:
        ablauf = int(ablauf_text)
    except ValueError:
        return False
    return ablauf > time.time() and hmac.compare_digest(sig, _unterschrift(ablauf))


def zugangsdaten_stimmen(benutzer: str, passwort: str) -> bool:
    ok_pw = secrets.compare_digest(passwort.encode(), settings.ui_password.encode())
    ok_user = (not settings.ui_user
               or secrets.compare_digest(benutzer.encode(), settings.ui_user.encode()))
    return ok_pw and ok_user


def _basic(request: Request) -> bool:
    kopf = request.headers.get("authorization", "")
    if not kopf.lower().startswith("basic "):
        return False
    try:
        benutzer, _, passwort = base64.b64decode(kopf[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return zugangsdaten_stimmen(benutzer, passwort)


def angemeldet(request: Request) -> bool:
    if not aktiv():
        return True
    return cookie_gueltig(request.cookies.get(COOKIE)) or _basic(request)


def adresse(request: Request) -> str:
    """Wer fragt - hinter dem Reverse Proxy steht das in X-Forwarded-For."""
    weiter = request.headers.get("x-forwarded-for", "")
    if weiter:
        return weiter.split(",")[0].strip()
    return request.client.host if request.client else "?"


def gesperrt(wer: str) -> bool:
    jetzt = time.monotonic()
    liste = [t for t in _fehlversuche.get(wer, []) if jetzt - t < FENSTER_S]
    _fehlversuche[wer] = liste
    return len(liste) >= MAX_FEHLVERSUCHE


def fehlversuch(wer: str) -> None:
    _fehlversuche.setdefault(wer, []).append(time.monotonic())


def erfolg(wer: str) -> None:
    _fehlversuche.pop(wer, None)


def per_https(request: Request) -> bool:
    """Ob der Browser per HTTPS kam - auch wenn der Proxy intern HTTP spricht."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


def sicheres_ziel(weiter: str | None) -> str:
    """Nur Pfade dieser Seite - kein Sprung auf fremde Adressen nach dem Login."""
    if not weiter or not weiter.startswith("/") or weiter.startswith("//") or "\\" in weiter:
        return "/"
    return weiter
