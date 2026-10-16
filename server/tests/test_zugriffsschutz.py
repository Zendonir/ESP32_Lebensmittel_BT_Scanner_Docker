"""Was das Web-Passwort schuetzt - und was es vorher nicht schuetzte.

Der Live-Socket der Oberflaeche stand jedem offen, waehrend jede REST-Route
dahinter verschlossen war. Verraten haette er nur, *dass* sich etwas geaendert
hat und nicht was - aber "mit Passwort kommt keiner rein" soll ohne Fussnote
gelten.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile

import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PASSWORT = "geheim"


@pytest.fixture(scope="module")
def client():
    """Das Passwort zur Laufzeit setzen, nicht ueber die Umgebung.

    settings ist eingefroren und wird beim ersten Import gebaut. Welches
    Testmodul zuerst importiert, entscheidet sonst darueber, ob ein Passwort
    gilt - und diese Datei fiel je nach Reihenfolge um.
    """
    object.__setattr__(settings, "ui_password", PASSWORT)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        object.__setattr__(settings, "ui_password", "")


def test_settings_ist_eingefroren():
    """Damit die Umgehung oben auffaellt, falls sich das je aendert."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.ui_password = "x"  # type: ignore[misc]


def test_ohne_passwort_kein_zugriff(client):
    antwort = client.get("/api/inventory")
    assert antwort.status_code == 401
    # Kein WWW-Authenticate: sonst oeffnet der Browser sein eigenes Fenster
    # statt der Login-Seite.
    assert "www-authenticate" not in antwort.headers
    seite = client.get("/h/1/mobile?x=1", follow_redirects=False)
    assert seite.status_code == 303
    assert seite.headers["location"] == "/login?weiter=%2Fh%2F1%2Fmobile%3Fx%3D1"


def _neu(client):
    from app.services import anmeldung

    anmeldung._fehlversuche.clear()
    client.cookies.clear()


def test_login_setzt_ein_cookie_das_ein_jahr_haelt(client):
    from app.services import anmeldung

    _neu(client)
    antwort = client.post("/login", data={"passwort": PASSWORT, "bleiben": "1",
                                          "weiter": "/h/1/"}, follow_redirects=False)
    assert antwort.status_code == 303 and antwort.headers["location"] == "/h/1/"
    kopf = antwort.headers["set-cookie"]
    assert anmeldung.COOKIE in kopf and "HttpOnly" in kopf and "Max-Age=31536000" in kopf
    assert client.get("/api/inventory").status_code == 200
    # Abmelden loescht es wieder.
    client.get("/logout")
    assert client.get("/api/inventory").status_code == 401


def test_falsches_passwort_und_sperre_nach_fehlversuchen(client):
    from app.services import anmeldung

    _neu(client)
    for _ in range(anmeldung.MAX_FEHLVERSUCHE):
        assert client.post("/login", data={"passwort": "falsch"}).status_code == 401
    # Jetzt hilft auch das richtige nicht mehr - bis das Fenster vorbei ist.
    assert client.post("/login", data={"passwort": PASSWORT}).status_code == 429
    _neu(client)


def test_login_springt_nicht_auf_fremde_seiten(client):
    _neu(client)
    for boese in ("https://example.com/", "//example.com/", "/\\example.com"):
        antwort = client.post("/login", data={"passwort": PASSWORT, "weiter": boese},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/"
    _neu(client)


def test_neues_passwort_meldet_alle_ab(client):
    from app.services import anmeldung

    wert = anmeldung.cookie_wert(3600)
    assert anmeldung.cookie_gueltig(wert)
    object.__setattr__(settings, "ui_password", "anderes")
    try:
        assert not anmeldung.cookie_gueltig(wert)
    finally:
        object.__setattr__(settings, "ui_password", PASSWORT)
    assert not anmeldung.cookie_gueltig("9999999999.gefaelscht")
    assert not anmeldung.cookie_gueltig(f"{int(__import__('time').time()) - 5}.x")


def test_mit_passwort_zugriff(client):
    # HTTP Basic gilt weiter - fuer Skripte und curl.
    antwort = client.get("/api/inventory", auth=("egal", PASSWORT))
    assert antwort.status_code == 200


def test_socket_ohne_karte_wird_abgewiesen(client):
    """Der Kern der Sache: der Socket stand vorher offen."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as fehler:
        with client.websocket_connect("/ws/ui"):
            pass
    assert fehler.value.code == 4401


def test_socket_mit_karte_wird_angenommen(client):
    karte = client.post("/api/ws-ticket", auth=("egal", PASSWORT)).json()["ticket"]
    with client.websocket_connect(f"/ws/ui?ticket={karte}") as ws:
        assert ws is not None


def test_karte_gilt_nur_einmal(client):
    """Sonst waere sie ein zweites, laenger gueltiges Passwort."""
    from starlette.websockets import WebSocketDisconnect

    karte = client.post("/api/ws-ticket", auth=("egal", PASSWORT)).json()["ticket"]
    with client.websocket_connect(f"/ws/ui?ticket={karte}"):
        pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/ui?ticket={karte}"):
            pass


def test_karte_gibt_es_nur_mit_passwort(client):
    assert client.post("/api/ws-ticket").status_code == 401


def test_abgelaufene_karte_wird_abgewiesen(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from app.device import routes

    karte = client.post("/api/ws-ticket", auth=("egal", PASSWORT)).json()["ticket"]
    # Ablauf vorziehen, statt eine Minute zu warten.
    routes._TICKETS[karte] = 0.0
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/ui?ticket={karte}"):
            pass


def test_geraetesocket_braucht_das_geraetetoken(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as fehler:
        with client.websocket_connect("/ws/device?id=x&token=falsch"):
            pass
    assert fehler.value.code == 4401
