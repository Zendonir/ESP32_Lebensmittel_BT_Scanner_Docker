"""Die Etikettenhoehe muss stimmen, nicht ungefaehr stimmen.

Ein Etikett darf **genau** eine Teilung Papier verbrauchen. Ist die Rechnung
zu klein, laeuft der Inhalt aufs naechste Etikett; ist sie zu gross, wandert
der Stapel. Beides faellt erst nach ein paar Etiketten auf - und dann sucht
man den Fehler beim Drucker statt in der Arithmetik.

Vorher rechnete der Server mit 24 Punkten je Zeile, der Drucker nahm aber
seinen eigenen Zeilenabstand (`ESC @` stellt den Standard ein, laut
Spezifikation 1/6 Zoll = rund 34 Punkte bei 203 dpi). Beim klassischen
Zuschnitt auf 50x30 mm waren das 314 statt 240 Punkten: 9,2 mm Ueberlauf je
Etikett. Die Firmware setzt den Abstand jetzt vor jeder Zeile ausdruecklich
auf den Wert, den der Server mitschickt - diese Tests halten die Zusage fest.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/mass.db"

from app.services import labels as L  # noqa: E402
from app.services.settings_store import DEFAULTS  # noqa: E402

ITEM = {
    "name": "Schweinefilet",
    "subcategory": "Schwein",
    "category": "Fleisch & Fisch",
    "expiry_date": "2026-12-24",
    "added_date": "2026-09-21",
    "quantity": 1,
    "unit": "St.",
    "location": "Kuehlschrank",
    "label": "LEB000123",
    "brand": "Hofgut",
}


def _cfg(**patch) -> dict:
    return {**DEFAULTS["printer"], **patch}


@pytest.mark.parametrize("layout", sorted(L.LAYOUTS))
@pytest.mark.parametrize("code", ["qr", "code128"])
@pytest.mark.parametrize("size", ["klein", "mittel", "gross"])
def test_etikett_verbraucht_genau_eine_teilung(layout, code, size):
    cfg = _cfg(label_layout=layout, label_code=code, label_code_size=size)
    payload = L.render_label(ITEM, cfg)

    pitch = int(payload["stack_mm"] * L.DOTS_PER_MM)
    verbraucht = L.total_dots(payload["blocks"])

    assert payload["overflow"] == 0, f"{layout}/{code}/{size} passt nicht aufs Etikett"
    assert verbraucht == pitch, (
        f"{layout}/{code}/{size}: {verbraucht} statt {pitch} Punkte - "
        "die Folgeetiketten wandern um die Differenz"
    )


@pytest.mark.parametrize("hoehe_mm", [25, 30, 40, 50])
def test_teilung_stimmt_bei_jeder_etikettenhoehe(hoehe_mm):
    cfg = _cfg(label_height_mm=hoehe_mm)
    payload = L.render_label(ITEM, cfg)
    assert L.total_dots(payload["blocks"]) == int(payload["stack_mm"] * L.DOTS_PER_MM)


def test_teilung_stimmt_auch_mit_rueckzug_und_totbereich():
    """Rueckzug und Totbereich verschieben nur den Anfang, nicht die Teilung."""
    cfg = _cfg(backfeed_dots=40, label_dead_zone_mm=5)
    payload = L.render_label(ITEM, cfg)
    assert L.total_dots(payload["blocks"]) == int(payload["stack_mm"] * L.DOTS_PER_MM)


def test_jeder_block_traegt_seine_hoehe():
    """Ohne `h` faellt die Firmware auf ihren eigenen Abstand zurueck - genau
    das war die Ursache des Ueberlaufs."""
    payload = L.render_label(ITEM, _cfg())
    for block in payload["blocks"]:
        if block["t"] in ("feed", "back"):
            assert "h" not in block      # die tragen ihre Laenge in `dots`
            continue
        assert block.get("h") == L.block_dots(block), block


@pytest.mark.parametrize("scale", [2, 3, 4])
def test_qr_reservierung_passt_zu_ganzen_baendern(scale):
    """`ESC *` druckt immer acht Punktzeilen auf einmal.

    Die Reservierung muss deshalb ein Vielfaches von acht sein, sonst bleibt
    je Code ein Rest uebrig, den niemand vorschiebt. Und sie muss fuer einen
    quadratischen Code mit Ruhezone reichen - die Firmware leitet ihre
    Skalierung daraus ab.
    """
    reserviert = L.block_dots({"t": "qr", "v": "LEB000123", "scale": scale})
    assert reserviert % L.QR_BAND == 0
    module = 21 + 2 * L.QR_QUIET
    assert reserviert >= module * scale - (L.QR_BAND - 1)
    # Die Firmware rechnet scale = reserviert // module und rundet ab; dabei
    # darf nicht 0 herauskommen.
    assert reserviert // module >= 1


def test_zu_kleines_etikett_wird_gemeldet_statt_verschluckt():
    """Passt der Code nicht mehr, soll das im Auftrag stehen.

    Vorher lief der Rest stillschweigend aufs Folgeetikett - und weil jedes
    weitere Etikett denselben Versatz erbt, verschiebt sich die ganze Rolle.
    """
    cfg = _cfg(label_height_mm=8, label_code="qr", label_code_size="gross")
    payload = L.render_label(ITEM, cfg)
    assert payload["overflow"] > 0


def test_formularvorschub_ersetzt_den_berechneten_rest():
    """Bei gestanzten Etiketten sucht der Drucker die Luecke selbst.

    Die Teilung muss auch dann rechnerisch aufgehen - die Vorschau und
    `total_dots()` zeigen weiterhin eine volle Teilung -, aber der letzte
    Block ist `form` statt `feed`. Die Firmware schickt dafuer `GS FF`, und
    damit stimmt die Registrierung bei jedem Etikett neu, statt an unserer
    Punkterechnung zu haengen.
    """
    payload = L.render_label(ITEM, _cfg(label_end="formfeed"))
    letzter = payload["blocks"][-1]

    # Bleibt ein feed-Block mit Merkmal, statt ein eigener Typ zu werden:
    # eine Firmware, die `form` nicht kennt, wuerde einen eigenen Typ
    # stillschweigend fallen lassen und gar nicht vorschieben - die Etiketten
    # liefen uebereinander. Mit `dots` daneben macht auch sie das Richtige.
    assert letzter["t"] == "feed"
    assert letzter["form"] is True
    assert letzter["dots"] > 0
    assert L.total_dots(payload["blocks"]) == int(payload["stack_mm"] * L.DOTS_PER_MM)

    # Ohne die Einstellung bleibt es beim berechneten Vorschub - und ohne
    # jedes Merkmal, damit sich nichts aendert, was sich nicht aendern soll.
    ohne = L.render_label(ITEM, _cfg())
    assert ohne["blocks"][-1]["t"] == "feed"
    assert "form" not in ohne["blocks"][-1]


# ----------------------------------------------------------- Kalibrierdruck
def test_kalibrierstreifen_passt_in_den_sendepuffer():
    """Der Streifen muss in einem Rutsch in die Firmware passen.

    `Printer::process()` faengt erst an, wenn genug Platz im Sendepuffer ist
    (6144 Byte), und schreibt dann alles am Stueck. Waere der Streifen
    groesser, wartete write() doch wieder auf die 9600-Baud-Leitung und der
    ganze Loop stuende - genau das, was die Warteschlange vermeiden soll.
    """
    from app.services import calibration as K

    job = K.build(line_mm=50)
    bytes_auf_der_leitung = 0
    for b in job["blocks"]:
        if b["t"] == "raster":
            baender = -(-b["h"] // 8)
            bytes_auf_der_leitung += baender * (b["w"] + 7)
        elif b["t"] in ("text", "row"):
            bytes_auf_der_leitung += 48 + len(str(b.get("v") or ""))
        else:
            bytes_auf_der_leitung += 6
    assert bytes_auf_der_leitung < 6144


def test_kalibrierstreifen_stellt_alle_fragen():
    """Jede Frage der Anleitung muss im Streifen auch wirklich vorkommen."""
    from app.services import calibration as K

    job = K.build(line_mm=50)
    arten = {b["t"] for b in job["blocks"]}
    assert "raster" in arten     # Zeilenabstand-Marken und Quadrat
    assert "back" in arten       # Rueckzug (ESC j)
    assert any(b.get("form") for b in job["blocks"])   # Lueckensensor (GS FF)

    # Das Quadrat muss quadratisch angefordert werden - sonst misst man
    # nicht die Verzerrung des Druckers, sondern die eigene.
    quadrat = [b for b in job["blocks"] if b["t"] == "raster" and b["h"] > 8]
    assert len(quadrat) == 1
    assert quadrat[0]["w"] == quadrat[0]["h"] == K.BOX_DOTS


def test_rasterdaten_sind_vollstaendig_und_randgenau():
    """Die Firmware liest Zeile fuer Zeile - fehlt ein Byte, druckt sie Muell.

    Und die ueberhaengenden Bits der letzten Spalte muessen geloescht sein,
    sonst wird der Balken bis zu sieben Punkte breiter als bestellt und die
    Messung stimmt nicht.
    """
    import base64

    from app.services import calibration as K

    block = K._raster(200, 8)
    roh = base64.b64decode(block["d"])
    row_bytes = (block["w"] + 7) // 8
    assert len(roh) == row_bytes * block["h"]
    assert all(byte == 0xFF for byte in roh)      # 200 ist durch 8 teilbar

    krumm = K._raster(daten_breite := 100, 8)     # 100 = 12 Bytes + 4 Bit
    roh = base64.b64decode(krumm["d"])
    assert len(roh) == ((daten_breite + 7) // 8) * 8
    assert roh[12] == 0xF0                        # nur die ersten vier Bit
