"""GS1-Codes (PDF417/DataMatrix an Frischfleisch) lesen - services/gs1.py."""

from app.services import gs1

# PDF417 von einer Fleischschale (Foto), so wie die Kamera ihn liefert.
SCHALE = "010430652852811517261102310300115939220926"


def test_fleischschale():
    r = gs1.lesen(SCHALE)
    assert r["gtin"] == "04306528528115" and r["ean"] == "4306528528115"
    assert r["mhd"] == "2026-11-02"
    assert (r["menge"], r["einheit"]) == (1159, "g")
    assert r["preis"] == 9.26


def test_trenner_und_symbologie():
    # FNC1 als GS, als "]" (Tastatur-Scanner) und mit Symbologie-Kennung davor
    for code in ("]d2010400000000000710ABC12\x1d17270131",
                 "010400000000000710ABC12]17270131",
                 "]C1010400000000000710ABC12\x1d17270131"):
        r = gs1.lesen(code)
        assert r["charge"] == "ABC12" and r["mhd"] == "2027-01-31", code


def test_tag_00_ist_monatsende_und_15_nach_17():
    assert gs1.lesen("01040000000000071526020000")["mhd"] == "2026-02-28"
    # 17 (verbrauchen bis) geht vor 15 (mindestens haltbar bis)
    assert gs1.lesen("0104000000000007152701011726123")["mhd"] == "2027-01-01"
    assert gs1.lesen("010400000000000715270101172612 31".replace(" ", ""))["mhd"] == "2026-12-31"


def test_volumen_und_stueck():
    assert gs1.lesen("01040000000000073153000750")["menge"] == 750      # 3153: 0,750 l
    assert gs1.lesen("01040000000000073152000750")["menge"] == 7500     # 3152: 7,50 l
    r = gs1.lesen("01040000000000073012")
    assert (r["menge"], r["einheit"]) == (12, "St.")


def test_digital_link():
    r = gs1.lesen("https://id.gs1.org/01/04306528528115/10/L42?17=261102")
    assert r["ean"] == "4306528528115" and r["charge"] == "L42" and r["mhd"] == "2026-11-02"


def test_gewoehnliche_codes_bleiben_gewoehnlich():
    for code in ("4306528528115", "40000000", "01040000000000007", "LEB000027",
                 "https://example.com/produkt", "0104000000000007"):
        assert gs1.lesen(code) is None, code


# ---------------------------------------------------------- im Scan-Ablauf
import asyncio  # noqa: E402
import random  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.device import workflow  # noqa: E402
from app.device.hub import hub  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Location, Product  # noqa: E402
from app.services import openfoodfacts  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:      # legt die Datenbank an
        yield c


@pytest.fixture()
def schale(client, monkeypatch):
    """Ein Produkt mit EAN, sein GS1-Code (MHD + 500 g) und ein Kuehlschrank."""
    ean = "40" + "".join(random.choice("0123456789") for _ in range(11))
    ort = f"Kuehl-{ean}"

    async def anlegen():
        async with session_scope() as session:
            session.add_all([Location(name=ort, kind="kuehl"),
                             Product(barcode=ean, name="Schweinenacken", category="Fleisch & Fisch")])
            await session.commit()
    _run(anlegen())

    async def nachschlagen(session, code, refresh=False):
        from app.services import inventory as inv
        return await inv.product_for_barcode(session, code)
    monkeypatch.setattr(openfoodfacts, "lookup", nachschlagen)
    return ean, f"010{ean}17271231\x1d3103000500", ort


def test_handy_scan_liest_gs1(client, schale):
    ean, code, _ = schale
    r = client.post("/api/inventory/scan", json={"code": code}).json()
    assert r["code"] == ean and r["product"]["name"] == "Schweinenacken"
    assert r["gs1"]["mhd"] == "2027-12-31" and r["gs1"]["menge"] == 500


def test_terminal_scan_liest_gs1(schale, monkeypatch):
    ean, code, ort = schale

    async def senden(device_id, msg):
        pass
    monkeypatch.setattr(hub, "send_to", senden)

    async def scannen():
        sess = workflow.session_for(f"gs1-{ean}")
        sess.location = ort
        sess.erfasst()
        async with session_scope() as session:
            await workflow.on_scan(session, sess, code)
            stapel = list(sess.stack)
            if sess.current == workflow.SUBCATEGORY:
                await workflow.on_tap(session, sess, "sub:Schwein")
                stapel += list(sess.stack)
        assert sess.current == workflow.ENTER_QTY
        return sess.draft, stapel

    draft, stapel = _run(scannen())
    assert draft.barcode == ean and draft.name == "Schweinenacken"
    assert draft.expiry_date == "2027-12-31"          # Kuehlschrank: wie gedruckt
    assert (draft.quantity, draft.unit) == (500, "g")
    # Das MHD wird nicht noch einmal gefragt: nach der Sorte (Fleisch hat
    # Unterkategorien) gleich die Anzahl.
    assert workflow.ENTER_DATE not in stapel
