"""Sprachsteuerung: gesprochene Werte lesen, gefuehrtes Etikett, Bestand, Auslagern."""

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import sprache

HEUTE = date(2026, 11, 1)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _name(basis):
    return f"{basis} {uuid.uuid4().hex[:5]}"


@pytest.mark.parametrize("text, erwartet", [
    ("morgen", "2026-11-02"),
    ("Übermorgen", "2026-11-03"),
    ("in 5 Tagen", "2026-11-06"),
    ("in zwei Wochen", "2026-11-15"),
    ("nächste Woche", "2026-11-08"),
    ("in 3 Monaten", "2027-02-01"),
    ("8. November", "2026-11-08"),
    ("3. Januar", "2027-01-03"),            # schon vorbei -> naechstes Jahr
    ("8. November 2027", "2027-11-08"),
    ("8.11.", "2026-11-08"),
    ("08.11.2026", "2026-11-08"),
    ("2026-12-24", "2026-12-24"),
    ("kein", ""),
    ("ohne MHD", ""),
    ("irgendwann", None),
])
def test_mhd_lesen(text, erwartet):
    assert sprache.mhd_lesen(text, HEUTE) == erwartet


@pytest.mark.parametrize("menge, einheit, erwartet", [
    ("500 g", "", (500, "g")),
    ("1,5 kg", "", (1500, "g")),
    ("2", "Liter", (2000, "ml")),
    (3, "Stück", (3, "St.")),
    ("drei", "", (3, "St.")),
    ("", "", None),
])
def test_menge_lesen(menge, einheit, erwartet):
    assert sprache.menge_lesen(menge, einheit) == erwartet


def test_etikett_gefuehrt(client):
    name = _name("Gulasch")
    r = client.post("/api/sprache/etikett", json={}).json()
    assert r["status"] == "frage" and r["fehlt"] == "name"

    # Mehrere Lagerorte -> Frage mit Auswahl
    r = client.post("/api/sprache/etikett", json={"name": name}).json()
    assert r["fehlt"] == "ort" and "Gefrierschrank" in r["optionen"]

    r = client.post("/api/sprache/etikett", json={"name": name, "ort": "Gefrierschrank"}).json()
    # Ohne Vorlage/Kategorie kein Vorschlag -> nach dem MHD fragen
    assert r["fehlt"] == "mhd"

    r = client.post("/api/sprache/etikett", json={
        "name": name, "ort": "gefrierschrank", "mhd": "kein", "menge": "800 g", "anzahl": 2,
        "drucken": False}).json()
    assert r["status"] == "bestaetigen"
    assert "2 Etiketten" in r["antwort"] and "800 g" in r["antwort"] and "ohne MHD" in r["antwort"]
    werte = r["werte"]
    assert werte["ort"] == "Gefrierschrank" and werte["mhd"] == "kein"
    # Menge reist mit Einheit - auch ohne eigenes Feld "einheit" (Home Assistant)
    assert werte["menge"] == "800 g" and "einheit" not in werte

    r = client.post("/api/sprache/etikett", json={**werte, "bestaetigt": True}).json()
    assert r["status"] == "erledigt" and len(r["labels"]) == 2
    items = client.get("/api/inventory", params={"q": name}).json()
    assert len(items) == 2 and items[0]["quantity"] == 800 and items[0]["location"] == "Gefrierschrank"


def test_etikett_vorschlag_aus_vorlage(client):
    name = _name("Bolognese")
    client.post("/api/templates", json={"name": name, "category": "Fertiggerichte",
                                        "haltbarkeit": {"kuehl": 3, "tiefkuehl": 90}})
    r = client.post("/api/sprache/etikett", json={"name": name.upper(), "ort": "Kühlschrank"}).json()
    assert r["status"] == "bestaetigen"
    assert r["werte"]["name"] == name                 # Name der Vorlage
    assert r["werte"]["mhd"] == (date.today() + timedelta(days=3)).isoformat()
    assert "Vorschlag" in r["antwort"]


