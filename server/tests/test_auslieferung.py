"""Nach einem Update muss der Browser die neuen Dateien wirklich holen.

`Cache-Control: no-cache` allein genuegt dafuer nicht. Es wirkt nur auf
Antworten, die der Server ab jetzt schickt - wer die alte Datei schon im
Zwischenspeicher hat, fragt gar nicht erst nach. Genau diese Browser sind aber
die kaputten: sie haben das neue `index.html` und noch das alte `app.js`, und
das alte Skript bricht am entfernten Feld ab.

Deshalb steht die Kennung im Pfad. Dass sie dort steht und nicht als
Abfrageteil, ist kein Geschmack: `app.js` importiert `./api.js` relativ -
haengt man die Kennung nur an `app.js`, loest der Import weiterhin auf die
alte Adresse auf und `api.js` bleibt genauso haengen.
"""

from __future__ import annotations

import os
import re
import tempfile

import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/ausl.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import _asset_version, app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _adressen(html: str) -> list[str]:
    return sorted(set(re.findall(r"/static/[^\"'\s>]+", html)))


def test_seite_verweist_nur_auf_versionierte_adressen(client):
    adressen = _adressen(client.get("/").text)
    assert adressen, "index.html verweist auf gar keine Dateien"
    for adresse in adressen:
        assert adresse.startswith("/static/v/"), adresse


def test_relativer_import_traegt_die_kennung_mit(client):
    """Der Punkt, an dem ein Abfrageteil versagt haette.

    `app.js` importiert `./api.js`. Steht die Kennung im Pfad, loest der
    Import auf `/static/v/<kennung>/js/api.js` auf - beide Dateien kommen also
    frisch. Mit `?v=` waere nur `app.js` neu gewesen.
    """
    app_js = next(a for a in _adressen(client.get("/").text) if a.endswith("app.js"))
    api_js = app_js.rsplit("/", 1)[0] + "/api.js"

    assert client.get(app_js).status_code == 200
    assert client.get(api_js).status_code == 200
    kennung = app_js.split("/")[3]
    assert kennung in api_js


def test_versionierte_adresse_darf_fuer_immer_gelten(client):
    """Unter *dieser* Adresse aendert sich nichts mehr - die naechste
    Auslieferung hat eine andere."""
    app_js = next(a for a in _adressen(client.get("/").text) if a.endswith("app.js"))
    cc = client.get(app_js).headers["cache-control"]
    assert "immutable" in cc
    assert "max-age=31536000" in cc


def test_seite_selbst_wird_nie_zwischengespeichert(client):
    """Aus ihr erfaehrt der Browser die neuen Adressen - sie muss frisch sein."""
    for pfad in ("/", "/mobile", "/sw.js", "/manifest.json"):
        assert client.get(pfad).headers.get("cache-control") == "no-cache", pfad


def test_unversionierte_adresse_bleibt_erreichbar(client):
    """Alte Lesezeichen und der Service Worker sollen nichts verlieren -
    aber ohne Vorratshaltung."""
    antwort = client.get("/static/js/app.js")
    assert antwort.status_code == 200
    assert antwort.headers.get("cache-control") == "no-cache"


@pytest.mark.parametrize(
    "pfad",
    ["/static/v/x/../../app/main.py", "/static/v/x/../../../etc/passwd", "/static/v/x/nichtda.js"],
)
def test_kein_ausbruch_aus_dem_web_verzeichnis(client, pfad):
    assert client.get(pfad).status_code == 404


def test_kennung_wechselt_mit_der_auslieferung(monkeypatch):
    """Sonst faende der Browser nach einem Update dieselbe Adresse vor und
    haette keinen Grund, noch einmal zu fragen."""
    monkeypatch.setenv("APP_COMMIT", "a" * 40)
    erste = _asset_version()
    monkeypatch.setenv("APP_COMMIT", "b" * 40)
    assert _asset_version() != erste

    # Ohne Abbild-Angaben: aus den Dateien selbst, damit es beim Entwickeln
    # trotzdem durchkommt.
    monkeypatch.delenv("APP_COMMIT", raising=False)
    monkeypatch.delenv("APP_BUILT", raising=False)
    assert _asset_version().startswith("dev")
