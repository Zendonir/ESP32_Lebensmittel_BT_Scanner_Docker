"""Freitext-Etiketten: Zeichnen, Vorschau, Druckauftraege an Brother (WLAN/USB)."""

import asyncio
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.device import workflow
from app.main import app
from app.models import Device, PrintJob
from app.services import brother, freitext


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------- Zeichnen
def test_teile_fett():
    assert freitext._teile("a **b** c", False) == [("a ", False), ("b", True), (" c", False)]
    assert freitext._teile("**ganz**", False) == [("ganz", True)]
    assert freitext._teile("x", True) == [("x", True)]


def test_zeilen_stile():
    z = freitext.zeilen_aus({"text": "\n\nKopf\n\n# Groß\nRest\n\n", "stil": "titel"})
    assert [bool(x.leer) for x in z] == [False, True, False, False]   # Rand-Leerzeilen weg
    assert z[0].faktor == 1.6 and all(f for _, f in z[0].teile)      # Titel gross und fett
    assert z[2].faktor == 1.5                                          # "# " = Ueberschrift
    z = freitext.zeilen_aus({"text": "A", "stil": "balken"})
    assert z[0].balken
    z = freitext.zeilen_aus({"text": "A", "stil": "datum"})
    assert z[-1].teile[0][0] == date.today().strftime("%d.%m.%Y")
    assert freitext.zeilen_aus({"text": "  \n "}) == []


@pytest.mark.parametrize("stil", list(freitext.STILE))
def test_alle_stile_zeichnen(stil):
    bild, info = freitext.zeichnen({"text": "Keller\n**Werkzeug** & Co", "stil": stil}, 432, 224)
    assert bild.size == (432, 224) and not info["ueberlauf"] and info["groesse"] > 15
    # Es ist etwas Schwarzes darauf
    assert bild.getextrema()[0] == 0


def test_schrift_waechst_und_ueberlauf():
    kurz = freitext.zeichnen({"text": "Hi"}, 432, 224)[1]["groesse"]
    lang = freitext.zeichnen({"text": "Ein deutlich längerer Text in einer Zeile"}, 432, 224)[1]["groesse"]
    assert kurz > lang
    _, info = freitext.zeichnen({"text": "x" * 300}, 432, 224)
    assert info["ueberlauf"] and info["groesse"] == freitext.MIN_PT
    _, info = freitext.zeichnen({"text": ""}, 432, 224)
    assert info["leer"]


def test_brother_auftrag_und_vorschau():
    cfg = {"etikett": "eigen-57x32"}
    daten = brother.auftrag_bauen({"freitext": {"text": "Test", "stil": "rahmen"}}, cfg)
    profil = brother.profil_aus(cfg)
    assert daten.startswith(bytes.fromhex(profil["kopf"])[:20])
    v = brother.freitext_vorschau({"text": "Test"}, cfg)
    assert v["png"].startswith("data:image/png;base64,") and v["breite"] > v["hoehe"]
    # Masse der gewaehlten Sorte (57 x 32 mm, gedruckt 54 x 28 mm)
    assert (v["breite_mm"], v["hoehe_mm"]) == (54.0, 28.0)
    # Hochkant gelesen: Hoehe und Breite getauscht
    h = brother.freitext_vorschau({"text": "Test", "drehung": "links"}, cfg)
    assert (h["breite"], h["hoehe"]) == (v["hoehe"], v["breite"])


# ------------------------------------------------------------------ API
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(brother, "anstossen", lambda: None)
    with TestClient(app) as c:
        c.patch("/api/settings/brother", json={"drucker": [
            {"id": "ft1", "name": "Flur", "url": "http://printserver-ft", "haushalt": "",
             "aktiv": True, "etikett": "eigen-57x32"}]})
        try:
            yield c
        finally:
            c.patch("/api/settings/brother", json={"drucker": []})


def _jobs(label="FREITEXT"):
    async def holen():
        async with session_scope() as session:
            return list((await session.execute(select(PrintJob).where(
                PrintJob.label == label, PrintJob.status == "queued"))).scalars())
    return _run(holen())


def _aufraeumen():
    async def weg():
        async with session_scope() as session:
            for j in (await session.execute(select(PrintJob).where(
                    PrintJob.label == "FREITEXT"))).scalars():
                await session.delete(j)
            await session.commit()
    _run(weg())


