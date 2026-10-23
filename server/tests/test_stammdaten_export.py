"""Vorlagen und Stammdaten exportieren und wieder einlesen."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _import(client, daten, **params):
    return client.post("/api/stammdaten/import", params=params,
                       files={"file": ("s.json", json.dumps(daten).encode(), "application/json")})


def test_export_enthaelt_alle_teile_ohne_bestand(client):
    tag = uuid.uuid4().hex[:6]
    client.post("/api/templates", json={"name": f"Hack {tag}", "category": "Fleisch & Fisch",
                                        "haltbarkeit": {"kuehl": 1, "tiefkuehl": 90},
                                        "brands": ["Metzger"], "sorten": ["Rind"]})
    r = client.get("/api/stammdaten/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["art"] == "lebensmittel-stammdaten" and "inventory" not in d
    assert {"haushalte", "kategorien", "lagerorte", "vorlagen", "produkte"} <= set(d)
    hack = next(v for v in d["vorlagen"] if v["name"] == f"Hack {tag}")
    assert hack["haltbarkeit"] == {"kuehl": 1, "tiefkuehl": 90} and hack["brands"] == ["Metzger"]
    assert "id" not in hack
    nur = client.get("/api/stammdaten/export", params={"teile": "kategorien"}).json()
    assert "kategorien" in nur and "vorlagen" not in nur
    assert client.get("/api/stammdaten/export", params={"teile": "bestand"}).status_code == 400


def test_import_ergaenzt_ueberschreibt_und_prueft(client):
    tag = uuid.uuid4().hex[:6]
    datei = {"art": "lebensmittel-stammdaten", "version": 1,
             "kategorien": [{"name": f"Eis {tag}", "color": "#00aaff", "haltbarkeit": {"tiefkuehl": 60}}],
             "lagerorte": [{"name": f"Truhe {tag}", "kind": "tiefkuehl"}],
             "vorlagen": [{"name": f"Suppe {tag}", "category": f"Eis {tag}", "unit": "ml",
                           "haltbarkeit": {"kuehl": 3, "tiefkuehl": 120}, "brands": ["Oma"]}],
             "produkte": [{"barcode": f"77{tag}", "name": "Pizza", "lagerart": "tiefkuehl",
                           "lagerart_fest": False, "lagerart_abgefragt": True, "source": "off"}]}

    # Pruefen schreibt nichts.
    r = _import(client, datei, probe=True).json()
    assert r["probe"] and r["teile"]["vorlagen"]["neu"] == 1
    assert not any(v["name"] == f"Suppe {tag}" for v in client.get("/api/templates").json())

    r = _import(client, datei).json()
    assert all(t["neu"] == 1 for t in r["teile"].values())
    tpl = next(v for v in client.get("/api/templates").json() if v["name"] == f"Suppe {tag}")
    assert tpl["haltbarkeit"] == {"kuehl": 3, "tiefkuehl": 120} and tpl["unit"] == "ml"
    ort = next(o for o in client.get("/api/locations").json() if o["name"] == f"Truhe {tag}")
    assert ort["kind"] == "tiefkuehl"
    # Schon abgefragt: kein erneuter OpenFoodFacts-Abruf nach dem Umzug.
    pizza = next(p for p in client.get("/api/stammdaten/export", params={"teile": "produkte"}).json()["produkte"]
                 if p["barcode"] == f"77{tag}")
    assert pizza["lagerart_abgefragt"] is True and pizza["lagerart"] == "tiefkuehl"

    # Ergaenzen laesst Vorhandenes stehen, Ueberschreiben nimmt die Datei.
    datei["vorlagen"][0]["haltbarkeit"] = {"tiefkuehl": 200}
    r = _import(client, datei).json()
    assert r["teile"]["vorlagen"] == {"neu": 0, "geaendert": 0, "gleich": 0, "uebersprungen": 1}
    r = _import(client, datei, modus="ueberschreiben").json()
    assert r["teile"]["vorlagen"]["geaendert"] == 1
    tpl = next(v for v in client.get("/api/templates").json() if v["name"] == f"Suppe {tag}")
    assert tpl["haltbarkeit"] == {"tiefkuehl": 200}

    # Nur gewaehlte Teile.
    datei["kategorien"][0]["color"] = "#ff0000"
    r = _import(client, datei, modus="ueberschreiben", teile="vorlagen").json()
    assert set(r["teile"]) == {"vorlagen"}
    kat = next(k for k in client.get("/api/categories").json() if k["name"] == f"Eis {tag}")
    assert kat["color"] == "#00aaff"


def test_falsche_dateien_werden_abgelehnt(client):
    assert client.post("/api/stammdaten/import",
                       files={"file": ("x.json", b"{kaputt", "application/json")}).status_code == 400
    assert _import(client, {"version": 2, "inventory": []}).status_code == 400
    assert _import(client, {"art": "lebensmittel-stammdaten", "version": 99}).status_code == 400
    assert _import(client, {"art": "lebensmittel-stammdaten", "version": 1},
                   modus="loeschen").status_code == 422


def test_nur_ein_standardort_nach_dem_import(client):
    tag = uuid.uuid4().hex[:6]
    _import(client, {"art": "lebensmittel-stammdaten", "version": 1,
                     "lagerorte": [{"name": f"Keller {tag}", "is_default": True}]})
    assert sum(o["is_default"] for o in client.get("/api/locations").json()) == 1
