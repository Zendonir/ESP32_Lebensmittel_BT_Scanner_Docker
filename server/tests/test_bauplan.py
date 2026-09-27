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
import subprocess

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


# ------------------------------------------------------------ Release-Kette
#
# release.yml haengt an drei Stellen an anderen Dateien, die man beim
# Aufraeumen leicht veraendert, ohne es zu merken: am *Namen* des CI-Ablaufs
# (workflow_run findet ihn nur darueber), an den Eingaben der beiden
# aufgerufenen Ablaeufe, und an der Versionsnummer in der Firmware.


WURZEL = WORKFLOWS.parents[1]


def _plan(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))


def _ausloeser(plan: dict) -> dict:
    # PyYAML liest den Schluessel `on` als True.
    return plan.get("on") or plan.get(True) or {}


def test_release_wartet_auf_den_ci_ablauf_von_main():
    ausloeser = _ausloeser(_plan("release.yml"))["workflow_run"]
    assert ausloeser["workflows"] == [_plan("ci.yml")["name"]], (
        "release.yml wartet auf einen Ablauf, den es unter diesem Namen nicht gibt"
    )
    assert ausloeser["branches"] == ["main"]
    bedingung = _plan("release.yml")["jobs"]["version"]["if"]
    assert "conclusion == 'success'" in bedingung, "Release nur aus gruenem Stand"


def test_aufgerufene_ablaeufe_passen_zu_ihren_eingaben():
    jobs = _plan("release.yml")["jobs"]
    for job in ("abbild", "firmware"):
        ziel = jobs[job]["uses"].rsplit("/", 1)[-1]
        erwartet = set(_ausloeser(_plan(ziel))["workflow_call"]["inputs"])
        assert set(jobs[job]["with"]) == erwartet, f"{job} -> {ziel}"


def test_firmware_traegt_den_namen_des_releases():
    """Sonst sieht der Server ein Terminal mit der neuesten Firmware nie als
    aktuell an, und "Update" steht fuer immer bereit."""
    bau = next(s for _, s in _schritte() if s.get("name") == "Firmware bauen")
    assert "FIRMWARE_VERSION" in _umgebung(bau)
    assert 'os.environ.get("FIRMWARE_VERSION"' in (
        WURZEL / "firmware" / "scripts" / "version.py"
    ).read_text("utf-8")


@pytest.mark.parametrize(
    "tags,stufe,erwartet",
    [
        ([], "patch", "v1.0.0"),
        (["v1.0.0"], "patch", "v1.0.1"),
        (["v1.0.9", "v1.0.10"], "patch", "v1.0.11"),   # Zahl, nicht Text
        (["v1.2.3", "vquatsch", "v9.9.9-rc1"], "patch", "v1.2.4"),
        (["v1.2.3"], "minor", "v1.3.0"),
        (["v1.2.3"], "major", "v2.0.0"),
    ],
)
def test_naechste_versionsnummer(tmp_path, tags, stufe, erwartet):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tmp_path)]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    for tag in tags:
        subprocess.run([*git, "tag", tag], check=True)
    skript = WURZEL / ".github" / "scripts" / "naechste-version.sh"
    aus = subprocess.run(["bash", str(skript), stufe], cwd=tmp_path,
                         capture_output=True, text=True, check=True)
    assert aus.stdout.strip() == erwartet
