"""Der Firmware-Bau in der Werkstatt darf nicht am Netz haengen.

Kein Servertest im engeren Sinn, aus demselben Grund wie test_schriften.py:
es ist die einzige Stelle, an der bei jedem Lauf etwas geprueft wird.

Hintergrund. Die ESP32-Plattform richtet sich eine eigene Python-Umgebung ein
und prueft deren Inhalt bei *jeder* Bauumgebung neu. In ihrer Abhaengigkeits-
liste steht der PlatformIO-Kern unter dem Namen "platformio", installiert wird
er aber als "pioarduino-core" (nachzusehen in builder/penv_setup.py, Zeile 53
gegen die Verteilung im penv). Die beiden Namen treffen sich nie, also kommt
die Pruefung jedes Mal zu dem Schluss, das Paket fehle, und laedt sein Zip
erneut von GitHub. Bei zwei Bauumgebungen sind das drei Ladevorgaenge
desselben Archivs innerhalb weniger Minuten.

Einer davon geht frueher oder spaeter daneben. Dann bricht der Lauf mit
"Failed to install Python dependencies into penv" ab - mitten im Bauen,
nachdem die erste Firmware schon fertig gemeldet wurde. Das sieht aus wie ein
Fehler am Code und ist keiner; genau so ist der Lauf auf main nach dem Merge
von #5 gestorben, obwohl der Baum Zeichen fuer Zeichen derselbe war wie auf
dem Zweig, auf dem er gruen durchlief.

PLATFORMIO_OFFLINE=1 laesst die Pruefung aus. Was gebraucht wird, holt der
Schritt davor ("Toolchain bereitstellen"); der darf ans Netz und wiederholt
sich notfalls. Dieser Test haelt die Aufteilung fest, weil sie einem beim
Lesen der Datei nicht ins Auge springt und ein spaeteres Aufraeumen sie
leicht wieder einsammelt.
"""

from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _schritte():
    """Alle Schritte aller Werkstattablaeufe, mit Herkunftsangabe."""
    for datei in sorted(WORKFLOWS.glob("*.yml")):
        plan = yaml.safe_load(datei.read_text("utf-8"))
        for job_name, job in (plan.get("jobs") or {}).items():
            for schritt in job.get("steps") or []:
                yield f"{datei.name}:{job_name}", schritt


def _befehl(schritt) -> str:
    return schritt.get("run") or ""


def _umgebung(schritt) -> dict:
    return schritt.get("env") or {}


def test_bauschritte_bauen_ohne_netz():
    """Jedes `pio run` laeuft mit PLATFORMIO_OFFLINE."""
    gefunden = 0
    for herkunft, schritt in _schritte():
        if "pio run" not in _befehl(schritt):
            continue
        gefunden += 1
        offline = str(_umgebung(schritt).get("PLATFORMIO_OFFLINE", "")).lower()
        assert offline in ("1", "true", "yes"), (
            f"{herkunft}, Schritt {schritt.get('name')!r}: `pio run` ohne "
            "PLATFORMIO_OFFLINE. Die Plattform laedt ihren eigenen Kern dann "
            "einmal pro Bauumgebung neu und der Lauf faellt irgendwann am "
            "Netz um - siehe den Kopf dieser Datei."
        )
    assert gefunden >= 2, "Es sollte mindestens je einen Bauschritt in ci.yml und firmware-release.yml geben"


def test_toolchain_darf_ans_netz():
    """Der vorbereitende Schritt braucht das Netz - und eine Wiederholung."""
    gefunden = 0
    for herkunft, schritt in _schritte():
        befehl = _befehl(schritt)
        if "pio pkg install" not in befehl:
            continue
        gefunden += 1
        assert "PLATFORMIO_OFFLINE" not in _umgebung(schritt), (
            f"{herkunft}: `pio pkg install` ohne Netz kann nichts holen"
        )
        assert "versuch" in befehl, (
            f"{herkunft}: `pio pkg install` ist der einzige Schritt am Netz und "
            "braucht deshalb eine Wiederholung"
        )
    assert gefunden >= 2
