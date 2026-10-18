"""MHD aus erkanntem Text - mit den Texten, die RapidOCR aus echten Fotos las.

Die Texterkennung selbst laeuft hier nicht (die braucht requirements-ocr.txt);
geprueft wird die Auswahl, und die ist der knifflige Teil: auf jeder
Verpackung stehen neben dem MHD Chargen, Uhrzeiten und Naehrwerte.
"""

from __future__ import annotations

import base64
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import ki, mhd_ocr

HEUTE = date(2026, 10, 18)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("texte, datum", [
    # Butter: Naehrwerte, Charge "09406:51L3", Barcode
    (["Energie/EHeprMNHgCToMoCT", "3060k/", "82g", "Salz/Con", "0,5g", "16.11.2026", "0,02g",
      "09406:51L3", "DE", "BE90003", "EG", "337185309694"], "2026-11-16"),
    # Flaschenhals: Datum und Los
    (["07/10/27", "L.2814"], "2027-10-07"),
    # Senf: nur Monat und Jahr, darunter Charge mit Uhrzeit
    (["Fettsauren", "3,8g", "44463995", "06966712828www.thomy.de", "Mindestens haltbarbisEnde:",
      "01.2027", "61520335 07:08"], "2027-01-31"),
    # Sriracha: erstes Trennzeichen verschluckt
    (["SUMYO", "BIS", "2810/2027"], "2027-10-28"),
    (["BIS1", "2810/2027"], "2027-10-28"),
])
def test_mhd_aus_echten_aufdrucken(texte, datum):
    assert mhd_ocr.mhd_finden(texte, HEUTE)["datum"] == datum


@pytest.mark.parametrize("texte, datum", [
    # More-Sirup, Punktdruck, nach "Punkte verdicken"
    (["Mindestens haltbar bis/ LOT:", "Best before / LOT:", "08.01.2028", "DMA0122602", "07:26"], "2028-01-08"),
    # Skyr-Deckel, schraeg
    (["milu", "28.10.26", "SKY", "R", "NATUR"], "2026-10-28"),
    # Creme fraiche: Datum, darunter Charge und Uhrzeit
    (["CREME", "18.10.26", "161F35 21:31 3", "8053971"], "2026-10-18"),
    # Milram: kein Jahr, Hinweis drei Zeilen davor, Spalte "Salz" dazwischen
    (["Bei +8oC mindestens haltbar bis:", "Salz", "Frische Reze", "31.10", "aufmilram.d", "23:15"],
     "2026-10-31"),
    (["MHD 31.10."], "2026-10-31"),
])
def test_mhd_aus_weiteren_aufdrucken(texte, datum):
    assert mhd_ocr.mhd_finden(texte, HEUTE)["datum"] == datum


def test_ohne_jahr_nur_mit_hinweiswort():
    # Naehrwerte sehen aus wie Tag.Monat - ohne Hinweis nie ein MHD.
    assert mhd_ocr.mhd_finden(["Fett 3.5", "Eiweiss 1.12", "22.10"], HEUTE)["datum"] == ""
    # Miree: "1,15 g" als "1.1" gelesen, zufaellig unter "haltbar bis:"
    assert mhd_ocr.mhd_finden(["bis +8Cmindestens", "haltbar bis:", "Eiweis", "1.1", "9"], HEUTE)["datum"] == ""


def test_jahr_wird_ergaenzt():
    from app.services.dates import mhd_ohne_jahr
    assert mhd_ohne_jahr(31, 10, HEUTE) == "2026-10-31"
    assert mhd_ohne_jahr(5, 10, HEUTE) == "2026-10-05"      # eben abgelaufen
    assert mhd_ohne_jahr(5, 3, HEUTE) == "2027-03-05"       # im Maerz naechstes Jahr
    assert mhd_ohne_jahr(29, 2, HEUTE) == ""                 # 2026 und 2027 ohne 29.2.
    assert mhd_ohne_jahr(31, 4, HEUTE) == ""


def test_hinweiswort_schlaegt_spaeteres_datum():
    texte = ["Hergestellt 01.09.2027", "Mindestens haltbar bis", "15.03.2027"]
    assert mhd_ocr.mhd_finden(texte, HEUTE)["datum"] == "2027-03-15"


