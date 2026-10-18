"""Lagerarten und MHD-Vorschlag: derselbe Joghurt haelt im Gefrierschrank laenger."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import Product
from app.services import haltbarkeit, inventory as inv, ki
from app.services.dates import shift_iso


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _ort(client, name, kind=None):
    body = {"name": name}
    if kind is not None:
        body["kind"] = kind
    r = client.post("/api/locations", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_lagerart_wird_aus_dem_namen_geraten():
    assert haltbarkeit.art_raten("Gefriertruhe (extern)") == "tiefkuehl"
    assert haltbarkeit.art_raten("Kühlschrank Keller") == "kuehl"
    assert haltbarkeit.art_raten("Vorratskammer") == "raum"


def test_neuer_ort_bekommt_eine_lagerart_und_behaelt_sie(client):
    tag = uuid.uuid4().hex[:6]
    ort = _ort(client, f"Truhe {tag}")
    assert ort["kind"] == "tiefkuehl"
    r = client.put(f"/api/locations/{ort['id']}", json={"name": ort["name"], "kind": "kuehl"})
    assert r.json()["kind"] == "kuehl"
    # Ohne Angabe bleibt sie stehen (aelterer Browser).
    r = client.put(f"/api/locations/{ort['id']}", json={"name": ort["name"]})
    assert r.json()["kind"] == "kuehl"
    assert client.put(f"/api/locations/{ort['id']}",
                      json={"name": ort["name"], "kind": "sauna"}).status_code == 422


def test_vorschlag_haengt_an_der_lagerart(client):
    tag = uuid.uuid4().hex[:6]
    kuehl, tief, raum = (_ort(client, f"K-{tag}", "kuehl"), _ort(client, f"G-{tag}", "tiefkuehl"),
                         _ort(client, f"R-{tag}", "raum"))
    frage = lambda ort: client.get(  # noqa: E731
        "/api/haltbarkeit/vorschlag", params={"ort": ort["name"], "kategorie": "Fleisch & Fisch"}).json()

    k, g, r = frage(kuehl), frage(tief), frage(raum)
    standard = haltbarkeit.STANDARD["Fleisch & Fisch"]
    assert k["tage"] == standard["kuehl"] and k["datum"] == shift_iso(standard["kuehl"])
    assert g["tage"] == standard["tiefkuehl"] and "Gefrierschrank" in g["quelle"]
    assert r["tage"] == 0 and r["datum"] == ""      # Fleisch lagert man nicht offen


def test_produkt_geht_der_kategorie_vor_aber_nicht_im_gefrierschrank():
    async def scenario():
        async with session_scope() as session:
            tag = uuid.uuid4().hex[:6]
            from app.models import Location
            session.add_all([Location(name=f"KK-{tag}", kind="kuehl"),
                             Location(name=f"GG-{tag}", kind="tiefkuehl")])
            await session.flush()
            joghurt = Product(barcode=f"9{tag}", name="Joghurt", default_shelf_days=21)
            kuehl = await haltbarkeit.vorschlag(session, ort=f"KK-{tag}", kategorie="Milchprodukte",
                                                produkt=joghurt)
            tief = await haltbarkeit.vorschlag(session, ort=f"GG-{tag}", kategorie="Milchprodukte",
                                               produkt=joghurt)
            joghurt.haltbarkeit = {"tiefkuehl": 60}
            eigen = await haltbarkeit.vorschlag(session, ort=f"GG-{tag}", kategorie="Milchprodukte",
                                                produkt=joghurt)
            await session.rollback()
            return kuehl["tage"], tief["tage"], eigen["tage"]

    kuehl, tief, eigen = _run(scenario())
    assert kuehl == 21                                   # Produkt: 21 Tage gekuehlt
    assert tief == haltbarkeit.STANDARD["Milchprodukte"]["tiefkuehl"]   # nicht 21!
    assert eigen == 60


def test_kategorie_haltbarkeit_einstellen(client):
    r = client.post("/api/categories", json={"name": f"Test-{uuid.uuid4().hex[:6]}",
                                             "haltbarkeit": {"raum": 10, "kuehl": "x", "tiefkuehl": -3,
                                                             "mond": 5}})
    assert r.status_code == 201
    assert r.json()["haltbarkeit"] == {"raum": 10}


def test_eingegebenes_produkt_wird_gemerkt(client):
    code = f"40{uuid.uuid4().int % 10**11:011d}"
    r = client.post("/api/labels", json={"name": "Hausgemachter Sirup", "barcode": code,
                                         "category": "Getränke", "print": False})
    assert r.status_code == 201

    async def holen():
        async with session_scope() as session:
            p = await inv.product_for_barcode(session, code)
            return p.name, p.category
    assert _run(holen()) == ("Hausgemachter Sirup", "Getränke")


def test_ki_ohne_schluessel_ist_aus(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert client.get("/api/ki").json()["aktiv"] is False
    assert client.post("/api/ki/vorschlag", json={"name": "Milch"}).status_code == 503


def test_ki_vorschlag_fuellt_nur_leeres_und_wird_gemerkt(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    gefragt = []

    async def vorschlagen(name, marke, kategorien):
        gefragt.append((name, marke))
        assert "Milchprodukte" in kategorien
        return {"kategorie": "Milchprodukte", "sorte": "Vollmilch",
                "haltbarkeit": {"kuehl": 10, "tiefkuehl": 90}, "hinweis": "Kühl lagern."}

    monkeypatch.setattr(ki, "vorschlagen", vorschlagen)
    code = f"41{uuid.uuid4().int % 10**11:011d}"

    async def anlegen():
        async with session_scope() as session:
            session.add(Product(barcode=code, name="", brand="", subcategory="Bio"))
            await session.commit()
    _run(anlegen())

    r = client.post("/api/ki/vorschlag", json={"name": "Frische Milch", "brand": "Hof", "barcode": code})
    assert r.status_code == 200, r.text
    assert r.json()["haltbarkeit"] == {"kuehl": 10, "tiefkuehl": 90}
    assert gefragt == [("Frische Milch", "Hof")]

    async def holen():
        async with session_scope() as session:
            p = await inv.product_for_barcode(session, code)
            return p.name, p.category, p.subcategory, p.haltbarkeit
    name, kategorie, sorte, halt = _run(holen())
    assert (name, kategorie, halt) == ("Frische Milch", "Milchprodukte", {"kuehl": 10, "tiefkuehl": 90})
    assert sorte == "Bio"        # Eingetragenes bleibt

    # Danach rechnet der Vorschlag mit den Werten des Produkts.
    tag = uuid.uuid4().hex[:6]
    _ort(client, f"KS-{tag}", "kuehl")
    v = client.get("/api/haltbarkeit/vorschlag",
                   params={"ort": f"KS-{tag}", "kategorie": "Milchprodukte", "barcode": code}).json()
    assert v["tage"] == 10 and v["quelle"] == "Produkt"


def test_ki_schema_beschraenkt_die_kategorie():
    schema = ki._schema(["Getränke", "Konserven"])
    assert schema["properties"]["kategorie"]["enum"] == ["Getränke", "Konserven"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["haltbarkeit"]["required"]) == set(haltbarkeit.ARTEN)


def test_vorlage_mit_haltbarkeit_je_lagerart(client):
    tag = uuid.uuid4().hex[:6]
    r = client.post("/api/templates", json={"name": f"Hack {tag}", "category": "Fleisch & Fisch",
                                            "haltbarkeit": {"kuehl": 1, "tiefkuehl": 120}})
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert tpl["haltbarkeit"] == {"kuehl": 1, "tiefkuehl": 120} and tpl["shelf_days"] == 1

    async def frage(art):
        async with session_scope() as session:
            from app.models import Location
            session.add(Location(name=f"{art}-{tag}", kind=art))
            await session.flush()
            v = await haltbarkeit.vorschlag(session, ort=f"{art}-{tag}", kategorie="Fleisch & Fisch",
                                            vorlage=tpl["haltbarkeit"])
            await session.rollback()
            return v
    tief, raum = _run(frage("tiefkuehl")), _run(frage("raum"))
    assert tief["tage"] == 120 and tief["quelle"].startswith("Vorlage")
    assert raum["tage"] == 0     # leer = wie die Kategorie, und Fleisch lagert nicht offen

    # Ohne Feld bleibt die Haltbarkeit stehen (aelterer Browser).
    r = client.put(f"/api/templates/{tpl['id']}", json={"name": tpl["name"], "category": "Fleisch & Fisch"})
    assert r.json()["haltbarkeit"] == {"kuehl": 1, "tiefkuehl": 120}
    r = client.put(f"/api/templates/{tpl['id']}", json={"name": tpl["name"], "category": "Fleisch & Fisch",
                                                       "haltbarkeit": {"kuehl": 3}})
    assert r.json()["haltbarkeit"] == {"kuehl": 3} and r.json()["shelf_days"] == 3


def test_vorschlag_mit_vorlage_ueber_die_api(client):
    tag = uuid.uuid4().hex[:6]
    tpl = client.post("/api/templates", json={"name": f"Gulasch {tag}", "category": "Fleisch & Fisch",
                                              "haltbarkeit": {"tiefkuehl": 120}}).json()
    _ort(client, f"T-{tag}", "tiefkuehl")
    v = client.get("/api/haltbarkeit/vorschlag", params={"ort": f"T-{tag}", "vorlage": tpl["id"]}).json()
    assert v["tage"] == 120 and v["quelle"].startswith("Vorlage")


def test_mhd_aus_den_teilen_des_aufdrucks():
    from datetime import date
    heute = date(2026, 10, 18)
    teile = ki.mhd_aus_teilen
    assert teile(0, 1, 2027, heute) == "2027-01-31"        # "01.2027": bis Monatsende
    assert teile(28, 10, 2027, heute) == "2027-10-28"      # "28/10/2027"
    assert teile(7, 10, 27, heute) == "2027-10-07"         # "07/10/27"
    assert teile(0, 2, 28, heute) == "2028-02-29"          # Schaltjahr
    assert teile(31, 4, 2027, heute) == ""                 # gibt es nicht
    assert teile(15, 3, 2020, heute) == ""                 # eher Charge als MHD
    assert teile("x", 1, 2027, heute) == ""


def test_mhd_foto_ohne_ki_und_mit_ki(client, monkeypatch):
    bild = "A" * 200
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert client.post("/api/ki/mhd", json={"bild": bild}).status_code == 503

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    async def lesen(b, typ):
        assert b == bild and typ == "image/jpeg"
        return {"datum": "2026-11-16", "gelesen": "16.11.2026"}
    monkeypatch.setattr(ki, "mhd_lesen", lesen)
    r = client.post("/api/ki/mhd", json={"bild": bild})
    assert r.status_code == 200 and r.json()["datum"] == "2026-11-16"
    assert client.post("/api/ki/mhd", json={"bild": bild, "typ": "image/gif"}).status_code == 422
