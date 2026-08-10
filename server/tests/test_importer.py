"""Import des Bestands aus Version 1 (current_inventory-CSV).

Läuft im selben Prozess wie test_flow.py; die Datenbank ist deshalb nicht
zwangsläufig leer (Settings ist ein Singleton, der beim ersten Modulimport
fixiert wird). Etikettennummern sind daher testfile-eindeutig gewählt
(Präfix IM9) statt Weltzustand vorauszusetzen.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_import.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

V1_CSV = (
    "label_barcode,barcode,name,brand,category,expiry_date,added_date,quantity,household\n"
    "IM9000001,4001,Kirschmarmelade,Zentis,Konserven,24.12.2026,01.01.2026,1,Standard\n"
    "IM9000002,,Restsuppe,,Sonstiges,2026-03-01,2026-01-05,2,Standard\n"
    ",,IM9-Ohne-Etikett,,Sonstiges,,,1,Standard\n"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _upload(client, content: bytes, filename: str = "export.csv", dry_run: bool = False):
    return client.post(
        f"/api/import/v1?dry_run={'true' if dry_run else 'false'}",
        files={"file": (filename, content, "text/csv")},
    )


def test_lehnt_falsche_dateiendung_ab(client):
    resp = _upload(client, b"irrelevant", filename="export.txt")
    assert resp.status_code == 400


def test_lehnt_unbekannte_spalten_ab(client):
    resp = _upload(client, b"foo,bar\n1,2\n")
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 0
    assert body["errors"]


def test_trockenlauf_schreibt_nichts(client):
    resp = _upload(client, V1_CSV.encode("utf-8"), dry_run=True)
    body = resp.json()
    assert body["dry_run"] is True
    assert body["imported"] == 3
    assert body["total_rows"] == 3

    assert client.get("/api/inventory/IM9000001").status_code == 404


def test_echter_import_uebernimmt_zeilen_und_normalisiert_datum(client):
    resp = _upload(client, V1_CSV.encode("utf-8"))
    body = resp.json()
    assert body["imported"] == 3
    assert body["skipped_duplicate"] == 0

    marmelade = client.get("/api/inventory/IM9000001").json()
    assert marmelade["expiry_date"] == "2026-12-24"
    assert marmelade["brand"] == "Zentis"

    rows = client.get("/api/inventory?status=all&q=IM9-Ohne-Etikett&limit=10").json()
    assert len(rows) == 1
    assert rows[0]["label"].startswith("IMPORT")


def test_wiederholter_import_ueberspringt_duplikate(client):
    resp = _upload(client, V1_CSV.encode("utf-8"))
    body = resp.json()
    assert body["skipped_duplicate"] == 3


def test_naechstes_etikett_kollidiert_nicht_mit_importierten_nummern(client):
    # Reguläre Etiketten benutzen weiterhin das konfigurierte Präfix (LEB);
    # der Zähler darf durch importierte IM9-Nummern nicht durcheinanderkommen.
    created = client.post(
        "/api/labels", json={"name": "Neu nach Import", "print": False}
    ).json()
    assert created["labels"][0].startswith("LEB")


def test_datum_varianten_werden_normalisiert(client):
    csv_body = (
        "label_barcode,name,expiry_date\n"
        "IM9900001,Test A,01.03.2027\n"
        "IM9900002,Test B,2027-03-02\n"
    ).encode("utf-8")
    _upload(client, csv_body)
    a = client.get("/api/inventory/IM9900001").json()
    b = client.get("/api/inventory/IM9900002").json()
    assert a["expiry_date"] == "2027-03-01"
    assert b["expiry_date"] == "2027-03-02"


def test_semikolon_getrennte_datei_wird_erkannt(client):
    csv_body = (
        "label_barcode;name;quantity\nIM9700001;Semikolon-Test;3\n"
    ).encode("utf-8")
    resp = _upload(client, csv_body)
    body = resp.json()
    assert body["imported"] == 1
    row = client.get("/api/inventory/IM9700001").json()
    assert row["quantity"] == 3
