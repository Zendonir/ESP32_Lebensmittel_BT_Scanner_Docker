"""Wartung: Ablage und Ausgabe der OTA-Abbilder, Sicherung der Datenbank.

Beides haengt daran, dass Dateien den Container verlassen bzw. in ihm landen -
deshalb in einer Datei.

Wie die uebrigen Testdateien laeuft das im selben Prozess wie test_flow.py -
Settings ist ein Singleton, das beim ersten Modulimport fixiert wird. Das
Firmware-Verzeichnis wird deshalb hier gesetzt, bevor `app` importiert wird.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_fw.db")
os.environ.setdefault("FIRMWARE_DIR", f"{_tmpdir}/firmware")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import firmware as fw  # noqa: E402


def _image(size: int = fw.MIN_SIZE, magic: int = 0xE9) -> bytes:
    """Ein Abbild, das gerade eben als gueltig durchgeht."""
    return bytes([magic]) + b"\x00" * (size - 1)


def test_abbild_ablegen_und_wiederfinden():
    meta = fw.store("35", _image(), "v9.9.9", "test")

    assert meta["board"] == "35"
    assert meta["version"] == "v9.9.9"
    assert meta["size"] == fw.MIN_SIZE
    assert len(meta["sha256"]) == 64

    # Was abgelegt wurde, muss auch wieder auffindbar sein.
    assert fw.meta("35") == meta
    assert fw.path_for("35") is not None
    assert meta in fw.available()


def test_factory_abbild_wird_abgelehnt():
    """Die haeufigste Verwechslung: firmware.factory.bin statt firmware.bin.

    Die faengt mit dem Bootloader an, nicht mit der ESP32-Kennung 0xE9. Per OTA
    eingespielt ergaebe sie ein Geraet, das nicht mehr startet - der Fehler muss
    deshalb hier auffallen und nicht erst auf dem Terminal.
    """
    with pytest.raises(ValueError, match="kein ESP32-Anwendungsabbild"):
        fw.store("35", _image(magic=0x00), "kaputt", "test")


@pytest.mark.parametrize(
    "groesse", [fw.MIN_SIZE - 1, fw.MAX_SIZE + 1], ids=["zu-klein", "zu-gross"]
)
def test_unplausible_groesse_wird_abgelehnt(groesse):
    with pytest.raises(ValueError, match="Byte gross"):
        fw.store("35", _image(groesse), "kaputt", "test")


def test_unbekannte_boardvariante_wird_abgelehnt():
    with pytest.raises(ValueError, match="Unbekannte Boardvariante"):
        fw.store("42", _image(), "v1", "test")


def test_datenbanksicherung_ist_eine_lesbare_vollstaendige_datenbank(tmp_path):
    """Die Sicherung muss sich oeffnen lassen und alle Tabellen enthalten.

    Der eigentliche Grund fuer die Online-Sicherung statt eines Dateikopierens:
    die Datenbank laeuft im WAL-Modus. Frisch geschriebene Zeilen stehen also
    noch in der -wal-Datei. Deshalb wird hier unmittelbar vor dem Abruf etwas
    geschrieben - eine schlichte Kopie wuerde diese Zeile verlieren.
    """
    with TestClient(app) as client:
        client.post("/api/shopping", json={"name": "Pruefartikel Sicherung"})

        response = client.get("/api/backup/db")
        assert response.status_code == 200
        assert response.content[:16].startswith(b"SQLite format 3")

        ziel = tmp_path / "sicherung.db"
        ziel.write_bytes(response.content)

        con = sqlite3.connect(ziel)
        tabellen = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # Auch das, was der JSON-Export nicht mitnimmt.
        assert {"inventory", "events", "devices", "print_jobs"} <= tabellen

        namen = [
            r[0] for r in con.execute("SELECT name FROM shopping_list")
        ]
        assert "Pruefartikel Sicherung" in namen
        con.close()


def test_download_verlangt_das_geraetetoken():
    """Der Download haengt nicht hinter dem Web-Passwort, sondern am Token."""
    fw.store("35b", _image(), "v9.9.9", "test")

    with TestClient(app) as client:
        assert client.get("/firmware/35b.bin").status_code == 401
        assert client.get("/firmware/35b.bin?token=falsch").status_code == 401

        ok = client.get(f"/firmware/35b.bin?token={os.environ['DEVICE_TOKEN']}")
        assert ok.status_code == 200
        assert ok.content[0] == 0xE9

        # Eine Variante ohne hinterlegtes Abbild darf keinen Serverfehler geben.
        fehlt = client.get(f"/firmware/35x.bin?token={os.environ['DEVICE_TOKEN']}")
        assert fehlt.status_code == 404
