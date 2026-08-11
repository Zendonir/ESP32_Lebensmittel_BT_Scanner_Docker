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


# --------------------------------------------------------------- SD-Sicherung
import json  # noqa: E402
import zipfile  # noqa: E402
from io import BytesIO  # noqa: E402

SD_INVENTORY = [
    {
        "barcode": "5001",
        "name": "SD-Import Marmelade",
        "brand": "Zentis",
        "category": "Konserven",
        "subcategory": "",
        "expiryDate": "24.12.2026",
        "addedDate": "01.01.2026",
        "quantity": 1,
        "unit": "",
        "labelBarcode": "IM9SD0001",
        "location": "Keller",
    }
]

SD_REMOVED = [
    {
        "removedAt": 1735689600,  # 2025-01-01T00:00:00Z
        "barcode": "5002",
        "name": "SD-Import Verlauf",
        "brand": "",
        "category": "Sonstiges",
        "subcategory": "",
        "expiryDate": "01.02.2025",
        "addedDate": "01.01.2025",
        "quantity": 1,
        "unit": "",
        "labelBarcode": "IM9SD0002",
        "location": "Keller",
    }
]


def test_sd_inventory_json_wird_uebernommen(client):
    resp = _upload(
        client, json.dumps(SD_INVENTORY).encode("utf-8"), filename="inventory.json"
    )
    body = resp.json()
    assert body["imported"] == 1

    row = client.get("/api/inventory/IM9SD0001").json()
    assert row["name"] == "SD-Import Marmelade"
    assert row["expiry_date"] == "2026-12-24"
    assert row["status"] == "active"


def test_sd_removed_items_json_landet_als_ausgelagert(client):
    resp = _upload(
        client, json.dumps(SD_REMOVED).encode("utf-8"), filename="removed_items.json"
    )
    body = resp.json()
    assert body["imported"] == 1

    row = client.get("/api/inventory/IM9SD0002").json()
    assert row["status"] == "removed"
    assert row["removed_reason"] == "import"


def test_sd_json_wiederholung_ueberspringt_duplikat(client):
    resp = _upload(
        client, json.dumps(SD_INVENTORY).encode("utf-8"), filename="inventory.json"
    )
    assert resp.json()["skipped_duplicate"] == 1


def test_sd_zip_verarbeitet_beide_dateien(client):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "scanner_backup/inventory.json",
            json.dumps(
                [{**SD_INVENTORY[0], "labelBarcode": "IM9ZIP001", "barcode": "6001"}]
            ),
        )
        zf.writestr(
            "scanner_backup/removed_items.json",
            json.dumps(
                [{**SD_REMOVED[0], "labelBarcode": "IM9ZIP002", "barcode": "6002"}]
            ),
        )
        zf.writestr("scanner_backup/categories.json", "[]")  # wird ignoriert

    resp = _upload(client, buffer.getvalue(), filename="backup.zip")
    body = resp.json()
    assert body["imported"] == 2

    active = client.get("/api/inventory/IM9ZIP001").json()
    removed = client.get("/api/inventory/IM9ZIP002").json()
    assert active["status"] == "active"
    assert removed["status"] == "removed"


def test_zip_ohne_bekannte_dateien_meldet_fehler(client):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("scanner_backup/categories.json", "[]")
    resp = _upload(client, buffer.getvalue(), filename="leer.zip")
    body = resp.json()
    assert body["imported"] == 0
    assert body["errors"]
