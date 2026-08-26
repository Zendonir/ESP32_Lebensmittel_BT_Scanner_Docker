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
    assert client.get("/api/inventory").status_code == 401
    assert client.get("/").status_code == 401


def test_mit_passwort_zugriff(client):
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
