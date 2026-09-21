"""Welche Fassung laeuft hier - und ist sie die neueste?

Beides war vorher nicht zu beantworten: im System-Panel stand eine
Zeichenkette aus Zweigname und vollem Commit-Hash, und womit man sie haette
vergleichen sollen, stand nirgends. Dazu behauptete main.py parallel
hartcodiert "2.0" - zwei Versionsangaben, die sich widersprechen.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/version.db"

from app.services import updates  # noqa: E402


@pytest.fixture(autouse=True)
def _leerer_zwischenspeicher():
    updates._cache.clear()
    updates._cached_at = 0.0
    yield
    updates._cache.clear()
    updates._cached_at = 0.0


def test_fassung_wird_zerlegt_statt_als_hash_ausgegeben(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "main")
    monkeypatch.setenv("APP_COMMIT", "af39450d0d1e2f3a4b5c6d7e8f90112233445566")
    monkeypatch.setenv("APP_BUILT", "2026-09-21T09:00:00Z")

    stand = updates.current()
    assert stand["version"] == "main"
    assert stand["commit_kurz"] == "af39450"      # lesbar, nicht 40 Zeichen
    assert stand["gebaut"] == "2026-09-21T09:00:00Z"
    assert stand["aus_abbild"] is True


def test_selbstgebauter_stand_gibt_sich_als_solcher_zu_erkennen(monkeypatch):
    """Ohne APP_VERSION gibt es nichts zu vergleichen - das ist kein Fehler."""
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.delenv("APP_COMMIT", raising=False)

    assert updates.current()["aus_abbild"] is False


@pytest.mark.anyio
async def test_ohne_abbild_wird_nicht_beim_ursprung_nachgefragt(monkeypatch):
    """Sonst laeuft jede Entwicklungsinstanz in eine Fehlermeldung."""
    monkeypatch.delenv("APP_VERSION", raising=False)

    def _darf_nicht(*_args, **_kwargs):
        raise AssertionError("Es haette keine Abfrage geben duerfen")

    monkeypatch.setattr(updates.httpx, "AsyncClient", _darf_nicht)

    ergebnis = await updates.check()
    assert ergebnis["update_verfuegbar"] is False
    assert "APP_VERSION" in ergebnis["hinweis"]


@pytest.mark.parametrize(
    "version,ist_tag",
    [("v2.1.0", True), ("v2.0", True), ("main", False), ("claude/foo", False), ("dev", False)],
)
def test_tag_und_zweig_werden_unterschieden(version, ist_tag):
    """Ein Tag wird gegen das neueste Release verglichen, ein Zweig gegen
    dessen Spitze - die Abfrage ist eine voellig andere."""
    assert updates._ist_tag(version) is ist_tag


@pytest.mark.anyio
async def test_netzfehler_wird_gemeldet_statt_geworfen(monkeypatch):
    """Ein Server ohne Internet soll das Panel nicht mit einem 500 zerlegen."""
    import httpx

    monkeypatch.setenv("APP_VERSION", "v2.0.0")

    class KaputterClient:
        def __init__(self, *_a, **_k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def get(self, *_a, **_k):
            raise httpx.ConnectError("kein Netz")

    monkeypatch.setattr(updates.httpx, "AsyncClient", KaputterClient)

    ergebnis = await updates.check()
    assert "fehler" in ergebnis
    assert ergebnis["update_verfuegbar"] is False


class _Antwort:
    status_code = 200
    text = ""

    def __init__(self, nutzlast): self._nutzlast = nutzlast
    def json(self): return self._nutzlast
    def raise_for_status(self): return None


def _client_mit(nutzlast, monkeypatch):
    class Client:
        def __init__(self, *_a, **_k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def get(self, *_a, **_k): return _Antwort(nutzlast)

    monkeypatch.setattr(updates.httpx, "AsyncClient", Client)


@pytest.mark.anyio
async def test_rueckstand_zaehlt_in_die_richtige_richtung(monkeypatch):
    """`ahead_by`, nicht `behind_by`.

    Verglichen wird base = unser Commit gegen head = die Zweigspitze. Gesucht
    ist, um wie viele Commits die Spitze *voraus* ist. `behind_by` ist die
    Gegenrichtung und steht bei einem ganz gewoehnlichen Rueckstand auf 0 -
    die Pruefung meldete damit "aktuell", obwohl zwei Commits fehlten.
    """
    monkeypatch.setenv("APP_VERSION", "main")
    monkeypatch.setenv("APP_COMMIT", "a" * 40)
    _client_mit(
        {
            "ahead_by": 2,
            "behind_by": 0,
            "html_url": "https://github.com/x/y/compare/a...b",
            "commits": [
                {"sha": "b" * 40, "commit": {"message": "Erster\n\nRumpf",
                                             "committer": {"date": "2026-09-20T10:00:00Z"}}},
                {"sha": "c" * 40, "commit": {"message": "Zweiter\n\nRumpf",
                                             "committer": {"date": "2026-09-21T10:00:00Z"}}},
            ],
        },
        monkeypatch,
    )

    ergebnis = await updates.check(force=True)
    assert ergebnis["update_verfuegbar"] is True
    assert ergebnis["rueckstand"] == 2
    assert ergebnis["neueste"]["commit_kurz"] == "c" * 7
    assert ergebnis["neueste"]["titel"] == "Zweiter"       # nur die erste Zeile


@pytest.mark.anyio
async def test_gleichstand_hat_keinen_neuesten_commit(monkeypatch):
    """Bei Gleichstand ist die Commit-Liste leer.

    Eine leere Commit-Meldung hat keine erste Zeile - genau daran ist die
    Pruefung vorher gescheitert ("list index out of range"), und zwar im
    haeufigsten Fall ueberhaupt.
    """
    monkeypatch.setenv("APP_VERSION", "main")
    monkeypatch.setenv("APP_COMMIT", "a" * 40)
    _client_mit({"ahead_by": 0, "behind_by": 0, "commits": []}, monkeypatch)

    ergebnis = await updates.check(force=True)
    assert ergebnis["update_verfuegbar"] is False
    assert ergebnis["rueckstand"] == 0
    assert ergebnis["neueste"] is None
    assert "neuesten Stand" in ergebnis["hinweis"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_hinweis_sagt_wo_man_hinklickt(monkeypatch):
    """"Das Abbild neu ziehen" hilft niemandem vor der TrueNAS-Oberflaeche.

    Der Hinweis nennt jetzt die Schritte und das Abbild mit der neuen
    Versionsnummer - aus derselben Quelle wie die Pruefung, damit beides
    nicht auseinanderlaeuft.
    """
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    # Settings ist eingefroren - also eine geaenderte Kopie einsetzen.
    import dataclasses

    monkeypatch.setattr(
        updates, "settings",
        dataclasses.replace(
            updates.settings,
            app_image="ghcr.io/zendonir/esp32_lebensmittel_bt_scanner_docker:1.0.0",
        ),
    )
    _client_mit(
        {"tag_name": "v1.1.0", "published_at": "2026-10-01T08:00:00Z",
         "name": "Etiketten", "html_url": "https://github.com/x/y/releases/v1.1.0"},
        monkeypatch,
    )

    ergebnis = await updates.check(force=True)
    assert ergebnis["update_verfuegbar"] is True
    assert ergebnis["abbild"].endswith(":1.1.0")

    schritte = " ".join(ergebnis["anleitung"])
    assert "TrueNAS" in schritte
    assert "1.1.0" in schritte          # die Nummer, die einzutragen ist
    assert "/data" in schritte          # und dass die Daten bleiben


@pytest.mark.anyio
async def test_auf_latest_genuegt_speichern(monkeypatch):
    """Wer keinen Versionstag faehrt, muss nichts eintragen."""
    monkeypatch.setenv("APP_VERSION", "main")
    monkeypatch.setenv("APP_COMMIT", "a" * 40)
    _client_mit(
        {"ahead_by": 1, "behind_by": 0,
         "commits": [{"sha": "b" * 40,
                      "commit": {"message": "Neu", "committer": {"date": "2026-10-01T08:00:00Z"}}}]},
        monkeypatch,
    )

    ergebnis = await updates.check(force=True)
    schritte = " ".join(ergebnis["anleitung"])
    assert "Speichern" in schritte
    assert "Tag" not in schritte.split("Passiert nichts")[0]
