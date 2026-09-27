"""Die Oberflaeche darf nur Einstellungen anbieten, die es gibt.

Jedes Feld mit data-setting="bereich.schluessel" speichert sich selbst per
PATCH. Der Server lehnt einen unbekannten *Bereich* ab, einen unbekannten
*Schluessel* darin aber nicht - er wuerde gespeichert und von niemandem
gelesen. Genau so standen `post_feed_dots`, `printer.qr` und
`printer.code128` jahrelang in der Oberflaeche, ohne etwas zu bewirken.
"""

from __future__ import annotations

import pathlib
import re

from app.services import settings_store

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"


def _felder() -> list[str]:
    html = (WEB / "index.html").read_text("utf-8")
    return re.findall(r'data-setting="([^"]+)"', html)


def test_es_gibt_einstellungsfelder():
    assert len(_felder()) >= 10


def test_jedes_feld_zeigt_auf_einen_bekannten_schluessel():
    unbekannt = []
    for feld in _felder():
        bereich, _, schluessel = feld.partition(".")
        vorgaben = settings_store.DEFAULTS.get(bereich)
        if not isinstance(vorgaben, dict) or schluessel not in vorgaben:
            unbekannt.append(feld)
    assert not unbekannt, f"Felder ohne Eintrag in settings_store.DEFAULTS: {unbekannt}"


def test_kein_feld_doppelt():
    """Zwei Felder fuer denselben Wert ueberschreiben sich gegenseitig."""
    felder = _felder()
    assert len(felder) == len(set(felder))


def test_systemauskunft_nennt_warnungen_ohne_geheimnisse():
    """Die Oberflaeche braucht nur ja/nein - nie den Wert selbst.

    /api/system ist ohne Web-Passwort fuer jeden im Netz abrufbar.
    """
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    with TestClient(app) as c:
        info = c.get("/api/system").json()
    for schluessel in ("token_standard", "update_knopf", "passwortschutz"):
        assert isinstance(info[schluessel], bool), schluessel
    text = str(info)
    assert settings.device_token not in text
