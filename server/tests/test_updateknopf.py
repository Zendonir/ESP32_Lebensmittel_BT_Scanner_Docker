"""Der Update-Knopf: anstossen, ohne selbst Code auszufuehren.

Der Server laedt keine Quellen und fuehrt nichts aus, was er sich gerade aus
dem Netz geholt hat. Er schickt einem Dienst, der den Container ersetzen darf,
genau eine Anfrage. Diese Tests halten die Grenze fest.
"""

from __future__ import annotations

import os
import tempfile

import httpx
import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/update.db")

from app.services import deploy  # noqa: E402


def _mit(monkeypatch, **felder):
    """Settings ist eingefroren - also eine geaenderte Kopie einsetzen."""
    import dataclasses

    monkeypatch.setattr(deploy, "settings", dataclasses.replace(deploy.settings, **felder))


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_ohne_eingerichteten_dienst_gibt_es_keinen_knopf(monkeypatch):
    _mit(monkeypatch, update_hook_url="")
    zustand = deploy.status()
    assert zustand["moeglich"] is False
    assert "watchtower.yaml" in zustand["hinweis"]


@pytest.mark.anyio
async def test_ohne_dienst_wird_erklaert_statt_gescheitert(monkeypatch):
    _mit(monkeypatch, update_hook_url="")
    with pytest.raises(ValueError, match="UPDATE_HOOK_URL"):
        await deploy.trigger()


@pytest.mark.anyio
async def test_token_wird_mitgeschickt(monkeypatch):
    """Ohne Token laesst Watchtower niemanden den Container ersetzen."""
    gesehen: dict = {}

    class Client:
        def __init__(self, *_a, **_k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def post(self, url, headers=None):
            gesehen["url"] = url
            gesehen["headers"] = headers or {}
            # Mit Request, sonst kann raise_for_status() nicht arbeiten.
            return httpx.Response(200, request=httpx.Request("POST", url))

    _mit(monkeypatch, update_hook_url="http://wt:8080/v1/update", update_hook_token="geheim")
    monkeypatch.setattr(deploy.httpx, "AsyncClient", Client)

    ergebnis = await deploy.trigger()
    assert ergebnis["ok"] is True
    assert gesehen["url"] == "http://wt:8080/v1/update"
    assert gesehen["headers"]["Authorization"] == "Bearer geheim"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fehler",
    [
        httpx.TimeoutException("zu lange"),
        httpx.RemoteProtocolError("abgeschnitten"),
        httpx.ConnectError("weg"),
    ],
)
async def test_abgerissene_verbindung_ist_kein_fehler(monkeypatch, fehler):
    """Der Normalfall bei einem gelungenen Update.

    Watchtower haelt die Anfrage offen und beendet dabei genau den Container,
    der sie gestellt hat - die Antwort kann gar nicht ankommen. Das als Fehler
    zu melden waere die haeufigste falsche Fehlermeldung im ganzen Projekt:
    es hat funktioniert, und der Benutzer liest "fehlgeschlagen".
    """
    class Client:
        def __init__(self, *_a, **_k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def post(self, *_a, **_k): raise fehler

    _mit(monkeypatch, update_hook_url="http://wt:8080/v1/update")
    monkeypatch.setattr(deploy.httpx, "AsyncClient", Client)

    ergebnis = await deploy.trigger()
    assert ergebnis["ok"] is True
    assert "abgerissen" in ergebnis["hinweis"]


@pytest.mark.anyio
async def test_falsches_token_wird_benannt(monkeypatch):
    class Client:
        def __init__(self, *_a, **_k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def post(self, url, **_k):
            return httpx.Response(401, request=httpx.Request("POST", url))

    _mit(monkeypatch, update_hook_url="http://wt:8080/v1/update")
    monkeypatch.setattr(deploy.httpx, "AsyncClient", Client)

    with pytest.raises(ValueError, match="UPDATE_HOOK_TOKEN"):
        await deploy.trigger()


def test_die_adresse_kommt_nur_aus_der_umgebung():
    """Nicht aus settings_store.

    Waere sie zur Laufzeit setzbar, koennte wer Zugriff auf die Oberflaeche
    hat den Server auf einen beliebigen Dienst zeigen lassen.
    """
    from app.services.settings_store import DEFAULTS

    flach = str(DEFAULTS)
    assert "update_hook" not in flach