def test_uebersicht_vorschau_und_wlan_druck(client):
    _aufraeumen()
    d = client.get("/api/freitext").json()
    assert {"ziel": "brother:ft1", "art": "wlan", "name": "Flur"}.items() <= next(
        z for z in d["ziele"] if z["ziel"] == "brother:ft1").items()
    assert {s["key"] for s in d["stile"]} == set(freitext.STILE)

    v = client.post("/api/freitext/vorschau", json={"text": "Hallo", "ziel": "brother:ft1"}).json()
    assert v["png"].startswith("data:image/png") and v["breite_mm"] > 0

    assert client.post("/api/freitext/drucken", json={"text": "  ", "ziel": "brother:ft1"}).status_code == 422
    assert client.post("/api/freitext/drucken", json={"text": "x", "ziel": "brother:gibtsnicht"}).status_code == 404

    vorher = client.get("/api/labels/roll").json()
    r = client.post("/api/freitext/drucken", json={"text": "Hallo **Welt**", "stil": "balken",
                                                   "ziel": "brother:ft1", "anzahl": 3})
    assert r.status_code == 200 and len(r.json()["auftraege"]) == 3
    # Die drei Etiketten zaehlen auf der Rolle
    nachher = client.get("/api/labels/roll").json()
    assert nachher["used"] == vorher["used"] + 3
    jobs = _jobs()
    assert len(jobs) == 3
    assert all(j.ziel == brother.ZIEL and j.payload["drucker"] == "ft1"
               and j.payload["freitext"]["stil"] == "balken" for j in jobs)
    _aufraeumen()


def test_usb_terminal_druck(client):
    _aufraeumen()
    geraet = f"ft-{uuid.uuid4().hex[:6]}"

    async def anlegen():
        async with session_scope() as session:
            session.add(Device(device_id=geraet, name="Küche", drucker="usb"))
            await session.commit()
    _run(anlegen())
    ziele = client.get("/api/freitext").json()["ziele"]
    usb = next(z for z in ziele if z["ziel"] == f"terminal:{geraet}")
    assert usb["art"] == "usb" and usb["online"] is False

    r = client.post("/api/freitext/drucken", json={"text": "Regal 3", "ziel": f"terminal:{geraet}"})
    assert r.status_code == 200 and r.json()["online"] is False
    job = _jobs()[0]
    assert job.ziel == "" and job.device_pk is not None and job.payload["freitext"]["text"] == "Regal 3"

    # Der USB-Weg zeichnet daraus Brother-Raster
    async def zeichnen():
        async with session_scope() as session:
            return await workflow._usb_daten(session, job, {"etikett": "eigen-57x32"}, {})
    assert isinstance(_run(zeichnen()), bytes)
    _aufraeumen()


def test_serieller_drucker_lehnt_freitext_ab(monkeypatch):
    """Ein ESC/POS-Terminal kann kein Freitext-Bild - der Auftrag scheitert sauber."""
    from app.db import init_db
    from app.device.hub import hub

    gesendet = []

    class Verbindung:
        async def send(self, msg):
            gesendet.append(msg)
            return True

    async def scenario():
        await init_db()
        geraet = f"ser-{uuid.uuid4().hex[:6]}"
        async with session_scope() as session:
            for j in (await session.execute(select(PrintJob))).scalars():
                await session.delete(j)
            d = Device(device_id=geraet, name="Alt", drucker="seriell")
            session.add(d)
            await session.flush()
            job = PrintJob(label="FREITEXT", status="queued", device_pk=d.id,
                           payload={"freitext": {"text": "x"}})
            session.add(job)
            await session.commit()
        monkeypatch.setattr(hub, "get", lambda device_id: Verbindung())
        sess = workflow.session_for(geraet)
        sess.caps, sess.drucker = {"raster2"}, "seriell"
        try:
            async with session_scope() as session:
                await workflow.flush_print_queue(session, geraet)
            async with session_scope() as session:
                frisch = await session.get(PrintJob, job.id)
                return frisch.status, frisch.error
        finally:
            workflow.drop_session(geraet)

    status, fehler = _run(scenario())
    assert status == "failed" and "Brother" in fehler and not gesendet
