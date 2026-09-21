"""Umgebung fuer alle Tests - laeuft garantiert vor jedem Testmodul.

`Settings` ist ein eingefrorenes Singleton: es entsteht beim allerersten
Import von `app.config` und liest die Umgebung dabei genau einmal. Welche
Testdatei diesen Import ausloest, entschied bisher die alphabetische
Reihenfolge - und damit hing an einem Dateinamen, ob das Firmware-Verzeichnis
auf einen Temporaerpfad oder auf `/data` zeigte.

`test_firmware.py` setzte `FIRMWARE_DIR` selbst und hat das im Kopf auch
begruendet. Nur war die Voraussetzung dafuer, dass es die erste Datei ist, die
`app` importiert - eine Bedingung, die niemand sieht und die kein Test prueft.
Eine neue Testdatei, die sich davor einsortiert (hier: `test_etikettenmass`),
liess die Firmware-Tests mit `PermissionError: '/data'` scheitern, ohne dass an
ihnen etwas geaendert worden waere. Lokal fiel es nicht auf, weil ein Container
als root `/data` anlegen darf; in der CI laeuft der Lauf ohne Rechte dafuer.

pytest importiert `conftest.py` vor allen Testmodulen. Hier gesetzt, gilt es
fuer alle - unabhaengig von Namen und Reihenfolge. `setdefault`, damit ein
bewusst gesetzter Wert von aussen weiterhin gewinnt.
"""

from __future__ import annotations

import os
import tempfile

_TESTDIR = tempfile.mkdtemp(prefix="lebensmittel-tests-")

os.environ.setdefault("DEVICE_TOKEN", "test-token")
os.environ.setdefault("FIRMWARE_DIR", os.path.join(_TESTDIR, "firmware"))
