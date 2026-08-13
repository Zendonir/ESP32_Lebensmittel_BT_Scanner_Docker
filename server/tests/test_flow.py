"""Ende-zu-Ende-Test des kompletten Ablaufs ohne echte Hardware.

Deckt genau die Stellen ab, die im Vorgaengerprojekt wiederholt kaputt gingen:
Etikettenvergabe, Datumsnormalisierung, Auslagern per Scan, Zurueckbuchen und
die Druckwarteschlange.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import labels as labels_service  # noqa: E402
from app.services.dates import days_left, to_iso_date  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------- Datum
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-03-01", "2026-03-01"),
        ("01.03.2026", "2026-03-01"),
        ("1.3.2026", "2026-03-01"),
        ("01032026", "2026-03-01"),
        ("", ""),
        ("Unsinn", ""),
    ],
)
def test_datum_normalisierung(value, expected):
    assert to_iso_date(value) == expected


def test_tage_bis_mhd():
    assert days_left("") is None
    assert days_left("1999-01-01") < 0


# ----------------------------------------------------------------- Stammdaten
def test_erstbefuellung(client):
    categories = client.get("/api/categories").json()
    locations = client.get("/api/locations").json()
    assert len(categories) >= 5
    assert any(loc["is_default"] for loc in locations)


# ------------------------------------------------------------------- Etiketten
def test_etiketten_sind_fortlaufend_und_eindeutig(client):
    first = client.post(
        "/api/labels",
        json={"name": "Milch", "expiry_date": "24.12.2026", "count": 3, "print": False},
    )
    assert first.status_code == 201
    labels = first.json()["labels"]
    assert len(labels) == 3
    assert len(set(labels)) == 3

    second = client.post(
        "/api/labels", json={"name": "Butter", "count": 2, "print": False}
    ).json()["labels"]
    assert not set(labels) & set(second)


def test_datum_wird_beim_anlegen_normalisiert(client):
    label = client.post(
        "/api/labels",
        json={"name": "Joghurt", "expiry_date": "05.06.2027", "print": False},
    ).json()["labels"][0]
    item = client.get(f"/api/inventory/{label}").json()
    assert item["expiry_date"] == "2027-06-05"


def test_auslagern_und_zurueckbuchen(client):
    label = client.post(
        "/api/labels", json={"name": "Sahne", "print": False}
    ).json()["labels"][0]

    removed = client.post("/api/inventory/remove", json={"label": label})
    assert removed.status_code == 200
    assert removed.json()["status"] == "removed"

    # Zweites Auslagern desselben Etiketts muss scheitern - sonst zaehlt der
    # Bestand ins Negative.
    assert client.post("/api/inventory/remove", json={"label": label}).status_code == 404

    restored = client.post("/api/inventory/restore", json={"label": label})
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"


def test_auslagern_per_barcode_nimmt_aeltestes_mhd(client):
    client.post(
        "/api/labels",
        json={"name": "Saft", "barcode": "4001", "expiry_date": "2030-01-01", "print": False},
    )
    client.post(
        "/api/labels",
        json={"name": "Saft", "barcode": "4001", "expiry_date": "2027-01-01", "print": False},
    )
    removed = client.post("/api/inventory/remove", json={"barcode": "4001"}).json()
    assert removed["expiry_date"] == "2027-01-01"


def test_druckauftrag_landet_in_der_warteschlange(client):
    result = client.post(
        "/api/labels", json={"name": "Käse", "print": True}
    ).json()
    assert result["print_jobs"]
    # Kein Terminal online -> nicht gedruckt, aber auch nicht verloren.
    assert result["printed"] is False

    queue = client.get("/api/labels/queue").json()
    assert any(job["id"] in result["print_jobs"] for job in queue)

    job = next(j for j in queue if j["id"] == result["print_jobs"][0])
    assert job["status"] == "queued"


def test_etikett_rendert_alle_bloecke():
    from app.services.labels import render_label

    rendered = render_label(
        {
            "label": "LEB000042",
            "name": "Ein sehr langer Produktname der umbrochen wird",
            "brand": "Marke",
            "subcategory": "Sorte",
            "expiry_date": "2026-12-24",
            "added_date": "2026-01-01",
            "quantity": 500,
            "unit": "g",
            "location": "Keller",
        },
        {"paper_chars": 32, "qr": True, "code128": True, "household": "Test"},
    )
    types = [b["t"] for b in rendered["blocks"]]
    # Genau ein Code, nicht beide: nebeneinander kann ein ESC/POS-Drucker
    # nicht, und untereinander kosten sie die halbe Etikettenhoehe fuer
    # dieselbe Information.
    assert ("qr" in types) != ("code128" in types)
    assert "feed" in types
    assert any(b.get("v") == "24.12.2026" for b in rendered["blocks"])
    assert all(len(b["v"]) <= 32 for b in rendered["blocks"] if b["t"] == "text")


@pytest.mark.parametrize("layout", sorted(labels_service.LAYOUTS))
@pytest.mark.parametrize("ausrichtung", ["quer", "hoch"])
def test_etikett_bleibt_im_hoehenbudget(layout, ausrichtung):
    """Der eigentliche Fehler: das Layout passte nicht auf 30 mm.

    Ohne diese Rechnung lief ein Etikett ueber drei Etiketten der Rolle.
    """
    rendered = labels_service.render_label(
        {
            "label": "LEB000042",
            "name": "Ein sehr langer Produktname der umbrochen wird",
            "brand": "Eine ziemlich lange Markenbezeichnung",
            "subcategory": "Schwein",
            "expiry_date": "2026-12-24",
            "added_date": "2026-01-01",
            "quantity": 500,
            "unit": "g",
            "location": "Gefrierschrank",
        },
        {
            "paper_chars": 32, "qr": True, "code128": True,
            "label_width_mm": 50, "label_height_mm": 30,
            "label_layout": layout, "label_orientation": ausrichtung,
        },
    )
    inhalt = labels_service.total_dots(
        [b for b in rendered["blocks"] if b["t"] != "feed"]
    )
    assert inhalt <= rendered["height_dots"] - labels_service.SAFETY_DOTS
    # Der Vorschub fuellt exakt bis zur Perforation auf, damit das naechste
    # Etikett wieder oben anfaengt.
    assert labels_service.total_dots(rendered["blocks"]) == rendered["height_dots"]


@pytest.mark.parametrize("layout", sorted(labels_service.LAYOUTS))
def test_name_wird_nicht_abgeschnitten(layout):
    """Doppelte Hoehe heisst doppelte Breite - nur halb so viele Zeichen.

    "Schweinefilet - Schwein" wurde dadurch mitten im Wort gekappt. Passt der
    Name nicht gross, gehoert er lieber normal gesetzt und vollstaendig aufs
    Etikett.
    """
    rendered = labels_service.render_label(
        {"label": "LEB000042", "name": "Rueckenfilet", "subcategory": "Schwein",
         "expiry_date": "2026-12-24", "added_date": "2026-01-01"},
        {"label_layout": layout, "paper_chars": 32, "qr": True, "code128": True},
    )
    gedruckt = " ".join(b["v"] for b in rendered["blocks"] if b["t"] == "text")
    assert "Rueckenfilet - Schwein" in gedruckt


@pytest.mark.parametrize("rueckzug", [0, 24, 40, 999])
def test_rueckzug_verschiebt_die_folgeetiketten_nicht(rueckzug):
    """Ein Etikett muss genau eine Etikettenteilung Papier verbrauchen.

    Der Rueckzug holt den Totbereich zurueck, darf den Vorschub aber nicht aus
    dem Tritt bringen - sonst wandert jedes weitere Etikett ein Stueck weiter
    nach unten, und nach ein paar Stueck druckt er ueber die Perforation.
    """
    rendered = labels_service.render_label(
        {"label": "LEB000042", "name": "Brot", "expiry_date": "2026-12-24",
         "added_date": "2026-01-01", "location": "Keller"},
        {"label_layout": "standard", "label_orientation": "quer",
         "label_width_mm": 50, "label_height_mm": 30,
         "backfeed_dots": rueckzug, "qr": True, "code128": True},
    )
    # 30 mm Teilung bei 8 Punkten je Millimeter, unabhaengig vom Rueckzug.
    assert labels_service.total_dots(rendered["blocks"]) == 30 * labels_service.DOTS_PER_MM

    erwartet = min(rueckzug, labels_service.MAX_BACKFEED_DOTS)
    assert rendered["backfeed"] == erwartet
    zurueck = [b for b in rendered["blocks"] if b["t"] == "back"]
    assert (zurueck[0]["dots"] if zurueck else 0) == erwartet
    # Der Rueckzug steht ganz vorn - danach zu fahren wuerde Gedrucktes
    # ueberschreiben.
    assert not zurueck or rendered["blocks"][0]["t"] == "back"


def test_rueckzug_ohne_totbereich_schafft_keine_flaeche():
    """Zurueckziehen holt nur zurueck, was der Totbereich genommen hat.

    Erst hiess es, der Rueckzug vergroessere die Druckflaeche immer - das war
    falsch: gibt es keinen Totbereich, faehrt der Drucker dabei in die Luecke
    vor dem Etikett und gewinnt nichts.
    """
    basis = {"label": "LEB000042", "name": "Brot", "expiry_date": "2026-12-24"}
    cfg = {"label_layout": "vollstaendig", "label_width_mm": 50,
           "label_height_mm": 30, "qr": True, "code128": True}
    ohne = labels_service.render_label(basis, cfg)
    mit = labels_service.render_label(basis, {**cfg, "backfeed_dots": 40})
    assert mit["height_dots"] == ohne["height_dots"]


def test_gedreht_niemals_strichcode_auch_wenn_qr_abgeschaltet_ist():
    """Der Schalter "QR" darf das Querformat nicht kaputt machen.

    ESC V dreht nur Zeichen; ein Strichcode laeuft weiter in Papierrichtung
    und steht dann quer zur Schrift. Genau so kam der erste Querformat-Druck
    heraus - Text gedreht, Strichcode nicht, und beides zusammen passte nicht
    mehr auf ein Etikett.
    """
    rendered = labels_service.render_label(
        {"label": "LEB000008", "name": "Natürliches Mineralwasser",
         "brand": "Vilsa", "expiry_date": "2028-11-12", "added_date": "2026-08-12",
         "quantity": 1, "location": "Kühlschrank"},
        {"label_layout": "vollstaendig", "label_orientation": "quer",
         "qr": False, "code128": True},
    )
    types = [b["t"] for b in rendered["blocks"]]
    assert "code128" not in types and "qr" in types


@pytest.mark.parametrize("totbereich", [0, 3, 6, 10])
@pytest.mark.parametrize("layout", sorted(labels_service.LAYOUTS))
def test_totbereich_haelt_das_etikett_bei_einem(layout, totbereich):
    """Was der Druckkopf nicht erreicht, zaehlt nicht zur bedruckbaren Hoehe.

    Ohne diese Rechnung ging der Server von der vollen Etikettenteilung aus,
    tatsaechlich stand der Anfang aber schon hinter dem Kopf - der Code
    rutschte aufs Folgeetikett.
    """
    rendered = labels_service.render_label(
        {"label": "LEB000008", "name": "Natürliches Mineralwasser",
         "brand": "Vilsa", "expiry_date": "2028-11-12", "added_date": "2026-08-12",
         "quantity": 1, "location": "Kühlschrank"},
        {"label_layout": layout, "label_orientation": "quer",
         "label_width_mm": 50, "label_height_mm": 30,
         "label_dead_zone_mm": totbereich, "qr": True, "code128": True},
    )
    inhalt = labels_service.total_dots(
        [b for b in rendered["blocks"] if b["t"] not in ("feed", "back")]
    )
    bedruckbar = (30 - totbereich) * labels_service.DOTS_PER_MM
    assert inhalt <= bedruckbar - labels_service.SAFETY_DOTS

    # Und der Papiervorschub bleibt trotzdem genau eine Etikettenteilung,
    # sonst wandern die Folgeetiketten.
    assert labels_service.total_dots(rendered["blocks"]) == 30 * labels_service.DOTS_PER_MM


def test_rueckzug_holt_den_totbereich_als_druckflaeche_zurueck():
    basis = {"label": "LEB000008", "name": "Mineralwasser",
             "expiry_date": "2028-11-12", "added_date": "2026-08-12"}
    cfg = {"label_layout": "vollstaendig", "label_width_mm": 50,
           "label_height_mm": 30, "label_dead_zone_mm": 6, "qr": True}
    ohne = labels_service.render_label(basis, cfg)
    mit = labels_service.render_label(basis, {**cfg, "backfeed_dots": 48})
    assert mit["height_dots"] == ohne["height_dots"] + 48


def test_quer_dreht_den_text_und_nimmt_den_qr_code():
    """Querformat heisst gedrehter Text - und damit zwingend QR.

    Der Druckkopf schreibt seine Zeilen ueber die kurze Kante; wer das Etikett
    quer lesen will, braucht die Drehung. ESC V dreht aber nur Zeichen: ein
    Strichcode liefe weiter in Papierrichtung und stuende quer zur Schrift.
    Ein QR-Code ist quadratisch und aus jeder Richtung lesbar.
    """
    rendered = labels_service.render_label(
        {"label": "LEB000042", "name": "Brot", "expiry_date": "2026-12-24"},
        {"label_layout": "kompakt", "label_orientation": "quer",
         "qr": True, "code128": True},
    )
    types = [b["t"] for b in rendered["blocks"]]
    assert rendered["rotate"] is True
    assert "qr" in types and "code128" not in types
    # Quer gelesen: Zeilen laufen ueber die lange Kante, gestapelt wird ueber
    # die kurze.
    assert (rendered["line_mm"], rendered["stack_mm"]) == (50, 30)


def test_hoch_druckt_ungedreht_und_darf_den_strichcode():
    rendered = labels_service.render_label(
        {"label": "LEB000042", "name": "Brot", "expiry_date": "2026-12-24"},
        {"label_layout": "kompakt", "label_orientation": "hoch",
         "qr": True, "code128": True},
    )
    assert rendered["rotate"] is False
    assert "code128" in [b["t"] for b in rendered["blocks"]]
    assert (rendered["line_mm"], rendered["stack_mm"]) == (30, 50)


# ------------------------------------------------------------------- Filterung
def test_inventarfilter(client):
    assert client.get("/api/inventory?q=Milch").json()
    assert client.get("/api/inventory?status=removed").status_code == 200
    stats = client.get("/api/inventory/stats").json()
    assert stats["total"] > 0


def test_kategorie_anlegen_und_doppelt_anlegen(client):
    body = {"name": "Testkategorie", "color": "#123456", "icon": "", "sort_order": 99}
    assert client.post("/api/categories", json=body).status_code == 201
    assert client.post("/api/categories", json=body).status_code == 409


def test_health_und_system(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert "inventory" in client.get("/api/system").json()


def test_csv_export(client):
    resp = client.get("/api/export/csv")
    assert resp.status_code == 200
    assert "Etikett;Barcode" in resp.text


# --------------------------------------------------------------- Geraeteablauf
def test_geraeteworkflow_ohne_hardware(client):
    """Der komplette Scan-Ablauf, angestossen wie vom Terminal."""
    import asyncio

    from app.db import session_scope
    from app.device import workflow

    async def scenario():
        sess = workflow.session_for("testgeraet")
        sess.location = "Kuehlschrank"

        async with session_scope() as session:
            # 1. Startbildschirm
            home = await workflow.render(session, sess)
            assert home["kind"] == "home"

            # 2. Vorlagen-Weg statt Netzabfrage
            await workflow.on_tap(session, sess, "templates")
            assert sess.current == workflow.TMPL_CATEGORY

            await workflow.on_tap(session, sess, "cat:Backwaren")
            assert sess.current == workflow.TMPL_PRODUCT

            screen = await workflow.render(session, sess)
            tpl_id = screen["items"][0]["id"]
            await workflow.on_tap(session, sess, tpl_id)

            # Gebaeck hat keine Einheit und keine Marken -> direkt zum Datum
            assert sess.current == workflow.ENTER_DATE
            assert sess.draft.expiry_date  # aus shelf_days vorbelegt

            await workflow.on_tap(session, sess, "ok")
            assert sess.current == workflow.ENTER_QTY

            await workflow.on_input(session, sess, 2)
            assert sess.draft.count == 2

            await workflow.on_tap(session, sess, "ok")
            assert sess.current == workflow.RESULT
            assert len(sess.last_result) == 4

        # 3. Ein eigenes Etikett scannen lagert aus
        async with session_scope() as session:
            from sqlalchemy import select

            from app.models import InventoryItem

            item = (
                await session.execute(
                    select(InventoryItem)
                    .where(InventoryItem.source_device == "testgeraet")
                    .limit(1)
                )
            ).scalar_one()
            label = item.label

            await workflow.on_scan(session, sess, label)

        async with session_scope() as session:
            from app.services import inventory as inv

            assert await inv.find_active_by_label(session, label) is None
            # Erneuter Scan bucht zurueck
            await workflow.on_scan(session, sess, label)

        async with session_scope() as session:
            from app.services import inventory as inv

            assert await inv.find_active_by_label(session, label) is not None

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
