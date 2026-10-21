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
    # ESC i U mit 127 Bytes Medieninformation - mit einem falschen "ESC i z" darin.
    medien = (b"\x1biz" + bytes(range(124)))[:127]
    kopf = (b"\x00" * 200 + b"\x1b@" + b"\x1bia\x01" + b"\x1biUw\x01" + medien
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
    assert daten.startswith(b"\x00" * 200 + b"\x1b@\x1bia\x01\x1biz\x8e\x0b\x33\x1a")
    assert b"\x1biM\x00\x1bid\x00\x00M\x00" in daten[:260]
    assert daten.endswith(b"\x1a")
    zeilen = 157                    # Referenz 2.3.2: 51 x 26 mm -> 157 Druckzeilen
    # ESC i z traegt die Zeilenzahl, jede Zeile ist "g 00h 38h" + 56 Bytes.
    z = daten.find(b"\x1biz")
    assert int.from_bytes(daten[z + 7:z + 11], "little") == zeilen
    rumpf = daten[len(bytes.fromhex(profil["kopf"])):-1]
    assert len(rumpf) == zeilen * (3 + 56)
    assert rumpf[:3] == b"g\x00\x38"
    gelesen = brother.analysieren(daten)
    assert gelesen["zeilen"] == zeilen and gelesen["zeilenbefehl"] == "g00n"


def test_masse_nach_referenz():
    assert brother.masse(brother.standard_profil(51, 26), {}) == (382, 157)
    assert brother.masse(brother.standard_profil(30, 30), {}) == (216, 192)
    assert brother.masse(brother.standard_profil(60, 60), {}) == (448, 432)
    endlos = brother.standard_profil(58, 0)
    assert bytes.fromhex(endlos["kopf"])[206:213] == b"\x1biz\x86\x0a\x3a\x00"
    assert b"\x1bid\x18\x00" in bytes.fromhex(endlos["kopf"])     # 3 mm Rand


def test_tiff_zeile_nie_laenger_als_57_bytes():
    standard = brother.standard_profil(51, 26)
    profil = {**standard, "tiff": True, "leerzeile_z": True,
              "kopf": standard["kopf"].replace("4d00", "4d02")}    # M 02h: TIFF
    import random
    zufall = random.Random(7)
    bild = brother.Image.new("1", (448, 157), 1)       # volle Kopfbreite
    for x in range(448):                # Rauschen: PackBits waere laenger als 56
        for y in range(0, 157, 2):
            if zufall.random() < 0.5:
                bild.putpixel((x, y), 0)
    daten = brother.rasterdaten(bild, profil, {})
    gelesen = brother.analysieren(daten)
    assert gelesen["bytes"] == 56 and gelesen["zeilen"] == 157
    i = len(bytes.fromhex(profil["kopf"]))
    assert daten[i:i + 3] == b"g\x00\x39"           # 57 Bytes: Kopfbyte 37h + 56


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
    endlos = brother.mit_medium({}, {"media_width_mm": 58, "media_length_mm": 0,
                                     "media_type": "continuous_tape"})
    assert endlos["endlos"] and brother.profil_aus(endlos)["laenge_mm"] == 0
    mit_profil = {"profil": {"kopf": "00", "zeilen": 10}}
    assert brother.mit_medium(mit_profil, medium) is mit_profil


def test_medium_abweichung():
    etiketten = {"typ": "die_cut_labels", "breite_mm": 51, "laenge_mm": 26}
    gestanzt = {"media_type": "die_cut_labels", "media_width_mm": 51, "media_length_mm": 26}
    assert brother.medium_abweichung(etiketten, gestanzt) == ""
    assert brother.medium_abweichung(etiketten, None) == ""
    assert "50 × 30" in brother.medium_abweichung(
        etiketten, {**gestanzt, "media_width_mm": 50, "media_length_mm": 30})
    assert "Endlosband" in brother.medium_abweichung(
        etiketten, {"media_type": "continuous_tape", "media_width_mm": 58})
    assert brother.medium_abweichung(etiketten, {"media_type": "none"}) == \
        "Keine Etiketten eingelegt"
    # Standardprofil kuendigt gestanzte Etiketten an - nur dann setzt der
    # Drucker jedes Etikett an der Luecke an.
    profil = brother.standard_profil(51, 26)
    assert brother.erwartet(profil, {}) == etiketten
    assert bytes.fromhex(profil["kopf"])[210] == 0x0B         # n2: Etiketten


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
        self.medium = {"media_width_mm": 51}   # gemerkter Stand (cached)
        self.medium_live = None                # Antwort auf eine frische Abfrage
        self.drucker_an = True
        self.einstellungen = {"command_mode": 3, "print_priority": 0, "rotate": 0,
                              "stop_position": 0, "recovery": 0}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/print/raw":
            key = request.headers.get("X-Job-Key", "")
            for job in self.jobs.values():
                if key and job["key"] == key:
                    return httpx.Response(200, json={"ok": True, "job_id": job["id"],
                                                     "duplicate": True, "job": job})
            job = {"id": self.naechste, "state": "queued", "message": "queued", "key": key,
                   "attempts": 0,
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
        if request.url.path == "/api/printer/settings":
            if request.method == "POST":
                for teil in request.content.decode().split("&"):
                    k, v = teil.split("=")
                    self.einstellungen[k] = int(v)
            optionen = {"command_mode": [(0, "ESC/P"), (1, "Raster"), (3, "P-touch Template")]}
            return httpx.Response(200, json={"ok": True, "settings": [
                {"key": k, "name": k, "value": v,
                 "options": [{"value": w, "label": t} for w, t in
                             optionen.get(k, [(0, "Off"), (1, "On")])]}
                for k, v in self.einstellungen.items()]})
        if request.url.path == "/api/printer/status":
            if self.medium_live is None:
                return httpx.Response(409, json={"ok": False})
            self.medium = self.medium_live
            return httpx.Response(200, json={"ok": True, "printer": {"status": self.medium}})
        if request.url.path == "/api/status":
            return httpx.Response(200, json={
                "firmware": {"version": "0.4.1-dev"},
                "printer": {"connected": self.drucker_an, "ready": self.drucker_an,
                            "product": "TD-2020"},
                "brother": {"status": self.medium}, "queue": {"queued": 0},
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
            _offene_brother_auftraege_abschliessen()


def _offene_brother_auftraege_abschliessen():
    """Jeder Test hat seinen eigenen nachgestellten Printserver - offene
    Auftraege eines Tests kaemen beim naechsten sonst ungedruckt zurueck in
    die Schlange und belegten dort die Plaetze (MAX_UNTERWEGS)."""
    async def schliessen():
        async with session_scope() as session:
            for job in (await session.execute(select(PrintJob).where(
                    PrintJob.ziel == "brother", PrintJob.status.in_(("queued", "sent"))))).scalars():
                job.status = "done"
            await session.commit()
    _run(schliessen())


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
    server.jobs[_job(a).payload["brother"]["id"]].update(state="failed", attempts=1,
                                                         message="printer error: Cover open")
    # b war schon beim Drucker, dann startet der Printserver neu: Ergebnis offen.
    server.jobs[_job(b).payload["brother"]["id"]].update(state="printing", attempts=1)
    _durchgang()
    del server.jobs[_job(b).payload["brother"]["id"]]
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
    async def zaehlen():
        async with session_scope() as session:
            return [j.status for j in (await session.execute(select(PrintJob).where(
                PrintJob.ziel == "brother", PrintJob.label.in_((label, "TEST")),
                PrintJob.status.in_(("queued", "sent"))))).scalars()]
    stand = _run(zaehlen())
    # Etikett, Nachdruck und Testetikett - hoechstens zwei zugleich beim Printserver.
    assert len(stand) == 3 and stand.count("sent") == brother.MAX_UNTERWEGS


def test_treiberdatei_hochladen_und_zustand(client, server):
    r = client.post("/api/brother/treiberdatei",
                    files={"datei": ("etikett.prn", _treiberdatei(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["quelle"] == "treiber" and "kopf" not in r.json()

    z = client.get("/api/brother").json()
    assert z["printserver"]["erreichbar"] and z["printserver"]["job_key"]
    assert z["profil"]["datei"] == "etikett.prn"
    assert z["masse_mm"] == {"breite": 47.8, "laenge": 5.0}    # Druckbereich 382 Pins
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


def test_falsche_rolle_haelt_zurueck_bis_sie_passt(client, server):
    client.patch("/api/settings/brother", json={"breite_mm": 51, "laenge_mm": 26})
    try:
        server.medium = {"media_type": "die_cut_labels", "media_width_mm": 50,
                         "media_length_mm": 30}
        label = client.post("/api/labels", json={"name": "Graupen"}).json()["labels"][0]
        assert _durchgang() == "unerreichbar"            # seltener nachsehen
        job = _job(label)
        assert job.status == "queued" and "50 × 30" in job.error
        assert not server.jobs
        assert "50 × 30" in client.get("/api/brother").json()["medium_hinweis"]

        # Rolle gewechselt: der gemerkte Stand ist alt, die frische Abfrage zaehlt.
        server.medium_live = {"media_type": "die_cut_labels", "media_width_mm": 51,
                              "media_length_mm": 26}
        assert _durchgang() is True
        assert _job(label).status == "sent" and len(server.jobs) == 1
    finally:
        client.patch("/api/settings/brother", json={"breite_mm": 0, "laenge_mm": 0})


# ----------------------------------------------------- Ziel und Standardziel
def test_standardziel_und_wunsch():
    cfg = {"aktiv": True, "url": "http://x"}
    assert brother.standardziel(cfg, "A") == "brother"
    assert brother.standardziel({**cfg, "standardziel": "terminal"}, "A") == "terminal"
    assert brother.standardziel({}, "A") == "terminal"          # kein Brother eingerichtet
    assert brother.an_brother(cfg, "A") is True
    assert brother.an_brother(cfg, "A", "terminal") is False
    assert brother.an_brother({**cfg, "standardziel": "terminal"}, "A", "brother") is True
    assert brother.an_brother({}, "A", "brother") is False      # API lehnt vorher ab


def test_drucken_an_waehlen_und_standardziel(client, server):
    z = client.get("/api/labels/ziele").json()
    assert z["standard"] == "brother" and z["brother"] is True

    angelegt = []

    def ziel_von(body):
        r = client.post("/api/labels", json={"name": f"Bohne {uuid.uuid4().hex[:5]}", **body})
        assert r.status_code == 201, r.text
        angelegt.append(r.json()["labels"][0])
        return _job(angelegt[-1]).ziel

    assert ziel_von({}) == "brother"                    # Standardziel
    assert ziel_von({"ziel": "terminal"}) == ""         # ausdruecklich Terminal
    client.patch("/api/settings/brother", json={"standardziel": "terminal"})
    try:
        assert client.get("/api/labels/ziele").json()["standard"] == "terminal"
        assert ziel_von({}) == ""                       # neues Standardziel
        assert ziel_von({"ziel": "brother"}) == "brother"
        # Nachdruck folgt dem Standardziel, auf Wunsch dem Brother.
        label = client.post("/api/labels", json={"name": "Nudeln"}).json()["labels"][0]
        angelegt.append(label)
        assert client.post(f"/api/labels/reprint/{label}").json().get("ziel") is None
        assert client.post(f"/api/labels/reprint/{label}?ziel=brother").json()["ziel"] == "brother"
    finally:
        client.patch("/api/settings/brother", json={"standardziel": "brother"})

        # Offene Terminal-Auftraege nicht den folgenden Tests (Druckziel) hinterlassen.
        async def aufraeumen():
            async with session_scope() as session:
                for job in (await session.execute(select(PrintJob).where(
                        PrintJob.label.in_(angelegt), PrintJob.ziel == ""))).scalars():
                    await session.delete(job)
                await session.commit()
        _run(aufraeumen())


def test_brother_waehlen_ohne_brother_lehnt_ab():
    with TestClient(app) as c:
        assert c.get("/api/labels/ziele").json()["standard"] == "terminal"
        r = c.post("/api/labels", json={"name": "Reis", "ziel": "brother"})
        assert r.status_code == 409 and "nicht eingerichtet" in r.text
        # Nachdruck ebenso: kein stiller Wechsel auf das Terminal.
        label = c.post("/api/labels", json={"name": "Reis", "print": False}).json()["labels"][0]
        r = c.post(f"/api/labels/reprint/{label}?ziel=brother")
        assert r.status_code == 409 and "nicht eingerichtet" in r.text
        assert c.post("/api/labels", json={"name": "Reis", "ziel": "quatsch"}).status_code == 422


# ------------------------------------------------- Haushalt wie bei Terminals
def test_gehoert_zu():
    assert brother.gehoert_zu({}, "A") and brother.gehoert_zu({"haushalt": "A"}, None)
    assert brother.gehoert_zu({"haushalt": "A"}, "A")
    assert not brother.gehoert_zu({"haushalt": "A"}, "B")
    assert not brother.zustaendig({"aktiv": True, "url": "http://x", "haushalt": "A"}, "B")


def test_brother_einem_haushalt_zuweisen(client, server):
    tag = uuid.uuid4().hex[:6]
    a = client.post("/api/households", json={"name": f"Oben-{tag}"}).json()["id"]
    b = client.post("/api/households", json={"name": f"Unten-{tag}"}).json()["id"]
    try:
        client.patch("/api/settings/brother", json={"haushalt": f"Oben-{tag}"})
        bei_a, bei_b = {"X-Haushalt": str(a)}, {"X-Haushalt": str(b)}

        # Geraeteliste: nur der Haushalt, dem er gehoert (Verwaltung sieht ihn immer).
        assert client.get("/api/brother/geraet", headers=bei_a).json()["sichtbar"]
        assert client.get("/api/brother/geraet", headers=bei_b).json()["sichtbar"] is False
        assert client.get("/api/brother/geraet").json()["haushalt"] == f"Oben-{tag}"
        assert client.get("/api/brother", headers=bei_b).json() == {"sichtbar": False}

        # Drucken: nur der eigene Haushalt hat ihn zur Wahl und als Standardziel.
        assert client.get("/api/labels/ziele", headers=bei_a).json()["brother"] is True
        assert client.get("/api/labels/ziele", headers=bei_b).json()["standard"] == "terminal"
        r = client.post("/api/labels", json={"name": "Hafer", "ziel": "brother"}, headers=bei_b)
        assert r.status_code == 409

        # Umbenennen zieht die Zuordnung nach.
        assert client.patch(f"/api/households/{a}", json={"name": f"Dach-{tag}"}).status_code == 200
        assert client.get("/api/brother/geraet").json()["haushalt"] == f"Dach-{tag}"
    finally:
        client.patch("/api/settings/brother", json={"haushalt": ""})


def test_geraeteliste_zeigt_nur_terminals_des_haushalts():
    tag = uuid.uuid4().hex[:6]
    with TestClient(app) as c:
        a = c.post("/api/households", json={"name": f"Links-{tag}"}).json()["id"]
        c.post("/api/households", json={"name": f"Rechts-{tag}"})

        async def anlegen():
            from app.models import Device
            async with session_scope() as session:
                session.add(Device(device_id=f"t-l-{tag}", household=f"Links-{tag}"))
                session.add(Device(device_id=f"t-r-{tag}", household=f"Rechts-{tag}"))
                await session.commit()
        _run(anlegen())
        try:
            eigene = {d["device_id"] for d in c.get("/api/devices", headers={"X-Haushalt": str(a)}).json()}
            assert eigene == {f"t-l-{tag}"}
            alle = {d["device_id"] for d in c.get("/api/devices").json()}
            assert {f"t-l-{tag}", f"t-r-{tag}"} <= alle
        finally:
            c.delete(f"/api/devices/t-l-{tag}")
            c.delete(f"/api/devices/t-r-{tag}")



# ------------------------------------------------- Drucker aus: alles wartet
def test_drucker_aus_alles_wartet_und_druckt_danach(client, server):
    server.drucker_an = False
    labels = client.post("/api/labels", json={"name": "Linsen", "count": 4}).json()["labels"]
    assert _durchgang() == "unerreichbar"            # seltener nachsehen
    assert not server.jobs                            # nichts abgeschickt
    assert {(_job(lb).status, _job(lb).error) for lb in labels} == {("queued", brother.DRUCKER_AUS)}

    # Eingeschaltet: alle gehen raus, hoechstens zwei zugleich beim Printserver.
    server.drucker_an = True
    for _ in range(4):
        _durchgang()
        for job in server.jobs.values():
            job["state"], job["attempts"] = "printed", 1
    assert all(_job(lb).status == "done" for lb in labels)
    assert len(server.jobs) == 4


def test_printserver_gibt_auf_ohne_zu_drucken_etikett_wartet_weiter(client, server):
    label = client.post("/api/labels", json={"name": "Graupen"}).json()["labels"][0]
    _durchgang()
    erster = server.jobs[_job(label).payload["brother"]["id"]]
    # Nach 10 min ohne Drucker gibt der Printserver auf - gedruckt wurde nichts.
    erster.update(state="failed", message="printer not connected", attempts=0)
    _durchgang()                        # zurueck in die Schlange und gleich wieder hinaus
    job = _job(label)
    zweiter = server.jobs[job.payload["brother"]["id"]]
    assert job.status == "sent" and job.payload["versand"] == 1
    assert zweiter is not erster and zweiter["key"] != erster["key"]   # kein "duplicate"

    # Printserver neu gestartet, waehrend der Auftrag dort nur wartete: noch einmal.
    server.jobs.clear()
    _durchgang()
    job = _job(label)
    assert job.status == "sent" and job.payload["versand"] == 2
    assert len(server.jobs) == 1



# --------------------------------------------- Medium und Druckereinstellungen
def test_medium_endlos_mit_laenge_je_etikett():
    endlos = {"media_type": "continuous_tape", "media_width_mm": 58, "media_length_mm": 0}
    # Drucker meldet Endlosband, eingetragen 40 mm: Endlosband mit 40 mm je Etikett
    # (vorher wurde daraus ein 40-mm-Etikett, und die Rolle "passte nicht").
    cfg = brother.mit_medium({"laenge_mm": 40}, endlos)
    profil = brother.profil_aus(cfg)
    assert profil["laenge_mm"] == 0 and profil["zeilen"] == 320 - 48
    assert brother.medium_abweichung(brother.erwartet(profil, cfg), endlos) == ""
    assert bytes.fromhex(profil["kopf"])[210] == 0x0A                   # n2: Endlosband

    # Ausdruecklich "Endlosband" ohne Druckermeldung.
    profil = brother.profil_aus({"medium": "endlos", "breite_mm": 58})
    assert profil["laenge_mm"] == 0 and profil["zeilen"] == 240 - 48

    # Ausdruecklich "Etiketten", der Drucker steht aber auf Endlosband: anhalten.
    cfg = brother.mit_medium({"medium": "etiketten", "breite_mm": 50, "laenge_mm": 30}, endlos)
    soll = brother.erwartet(brother.profil_aus(cfg), cfg)
    assert soll["typ"] == "die_cut_labels"
    assert "Endlosband" in brother.medium_abweichung(soll, endlos)


def test_druckereinstellungen_ueber_den_printserver(client, server):
    r = client.get("/api/brother/druckereinstellungen")
    assert r.status_code == 200, r.text
    modus = next(e for e in r.json()["einstellungen"] if e["key"] == "command_mode")
    assert modus["name"] == "Befehlsmodus" and modus["wert"] == 3 and modus["empfohlen"] == 1

    r = client.patch("/api/brother/druckereinstellungen", json={"command_mode": 1})
    assert r.status_code == 200 and server.einstellungen["command_mode"] == 1
    assert client.patch("/api/brother/druckereinstellungen",
                        json={"druckdichte": 2}).status_code == 422
