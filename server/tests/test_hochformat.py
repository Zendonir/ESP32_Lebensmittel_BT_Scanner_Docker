"""Hochformat: das Etikett als Bild, um 90 Grad gedreht.

Geprueft wird, was man am Drucker sonst erst nach einer halben Rolle merkt:
dass ein Etikett genau eine Teilung Papier verbraucht, dass die Firmware die
Streifen wieder auspacken kann, dass die Nachricht unter ihrer Grenze bleibt,
und dass kein Name stillschweigend abgeschnitten wird.

Dass der QR-Code im gedruckten Bild lesbar ist, laesst sich hier nicht ohne
Browser pruefen; das ist beim Bau mit ZXing (web/js/vendor) nachgeprueft.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random

import pytest
from PIL import Image
from sqlalchemy import select

from app.db import init_db, session_scope
from app.device import workflow
from app.device.hub import hub
from app.models import PrintJob
from app.services import hochformat, labels

CFG = {
    "label_layout": "hochformat",
    "label_width_mm": 50,
    "label_height_mm": 30,
    "label_feed_edge": "hoehe",
    "paper_chars": 32,
}
ITEM = {
    "label": "LEB000123", "name": "Schweinefilet", "subcategory": "Schwein",
    "expiry_date": "2026-09-30", "added_date": "2026-08-12",
    "quantity": 500, "unit": "g", "location": "Kühlschrank",
}


# ------------------------------------------------------------------ PackBits
def _zufall(laenge: int) -> bytes:
    # Ein Generator fuer alle Bytes - einer je Byte lieferte immer denselben
    # Wert, und der "Zufall" war ein einziger langer Lauf.
    rng = random.Random(1)
    return bytes(rng.choice(b"\x00\x00\x00\xff\x81") for _ in range(laenge))


@pytest.mark.parametrize("daten", [
    b"",
    b"\x00",
    b"\x00" * 1000,                         # laenger als ein Lauf (128)
    bytes(range(256)) * 3,                  # nie zwei gleiche hintereinander
    b"\xff\xff\x00\x00\x00\x12\x12\x12\x12\x34",
    _zufall(5000),
])
def test_packbits_hin_und_zurueck(daten):
    gepackt = hochformat.packbits(daten)
    assert hochformat.unpackbits(gepackt, len(daten)) == daten


def test_packbits_spart_bei_einem_etikett_deutlich():
    roh = sum(
        ((b["w"] + 7) // 8) * b["h"]
        for b in labels.render_label(ITEM, CFG)["blocks"] if b["t"] == "raster"
    )
    gepackt = sum(
        len(base64.b64decode(b["d"]))
        for b in labels.render_label(ITEM, CFG)["blocks"] if b["t"] == "raster"
    )
    assert gepackt < roh / 2


# ------------------------------------------------------------------ Teilung
@pytest.mark.parametrize("totbereich,rueckzug,ende", [
    (0, 0, "feed"), (4, 0, "feed"), (8, 32, "feed"), (3.5, 0, "formfeed"),
])
def test_ein_etikett_verbraucht_genau_eine_teilung(totbereich, rueckzug, ende):
    payload = labels.render_label(ITEM, {
        **CFG, "label_dead_zone_mm": totbereich, "backfeed_dots": rueckzug, "label_end": ende,
    })
    # Die ganze Teilung, nicht nur der bedruckbare Teil: der Totbereich wird
    # mit durchgeschoben, ein Rueckzug zaehlt negativ.
    assert labels.total_dots(payload["blocks"]) == 30 * labels.DOTS_PER_MM
    assert payload["overflow"] == 0


def test_rasterbloecke_passen_zu_ihren_daten():
    """Die Firmware entpackt genau w/8 * h Bytes. Ein Block, dessen `h`
    nachtraeglich aufgerundet wird, liesse sie auf Daten warten, die nie kommen."""
    for block in labels.render_label(ITEM, CFG)["blocks"]:
        if block["t"] != "raster":
            continue
        assert block["h"] % hochformat.BAND == 0, "ESC * 33 druckt nur ganze 24er Baender"
        assert block["mode"] in hochformat.RASTER_MODI and block["z"] == "rle"
        roh = hochformat.unpackbits(base64.b64decode(block["d"]), 10**6)
        assert len(roh) == (block["w"] // 8) * block["h"]


def test_nachricht_bleibt_unter_der_grenze_der_firmware():
    """WebSockets nimmt hoechstens 15 KB je Nachricht, die Firmware 14 KB."""
    lang = {**ITEM, "name": "Bolognese mit extra viel Hackfleisch und Rotwein (selbstgemacht)",
            "location": "Gefrierschrank unten links, hinterste Schublade"}
    for item in (ITEM, lang):
        for groesse in ("klein", "mittel", "gross"):
            payload = labels.render_label(item, {**CFG, "label_code_size": groesse})
            nachricht = json.dumps({"t": "print", "job": 99999, "data": payload})
            assert len(nachricht) < 12_000, (groesse, len(nachricht))


def test_braucht_die_faehigkeit_und_dreht_keine_schrift():
    payload = labels.render_label(ITEM, {**CFG, "label_rotate": True})
    assert payload["braucht"] == labels.BILD_FAEHIGKEIT
    assert payload["rotate"] is False, "ESC V wuerde Text drehen - hier gibt es keinen"


def test_andere_layouts_brauchen_nichts_neues():
    assert "braucht" not in labels.render_label(ITEM, {**CFG, "label_layout": "standard"})


# ------------------------------------------------------------------ Drehung
def test_drehrichtung_legt_die_oberkante_fest():
    """"links": Oberkante des Etiketts am linken Ende des Druckkopfs."""
    bild = Image.new("1", (40, 80), 1)
    bild.putpixel((0, 0), 0)                      # oben links im Hochformat
    links = hochformat.gedreht(bild, "links")
    rechts = hochformat.gedreht(bild, "rechts")
    assert links.size == rechts.size == (80, 40)  # Breite = Druckkopf
    assert links.getpixel((0, 39)) == 0
    assert rechts.getpixel((79, 0)) == 0


# ------------------------------------------------------- nichts abschneiden
def _text(item: dict) -> str:
    return " ".join(hochformat.textzeilen(item, {}, 216, 384))


def test_langer_name_steht_vollstaendig_da():
    name = "Bolognese mit extra viel Hackfleisch (selbstgemacht)"
    text = _text({**ITEM, "name": name, "subcategory": ""})
    for wort in name.split():
        assert wort in text
    assert "…" not in text


def test_kein_wort_wird_getrennt_wenn_es_kleiner_passt():
    for name in ("Schweinefilet", "Rinderrouladen", "Hähnchenbrustfilet"):
        zeilen = hochformat.textzeilen({**ITEM, "name": name, "subcategory": ""}, {}, 216, 384)
        assert name in zeilen, zeilen


def test_ueberlanges_wort_wird_getrennt_nicht_gekappt():
    wort = "Donaudampfschifffahrtsgesellschaftskapitänsmütze"
    zeilen = hochformat.textzeilen({**ITEM, "name": wort, "subcategory": ""}, {}, 216, 384)
    zusammen = "".join(z.rstrip("-") for z in zeilen[:4])
    assert wort in zusammen


def test_mhd_und_nummer_sind_immer_da():
    text = _text({**ITEM, "name": "X " * 80})
    assert "30.09.2026" in text and "LEB000123" in text


# ------------------------------------------------- nur an faehige Terminals
class _Socket:
    def __init__(self) -> None:
        self.gesendet: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.gesendet.append(payload)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("caps,erwartet", [
    (None, "queued"),                 # hello steht noch aus: warten
    (set(), "failed"),                # alte Firmware: sagen, woran es liegt
    ({"raster2"}, "sent"),
])
def test_hochformat_geht_nur_an_terminals_die_es_koennen(caps, erwartet):
    async def scenario():
        await init_db()
        async with session_scope() as session:
            for job in (await session.execute(select(PrintJob))).scalars().all():
                await session.delete(job)
            await session.commit()
        geraet = f"hochformat-{erwartet}"
        conn = await hub.register(geraet, _Socket())
        workflow.session_for(geraet).caps = caps
        try:
            async with session_scope() as session:
                job = PrintJob(label="LEB000123", payload=labels.render_label(ITEM, CFG),
                               status="queued")
                session.add(job)
                await session.commit()
                await workflow.flush_print_queue(session, geraet)
                await session.refresh(job)
                return job.status, job.error
        finally:
            await hub.unregister(conn)
            workflow.drop_session(geraet)

    status, fehler = _run(scenario())
    assert status == erwartet
    if erwartet == "failed":
        assert "Firmware" in fehler
