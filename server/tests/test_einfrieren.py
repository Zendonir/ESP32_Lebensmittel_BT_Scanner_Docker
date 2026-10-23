"""Einfrieren: frische Ware haelt laenger, tiefgekuehlt gekaufte behaelt ihr MHD."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.device import workflow
from app.device.hub import hub
from app.main import app
from app.models import Location, Product
from app.services import haltbarkeit, openfoodfacts
from app.services.dates import shift_iso

GEFRIER_FLEISCH = haltbarkeit.STANDARD["Fleisch & Fisch"]["tiefkuehl"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _orte_und_produkte(tag):
    async def anlegen():
        async with session_scope() as session:
            session.add_all([
                Location(name=f"Truhe-{tag}", kind="tiefkuehl"),
                Location(name=f"Kuehl-{tag}", kind="kuehl"),
                Product(barcode=f"1{tag}", name="Hähnchen frisch", category="Fleisch & Fisch",
                        lagerart="kuehl"),
                Product(barcode=f"2{tag}", name="Fischstäbchen", category="Fleisch & Fisch",
                        lagerart="tiefkuehl"),
                Product(barcode=f"3{tag}", name="Pizza", category="Tiefkühl"),
            ])
            await session.commit()
    _run(anlegen())


@pytest.mark.parametrize("product,erwartet", [
    ({"storage_conditions_tags": ["en:frozen"]}, "tiefkuehl"),
    ({"storage_conditions_tags": ["en:refrigerated"]}, "kuehl"),
    ({"storage_conditions_tags": ["en:ambient"]}, "raum"),
    ({"categories_tags": ["en:meals", "en:frozen-foods"]}, "tiefkuehl"),
    ({"categories_tags": ["en:pizzas", "de:tiefkuhlpizza"]}, "tiefkuehl"),
    ({"categories_tags": ["fr:surgeles"]}, "tiefkuehl"),
    ({"categories_tags": ["en:frozen-desserts"]}, "tiefkuehl"),
    ({"categories_tags": ["en:dairies", "en:yogurts"]}, ""),
    ({"conservation_conditions_de": "Bei -18 °C lagern. Nach dem Auftauen nicht wieder einfrieren."},
     "tiefkuehl"),
    ({"conservation_conditions_de": "Gekühlt bei max. +7 °C lagern."}, "kuehl"),
    # Was nach dem Oeffnen gilt, sagt nichts ueber den Verkauf.
    ({"conservation_conditions_de": "Kühl und trocken lagern. Nach dem Öffnen gekühlt aufbewahren."}, ""),
    ({}, ""),
])
def test_lagerart_aus_openfoodfacts(product, erwartet):
    assert openfoodfacts.lagerart(product) == erwartet


def test_aufgedrucktes_mhd_im_gefrierschrank(client):
    tag = uuid.uuid4().hex[:8]
    _orte_und_produkte(tag)
    packung = shift_iso(3)

    def frage(code, ort, mhd=packung, kategorie="Fleisch & Fisch"):
        r = client.get("/api/haltbarkeit/eingefroren", params={
            "mhd": mhd, "ort": f"{ort}-{tag}", "kategorie": kategorie, "barcode": code})
        assert r.status_code == 200, r.text
        return r.json()

    # Frisch gekauft, eingefroren: verlaengert.
    v = frage(f"1{tag}", "Truhe")
    assert v["verlaengert"] and v["datum"] == shift_iso(GEFRIER_FLEISCH)
    # Tiefgekuehlt gekauft: 1:1 - auch ueber die Kategorie "Tiefkühl".
    v = frage(f"2{tag}", "Truhe")
    assert not v["verlaengert"] and v["datum"] == packung and "tiefgekühlt" in v["hinweis"]
    v = frage(f"3{tag}", "Truhe", kategorie="Tiefkühl")
    assert not v["verlaengert"] and v["datum"] == packung
    # Kuehlschrank: bleibt.
    assert frage(f"1{tag}", "Kuehl")["datum"] == packung
    # Nie verkuerzt: Konserve mit langem MHD im Gefrierschrank.
    spaet = shift_iso(GEFRIER_FLEISCH + 400)
    v = frage(f"1{tag}", "Truhe", mhd=spaet)
    assert v["datum"] == spaet and "länger" in v["hinweis"]
    # Getraenke haben keine Gefrier-Haltbarkeit (0): bleibt, mit Begruendung.
    v = frage(f"9{tag}", "Truhe", kategorie="Getränke")
    assert not v["verlaengert"] and v["datum"] == packung and "Getränke" in v["hinweis"]
    # Anzeigeformat wird verstanden.
    tt = packung.split("-")
    assert frage(f"1{tag}", "Truhe", mhd=f"{tt[2]}.{tt[1]}.{tt[0]}")["verlaengert"]


def test_umlagern_in_den_gefrierschrank(client):
    tag = uuid.uuid4().hex[:8]
    _orte_und_produkte(tag)
    packung = shift_iso(3)
    labels = {}
    for code in (f"1{tag}", f"2{tag}"):
        r = client.post("/api/labels", json={"name": "x", "barcode": code, "category": "Fleisch & Fisch",
                                             "expiry_date": packung, "location": f"Kuehl-{tag}",
                                             "print": False})
        assert r.status_code == 201, r.text
        labels[code] = r.json()["labels"][0]

    frisch = client.patch(f"/api/inventory/{labels[f'1{tag}']}", json={"location": f"Truhe-{tag}"}).json()
    assert frisch["expiry_date"] == shift_iso(GEFRIER_FLEISCH)
    gefroren = client.patch(f"/api/inventory/{labels[f'2{tag}']}", json={"location": f"Truhe-{tag}"}).json()
    assert gefroren["expiry_date"] == packung
    zurueck = client.patch(f"/api/inventory/{labels[f'1{tag}']}", json={"location": f"Kuehl-{tag}"}).json()
    assert zurueck["expiry_date"] == shift_iso(GEFRIER_FLEISCH)    # Auftauen kuerzt nicht
    # Bearbeiten-Dialog schickt das unveraenderte Datum mit: verlaengert trotzdem.
    r = client.post("/api/labels", json={"name": "x", "barcode": f"1{tag}", "category": "Fleisch & Fisch",
                                         "expiry_date": packung, "location": f"Kuehl-{tag}", "print": False})
    dialog = client.patch(f"/api/inventory/{r.json()['labels'][0]}",
                          json={"location": f"Truhe-{tag}", "expiry_date": packung}).json()
    assert dialog["expiry_date"] == shift_iso(GEFRIER_FLEISCH)
    # Datum im selben Zug selbst geaendert: bleibt.
    eigen = shift_iso(5)
    r = client.patch(f"/api/inventory/{labels[f'1{tag}']}",
                     json={"location": f"Truhe-{tag}", "expiry_date": eigen}).json()
    assert r["expiry_date"] == eigen


def test_terminal_verlaengert_beim_eintippen(client, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    _orte_und_produkte(tag)
    gesendet = []

    async def senden(device_id, msg):
        gesendet.append(msg)
    monkeypatch.setattr(hub, "send_to", senden)
    packung = shift_iso(3)

    async def tippen(code):
        sess = workflow.session_for(f"frost-{tag}-{code}")
        sess.location = f"Truhe-{tag}"
        sess.draft = workflow.Draft(name="x", barcode=code, category="Fleisch & Fisch")
        sess.stack = [workflow.HOME, workflow.ENTER_DATE]
        async with session_scope() as session:
            await workflow.on_input(session, sess, packung)
        return sess.draft.expiry_date

    assert _run(tippen(f"1{tag}")) == shift_iso(GEFRIER_FLEISCH)
    assert any("Eingefroren" in str(m) for m in gesendet)
    assert _run(tippen(f"2{tag}")) == packung


def test_haken_im_original_gefroren_wird_gemerkt(client, monkeypatch):
    """Falsch erkannt -> beim Einlagern umstellen; der naechste Scan kennt es."""
    tag = uuid.uuid4().hex[:8]
    _orte_und_produkte(tag)
    packung = shift_iso(3)

    async def nachschlagen(session, code, refresh=False):
        from app.services import inventory as inv
        return await inv.product_for_barcode(session, code)
    monkeypatch.setattr(openfoodfacts, "lookup", nachschlagen)

    def scan(code):
        r = client.post("/api/inventory/scan", json={"code": code})
        assert r.status_code == 200, r.text
        return r.json()["product"]

    def einlagern(code, gefroren):
        r = client.post("/api/labels", json={"name": "x", "barcode": code, "category": "Fleisch & Fisch",
                                             "expiry_date": packung, "location": f"Truhe-{tag}",
                                             "print": False, "gefroren": gefroren})
        assert r.status_code == 201, r.text

    frisch, pizza = f"1{tag}", f"3{tag}"
    assert scan(frisch)["gefroren"] is False
    assert scan(pizza)["gefroren"] is True            # ueber die Kategorie "Tiefkühl"

    # Haken setzen bzw. wegnehmen - danach so beim naechsten Scan.
    einlagern(frisch, True)
    einlagern(pizza, False)
    assert scan(frisch)["gefroren"] is True
    assert scan(pizza)["gefroren"] is False

    # Die Rechnung folgt dem Haken, auch bevor gespeichert ist.
    frage = {"mhd": packung, "ort": f"Truhe-{tag}", "kategorie": "Fleisch & Fisch", "barcode": f"2{tag}"}
    assert not client.get("/api/haltbarkeit/eingefroren", params=frage).json()["verlaengert"]
    assert client.get("/api/haltbarkeit/eingefroren",
                      params={**frage, "gefroren": "false"}).json()["verlaengert"]


def test_von_hand_gesetzt_ueberschreibt_openfoodfacts_nicht():
    p = Product(barcode="x", lagerart="", lagerart_fest=False, category="Tiefkühl")
    assert haltbarkeit.gefroren_setzen(p, False) is True      # Kategorie sagt gefroren
    assert p.lagerart == "" and p.lagerart_fest
    # Kategorie "Tiefkühl" erst aus derselben Maske: der Haken wird trotzdem fest.
    p = Product(barcode="z", lagerart="", lagerart_fest=False, category="Tiefkühl")
    assert haltbarkeit.gefroren_setzen(p, True) is True
    assert p.lagerart == "tiefkuehl" and p.lagerart_fest
    p = Product(barcode="y", lagerart="", lagerart_fest=False, category="")
    assert haltbarkeit.gefroren_setzen(p, True) is True
    assert p.lagerart == "tiefkuehl" and p.lagerart_fest
    assert haltbarkeit.gefroren_setzen(p, True) is False      # nichts zu tun
    assert haltbarkeit.gefroren_setzen(p, False) is True
    assert p.lagerart == "" and haltbarkeit.schon_gefroren(p, "Tiefkühl") is False


def test_alte_produkte_holen_die_lagerart_einmal_nach(client, monkeypatch):
    """Vor dem Update gescannt: beim naechsten Scan einmal OpenFoodFacts fragen,
    nur die Lagerart uebernehmen, danach nicht wieder."""
    from datetime import datetime, timedelta, timezone

    from app.services import inventory as inv
    code = f"5{uuid.uuid4().hex[:10]}"
    alt = datetime.now(timezone.utc) - timedelta(days=30)

    async def anlegen():
        async with session_scope() as session:
            session.add(Product(barcode=code, name="Eigener Name", source="off", fetched_at=alt))
            await session.commit()
    _run(anlegen())

    fragen = []

    async def remote(barcode, available):
        fragen.append(barcode)
        return {"name": "OFF-Name", "brand": "", "amount": "", "category": "", "subcategory": "",
                "nutriscore": "", "image_url": "", "lagerart": "tiefkuehl"}
    monkeypatch.setattr(openfoodfacts, "_fetch_remote", remote)
    import dataclasses
    monkeypatch.setattr(openfoodfacts, "settings",
                        dataclasses.replace(openfoodfacts.settings, openfoodfacts_enabled=True))

    antwort = client.post("/api/inventory/scan", json={"code": code}).json()["product"]
    assert antwort["gefroren"] is True and antwort["name"] == "Eigener Name"
    client.post("/api/inventory/scan", json={"code": code})
    assert fragen == [code]                                   # nur einmal

    # Gezielt abgewaehlt: bleibt so, auch wenn OpenFoodFacts "gefroren" sagt.
    r = client.post("/api/labels", json={"name": "Eigener Name", "barcode": code, "print": False,
                                         "gefroren": False})
    assert r.status_code == 201, r.text
    assert client.post("/api/inventory/scan", json={"code": code}).json()["product"]["gefroren"] is False

    async def holen():
        async with session_scope() as session:
            p = await inv.product_for_barcode(session, code)
            await openfoodfacts.lookup(session, code, refresh=True)
            return p.lagerart, p.lagerart_fest
    assert _run(holen()) == ("", True)