def test_etikett_unbekannter_ort_und_datum(client):
    r = client.post("/api/sprache/etikett", json={"name": "X", "ort": "Dachboden"}).json()
    assert r["fehlt"] == "ort" and "Keller" in r["optionen"]
    r = client.post("/api/sprache/etikett", json={"name": "X", "ort": "Keller", "mhd": "bald mal"}).json()
    assert r["fehlt"] == "mhd" and "nicht verstanden" in r["antwort"]


def test_etikett_druckt_ohne_terminal_wartet(client):
    r = client.post("/api/sprache/etikett", json={
        "name": _name("Brot"), "ort": "Vorratskammer", "mhd": "in 3 Tagen",
        "bestaetigt": True}).json()
    assert r["status"] == "erledigt" and r["gedruckt"] is False and "wartet" in r["antwort"]


def test_bestand_ablauf_auslagern(client):
    name = _name("Zwetschgen")
    for mhd in ("morgen", "in 10 Tagen"):
        client.post("/api/sprache/etikett", json={
            "name": name, "ort": "Keller", "mhd": mhd, "drucken": False, "bestaetigt": True})

    r = client.post("/api/sprache/bestand", json={"name": name}).json()
    assert r["anzahl"] == 2 and "2 × " in r["antwort"] and "morgen" in r["antwort"]

    r = client.post("/api/sprache/bestand", json={"ort": "keller"}).json()
    assert r["status"] == "info" and r["anzahl"] >= 2

    r = client.post("/api/sprache/ablauf", json={}).json()
    assert "Morgen" in r["antwort"] and name in r["antwort"]

    # Das mit dem fruehesten MHD geht zuerst
    r = client.post("/api/sprache/auslagern", json={"name": name}).json()
    assert r["status"] == "erledigt" and "morgen" in r["antwort"] and r["rest"] == 1
    r = client.post("/api/sprache/auslagern", json={"name": name, "weggeworfen": True}).json()
    assert "weggeworfen" in r["antwort"] and r["rest"] == 0
    r = client.post("/api/sprache/auslagern", json={"name": name}).json()
    assert r["status"] == "info" and "nicht" in r["antwort"]
    entnommen = client.get("/api/inventory", params={"q": name, "status": "removed"}).json()
    assert sorted(i["removed_reason"] for i in entnommen) == ["sprache", "weggeworfen"]


def test_auslagern_mehrdeutig(client):
    basis = _name("Saft")
    for sorte in ("Apfel", "Orange"):
        client.post("/api/sprache/etikett", json={
            "name": f"{basis} {sorte}", "ort": "Keller", "mhd": "kein", "drucken": False,
            "bestaetigt": True})
    r = client.post("/api/sprache/auslagern", json={"name": basis}).json()
    assert r["status"] == "frage" and len(r["optionen"]) == 2


def test_token(client):
    object.__setattr__(settings, "sprache_token", "geheim")     # Settings ist eingefroren
    try:
        assert client.post("/api/sprache/ablauf", json={},
                           headers={"Authorization": "Bearer falsch"}).status_code == 401
        assert client.post("/api/sprache/ablauf", json={},
                           headers={"Authorization": "Bearer geheim"}).status_code == 200
    finally:
        object.__setattr__(settings, "sprache_token", "")


def test_unbekannter_haushalt_fragt_statt_alle(client):
    name = _name("Quark")
    client.post("/api/sprache/etikett", json={
        "name": name, "ort": "Keller", "mhd": "kein", "drucken": False, "bestaetigt": True})
    for pfad, daten in (("auslagern", {"name": name}), ("bestand", {"name": name}),
                        ("ablauf", {}), ("etikett", {"name": name})):
        r = client.post(f"/api/sprache/{pfad}", json={**daten, "haushalt": "Nirgendwo"}).json()
        assert r["status"] == "frage" and r["fehlt"] == "haushalt", pfad
    # nichts ausgelagert
    assert len(client.get("/api/inventory", params={"q": name}).json()) == 1