def test_ohne_hinweis_gewinnt_das_spaetere():
    assert mhd_ocr.mhd_finden(["12.10.2026", "12.04.2027"], HEUTE)["datum"] == "2027-04-12"


@pytest.mark.parametrize("texte", [
    ["61520335 07:08"], ["26042921-2 03:57"], ["L.2814"], ["3,8g", "100g"], ["4337185309694"], [],
])
def test_keine_charge_als_datum(texte):
    assert mhd_ocr.mhd_finden(texte, HEUTE)["datum"] == ""


def test_null_als_o_gelesen():
    assert mhd_ocr.mhd_finden(["O7/1O/27"], HEUTE)["datum"] == "2027-10-07"


def test_foto_endpunkt_lokal_dann_ki(client, monkeypatch):
    bild = base64.b64encode(b"\xff\xd8" + b"x" * 200).decode()
    aufrufe = []

    async def lokal(roh, gruendlich=False):
        aufrufe.append("lokal")
        return {"datum": "", "gelesen": ""}

    async def per_ki(b, typ):
        aufrufe.append("ki")
        return {"datum": "2027-01-31", "gelesen": "01.2027"}

    monkeypatch.setattr(mhd_ocr, "mhd_lesen", lokal)
    monkeypatch.setattr(ki, "mhd_lesen", per_ki)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    # Live-Bild: nur lokal, keine KI-Kosten im Sekundentakt.
    r = client.post("/api/mhd/foto", json={"bild": bild})
    assert r.json()["datum"] == "" and aufrufe == ["lokal"]
    # Ausdruecklich mit KI: erst lokal, dann KI.
    r = client.post("/api/mhd/foto", json={"bild": bild, "ki": True})
    assert r.json() == {"datum": "2027-01-31", "gelesen": "01.2027", "quelle": "ki"}
    assert aufrufe == ["lokal", "lokal", "ki"]
    assert client.post("/api/mhd/foto", json={"bild": "kein base64 !" * 20}).status_code == 422


def test_wege(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(mhd_ocr, "verfuegbar", lambda: True)
    assert client.get("/api/mhd").json() == {"lokal": True, "ki": False}


@pytest.mark.skipif(not mhd_ocr.verfuegbar(), reason="Texterkennung nicht installiert (requirements-ocr.txt)")
def test_echte_texterkennung_an_einem_bild():
    """Durchstich mit dem Modell: ein gedrucktes MHD neben Charge und Uhrzeit."""
    import asyncio
    import io
    from pathlib import Path

    from PIL import Image, ImageDraw, ImageFont

    schrift = next(Path(__file__).parents[1].joinpath("app", "fonts").glob("*.ttf"))
    font = ImageFont.truetype(str(schrift), 44)
    bild = Image.new("RGB", (900, 260), "white")
    zeichnen = ImageDraw.Draw(bild)
    zeichnen.text((30, 30), "Mindestens haltbar bis:", fill="black", font=font)
    zeichnen.text((30, 100), "16.11.2026", fill="black", font=font)
    zeichnen.text((30, 170), "L 2814  06:51", fill="black", font=font)
    puffer = io.BytesIO()
    bild.save(puffer, "JPEG", quality=90)

    ergebnis = asyncio.run(mhd_ocr.mhd_lesen(puffer.getvalue()))
    assert ergebnis["datum"] == "2026-11-16"


def test_kandidaten_zeigen_alle_daten_gewaehltes_zuerst():
    liste = mhd_ocr.kandidaten(["Hergestellt 01.09.2027", "Mindestens haltbar bis", "15.03.2027"], HEUTE)
    assert [k["datum"] for k in liste] == ["2027-03-15", "2027-09-01"]
    assert liste[0]["hinweis"] is True and liste[0]["zeile"] == 2


def test_testseite_zeigt_zwischenschritte(client, monkeypatch):
    bild = base64.b64encode(b"\xff\xd8" + b"x" * 200).decode()

    async def pruefen(roh, gruendlich=True):
        return {"datum": "2027-10-07", "gelesen": "07/10/27", "kandidaten": [], "texte": [], "ms": 5}
    monkeypatch.setattr(mhd_ocr, "pruefen", pruefen)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = client.post("/api/mhd/test", json={"bild": bild, "ki": True})
    assert r.status_code == 200
    assert r.json()["lokal"]["datum"] == "2027-10-07" and r.json()["ki"] is None   # KI nicht eingerichtet
