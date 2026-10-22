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
    assert frage(f"1{tag}", "Truhe", mhd=spaet)["datum"] == spaet
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
    # Datum im selben Zug selbst gesetzt: bleibt.
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
