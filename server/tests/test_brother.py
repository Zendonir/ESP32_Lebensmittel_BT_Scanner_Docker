"""Brother-Drucker am WLAN-Printserver (services/brother.py).

Der Printserver wird mit einem httpx-MockTransport nachgestellt - mit genau
dem Verhalten der Firmware: 202 + Auftragsnummer, ein bekannter X-Job-Key
liefert den vorhandenen Auftrag (200, duplicate), nach einem Neustart kennt er
die Nummer nicht mehr.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.main import app
from app.models import PrintJob
from app.services import brother, settings_store


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------- Rasterdaten
def _treiberdatei(form: str = "g2", tiff: bool = True, zeilen: int = 40) -> bytes:
    """So etwas, wie der Treiber schreibt: Kopf mit ESC i K und ESC i d,
    Leerzeilen als Z, Daten TIFF-gepackt."""
    kopf = (b"\x00" * 200 + b"\x1b@" + b"\x1bia\x01"
            + b"\x1biz" + bytes([0x8E, 0x0B, 51, 26]) + zeilen.to_bytes(4, "little") + b"\x00\x00"
            + b"\x1biM\x00" + b"\x1biK\x08" + b"\x1bid\x00\x00"
            + (b"M\x02" if tiff else b""))
    rumpf = bytearray()
    for y in range(zeilen):
        if y % 4 == 0:
            rumpf += b"Z"
            continue
        zeile = bytes([0xFF if x < y % 56 else 0x00 for x in range(56)])
        daten = brother.hochformat.packbits(zeile) if tiff else zeile
        if form == "g00n":
            rumpf += b"g\x00" + bytes([len(daten)]) + daten
        else:
            rumpf += (b"G" if form == "G2" else b"g") + len(daten).to_bytes(2, "little") + daten
    return kopf + bytes(rumpf) + b"\x1a"


@pytest.mark.parametrize("form,tiff", [("g2", True), ("g00n", False), ("G2", True)])
def test_treiberdatei_wird_gelesen(form, tiff):
    profil = brother.analysieren(_treiberdatei(form, tiff))
    assert profil["zeilenbefehl"] == form
    assert profil["tiff"] is tiff
    assert profil["bytes"] == 56
    assert profil["zeilen"] == 40 and profil["zeilen_im_kopf"] == 40
    assert profil["leerzeile_z"] is True
    assert (profil["breite_mm"], profil["laenge_mm"]) == (51, 26)
    assert profil["ende"] == "1a"
    assert profil["vorschau"].startswith("data:image/png;base64,")
    # Der Kopf endet vor der ersten Rasterzeile - unveraendert uebernommen.
    assert bytes.fromhex(profil["kopf"]).endswith(b"M\x02" if tiff else b"\x1bid\x00\x00")


def test_eigener_auftrag_hat_die_form_des_treibers():
    """Was wir senden, muss sich wieder genauso lesen lassen wie die Vorlage -
    gleicher Kopf, gleicher Zeilenbefehl, gleiche Zeilenzahl."""
    profil = brother.analysieren(_treiberdatei("g2", True, zeilen=200))
    daten = brother.auftrag_bauen({"item": {"label": "LEB000001", "name": "Gulasch",
                                            "expiry_date": "2026-11-30"}}, {"profil": profil})
    wieder = brother.analysieren(daten)
    assert wieder["kopf"] == profil["kopf"]
    assert (wieder["zeilenbefehl"], wieder["tiff"], wieder["bytes"], wieder["zeilen"]) == \
        ("g2", True, 56, 200)
    assert wieder["zeilen_im_kopf"] == 200


def test_standardprofil_ohne_treiberdatei():
    cfg = {"breite_mm": 51, "laenge_mm": 26}
    profil = brother.profil_aus(cfg)
    assert profil["quelle"] == "standard"
    daten = brother.auftrag_bauen({"test": True}, cfg)
    assert daten.startswith(b"\x00" * 200 + b"\x1b@\x1bia\x01\x1biz")
    assert daten.endswith(b"\x1a")
    zeilen = 26 * 8 - 24
    # ESC i z traegt die Zeilenzahl, jede Zeile ist "g 00h 38h" + 56 Bytes.
    z = daten.find(b"\x1biz")
    assert int.from_bytes(daten[z + 7:z + 11], "little") == zeilen
    rumpf = daten[len(bytes.fromhex(profil["kopf"])):-1]
    assert len(rumpf) == zeilen * (3 + 56)
    assert rumpf[:3] == b"g\x00\x38"
    gelesen = brother.analysieren(daten)
    assert gelesen["zeilen"] == zeilen and gelesen["zeilenbefehl"] == "g00n"


def test_drehung_und_spiegeln_aendern_das_bild():
    profil = brother.standard_profil(51, 26)
    item = {"label": "LEB000002", "name": "Kartoffelsuppe", "expiry_date": "2026-11-30"}
    gerade = brother.rasterdaten(brother.etikett_bild(item, profil, {}), profil, {})
    gedreht = brother.rasterdaten(brother.etikett_bild(item, profil, {"drehung": "links"}),
                                  profil, {})
    gespiegelt = brother.rasterdaten(brother.etikett_bild(item, profil, {}), profil,
                                     {"spiegeln": True})
    assert len({gerade, gedreht, gespiegelt}) == 3
    assert len(gerade) == len(gedreht) == len(gespiegelt)


@pytest.mark.parametrize("daten,meldung", [
    (b"\x00" * 300, "ESC i z"),
    (b"\x00" * 200 + b"\x1b@\x1biz" + bytes(10) + b"\x1bi\x7f\x00", "Unbekannter Befehl"),
])
def test_unlesbare_treiberdatei(daten, meldung):
    with pytest.raises(brother.BrotherFehler, match=meldung):
        brother.analysieren(daten)


def test_masse_vom_drucker_ohne_treiberdatei():
    medium = {"media_width_mm": 62, "media_length_mm": 29}
    assert brother.mit_medium({}, medium) == {"breite_mm": 62, "laenge_mm": 29}
    assert brother.mit_medium({"breite_mm": 50}, medium)["breite_mm"] == 50
    mit_profil = {"profil": {"kopf": "00", "zeilen": 10}}
    assert brother.mit_medium(mit_profil, medium) is mit_profil


def test_zustaendig():
    assert not brother.zustaendig({}, "A")
    assert not brother.zustaendig({"aktiv": True, "url": ""}, "A")
    assert brother.zustaendig({"aktiv": True, "url": "http://x"}, "A")
    cfg = {"aktiv": True, "url": "http://x", "haushalt": "Oben, Unten"}
    assert brother.zustaendig(cfg, "Unten") and not brother.zustaendig(cfg, "Keller")
    assert brother.basis_url({"url": "192.168.1.5/"}) == "http://192.168.1.5"


# ------------------------------------------------------- mit Printserver
class Printserver:
    """Nachbau der Druck-API der Firmware (web_server.c / print_queue.c)."""

    def __init__(self):
        self.jobs: dict[int, dict] = {}
        self.naechste = 1
        self.antwort_verlieren = 0      # so viele POSTs annehmen, Antwort "geht verloren"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/print/raw":
            key = request.headers.get("X-Job-Key", "")
            for job in self.jobs.values():
                if key and job["key"] == key:
                    return httpx.Response(200, json={"ok": True, "job_id": job["id"],
                                                     "duplicate": True, "job": job})
            job = {"id": self.naechste, "state": "queued", "message": "queued", "key": key,
                   "name": request.headers.get("X-Job-Name", ""), "size": len(request.content)}
            self.jobs[job["id"]] = job
            self.naechste += 1
            if self.antwort_verlieren:
                self.antwort_verlieren -= 1
                raise httpx.ReadTimeout("Antwort verloren", request=request)
            return httpx.Response(202, json={"ok": True, "job_id": job["id"], "job": job})
        if request.method == "GET" and request.url.path.startswith("/api/jobs/"):
            job = self.jobs.get(int(request.url.path.rsplit("/", 1)[1]))
            if job is None:
                return httpx.Response(404, json={"ok": False, "message": "job not found"})
            return httpx.Response(200, json={"ok": True, "job": job})
        if request.url.path == "/api/status":
            return httpx.Response(200, json={
                "firmware": {"version": "0.4.1-dev"}, "printer": {"connected": True,
                                                                 "product": "TD-2020"},
                "brother": {"status": {"media_width_mm": 51}}, "queue": {"queued": 0},
                "print_api": {"job_key": True}, "wifi": {"ip": "192.168.1.50"}})
        return httpx.Response(404)


@pytest.fixture()
def server(monkeypatch):
    ps = Printserver()
    monkeypatch.setattr(brother, "_transport", httpx.MockTransport(ps))
    # Den Hintergrunddienst nicht wecken - die Tests rufen durchgang() selbst.
    monkeypatch.setattr(brother, "anstossen", lambda: None)
    return ps


@pytest.fixture()
def client(server):
    with TestClient(app) as c:
        c.patch("/api/settings/brother", json={"aktiv": True, "url": "http://printserver",
                                               "haushalt": "", "profil": {}})
        try:
            yield c
        finally:
            c.patch("/api/settings/brother", json={"aktiv": False, "url": ""})


def _durchgang():
    try:
        return _run(brother.durchgang())
    except ConnectionError:
        return "unerreichbar"


def _job(label: str) -> PrintJob:
    async def holen():
        async with session_scope() as session:
            return (await session.execute(
                select(PrintJob).where(PrintJob.label == label)
                .order_by(PrintJob.id.desc()))).scalars().first()
    return _run(holen())


def test_etikett_geht_an_den_brother_und_wird_bestaetigt(client, server):
    r = client.post("/api/labels", json={"name": f"Linsen {uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201, r.text
    label = r.json()["labels"][0]
    job = _job(label)
    assert job.ziel == "brother" and job.status == "queued" and job.device_pk is None
    assert job.payload["item"]["label"] == label

    assert _durchgang() is True
    job = _job(label)
    assert job.status == "sent"
    gesendet = server.jobs[job.payload["brother"]["id"]]
    assert gesendet["name"] == f"Etikett {label}"
    assert gesendet["size"] > 1000

    gesendet["state"] = "printed"
    assert _durchgang() is False
    assert _job(label).status == "done"


def test_verlorene_antwort_druckt_nicht_doppelt(client, server):
    label = client.post("/api/labels", json={"name": "Erbsen"}).json()["labels"][0]
    server.antwort_verlieren = 1
    assert _durchgang() == "unerreichbar"
    job = _job(label)
    assert job.status == "queued" and "Zeitüberschreitung" in job.error

    assert _durchgang() is True                 # derselbe Schluessel noch einmal
    assert len([j for j in server.jobs.values() if j["name"] == f"Etikett {label}"]) == 1
    assert _job(label).status == "sent"


def test_druckerfehler_und_neustart(client, server):
    a = client.post("/api/labels", json={"name": "Bohnen"}).json()["labels"][0]
    b = client.post("/api/labels", json={"name": "Mais"}).json()["labels"][0]
    _durchgang()
    server.jobs[_job(a).payload["brother"]["id"]].update(state="failed",
                                                         message="printer error: Cover open")
    del server.jobs[_job(b).payload["brother"]["id"]]          # Printserver neu gestartet
    _durchgang()
    ja, jb = _job(a), _job(b)
    assert ja.status == "failed" and "Cover open" in ja.error
    assert jb.status == "failed" and "neu gestartet" in jb.error


def test_terminal_fasst_brother_auftraege_nicht_an(client, server, monkeypatch):
    from app.device import workflow
    from app.device.hub import hub

    label = client.post("/api/labels", json={"name": "Reis"}).json()["labels"][0]
    gesendet = []

    class Verbindung:
        async def send(self, msg):
            gesendet.append(msg)
            return True
    monkeypatch.setattr(hub, "get", lambda device_id: Verbindung())
    workflow.session_for("brother-test-terminal").caps = ["raster2", "raw", "baud"]

    async def leeren():
        async with session_scope() as session:
            return await workflow.flush_print_queue(session, "brother-test-terminal",
                                                    resend_in_flight=True)
    _run(leeren())
    assert not any(json.dumps(m).find(label) >= 0 for m in gesendet)
    assert _job(label).status == "queued"


def test_nachdruck_und_testdruck_gehen_an_den_brother(client, server):
    label = client.post("/api/labels", json={"name": "Hirse"}).json()["labels"][0]
    r = client.post(f"/api/labels/reprint/{label}")
    assert r.status_code == 200 and r.json()["ziel"] == "brother"
    r = client.post("/api/brother/test")
    assert r.status_code == 200
    _durchgang()
    _durchgang()
    async def zaehlen():
        async with session_scope() as session:
            return (await session.execute(select(PrintJob).where(
                PrintJob.ziel == "brother", PrintJob.label.in_((label, "TEST")),
                PrintJob.status == "sent"))).scalars().all()
    assert len(_run(zaehlen())) >= 3


def test_treiberdatei_hochladen_und_zustand(client, server):
    r = client.post("/api/brother/treiberdatei",
                    files={"datei": ("etikett.prn", _treiberdatei(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["quelle"] == "treiber" and "kopf" not in r.json()

    z = client.get("/api/brother").json()
    assert z["printserver"]["erreichbar"] and z["printserver"]["job_key"]
    assert z["profil"]["datei"] == "etikett.prn"
    assert z["masse_mm"] == {"breite": 51.0, "laenge": 5.0}
    assert z["vorschau"].startswith("data:image/png")

    r = client.post("/api/brother/treiberdatei",
                    files={"datei": ("kaputt.prn", b"\x00" * 100, "application/octet-stream")})
    assert r.status_code == 422
    assert client.delete("/api/brother/treiberdatei").json()["ok"]
    assert client.get("/api/brother").json()["profil"]["quelle"] == "standard"


def test_einstellungen_haben_vorgaben():
    for schluessel in ("aktiv", "url", "haushalt", "breite_mm", "laenge_mm", "drehung",
                       "versatz_mm", "spiegeln", "profil"):
        assert schluessel in settings_store.DEFAULTS["brother"]
    assert settings_store.DEFAULTS["brother"]["drehung"] in brother.DREHUNGEN
