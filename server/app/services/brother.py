"""Brother-Etikettendrucker (TD-2020A) am WLAN-Printserver.

Der Drucker haengt per USB an einem eigenen ESP32-S3
(ESP32-USB-Brother-Label-Wifi-Server). Der Printserver ist bewusst dumm: er
nimmt druckfertige Daten an (`POST /api/print/raw`), reiht sie in seine
Warteschlange, prueft vor dem Druck den Brother-Status und meldet, was der
Drucker selbst zurueckgibt (`GET /api/jobs/{id}`). Alles andere passiert hier -
dieselbe Regel wie beim Terminal: der Server haelt Daten und Ablauf.

Ablauf eines Etiketts:

1. `inventory.add_items()` legt einen `PrintJob` mit `ziel = "brother"` an.
   Im Payload steht nur der Artikel; gezeichnet wird erst beim Senden, mit den
   dann gueltigen Einstellungen.
2. `durchgang()` zeichnet das Etikett (`hochformat.zeichnen`, dieselbe Schrift
   und Anordnung wie das Hochformat am Terminal), setzt es in Brother-Raster-
   befehle um und schickt es mit `X-Job-Key`. Kommt die Antwort nicht an,
   schickt der naechste Durchgang dieselben Daten mit demselben Schluessel -
   der Printserver erkennt ihn und druckt nicht doppelt.
3. Spaetere Durchgaenge fragen den Auftrag beim Printserver ab, bis der Drucker
   "gedruckt" oder einen Fehler meldet. Nach einem Druckerfehler verwirft der
   TD-2020A den Auftrag (am Geraet gemessen); deshalb gibt es keinen
   selbsttaetigen Nachdruck, sondern "failed" mit der Meldung des Druckers.

Die Rasterbefehle stehen in der "Raster Command Reference TD-2020/2120N/
2130N" (Version 1.01, darf nicht weitergegeben werden - deshalb hier nur die
Fakten, die der Code braucht). `standard_profil()` baut den Auftrag danach.
Was der Originaltreiber fuer den TD-2020A tatsaechlich schickt, kann
`analysieren()` aus einer Druckdatei uebernehmen (Kopf samt Zusatzbefehlen
wie ESC i U, Kompression, Zeilenzahl) - das hat Vorrang, weil es genau das
ist, womit der Drucker schon einmal richtig gedruckt hat.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
from typing import Any

import httpx
from PIL import Image, ImageDraw
from sqlalchemy import select

from ..models import PrintJob
from . import hochformat

log = logging.getLogger(__name__)

DOTS_PER_MM = 8           # 203 dpi

# Druckkopf des TD-2020: 448 Pins = 56 Bytes je Rasterzeile (Referenz 2.3.5).
KOPF_BYTES = 56

# Druckbereich quer (Pins) je Etikettenbreite in mm, Referenz 2.3.5. Er liegt
# mittig auf dem Kopf: 51 mm = 33 Pins frei, 382 bedruckt, 33 frei.
DRUCKBREITE = {51: 382, 30: 216, 40: 296, 50: 376, 60: 448, 57: 432, 58: 440}
# Druckzeilen gestanzter Etiketten (Breite, Laenge) -> Zeilen, Referenz 2.3.2:
# je 3 mm (24 Punkte) am Anfang und Ende haelt der Drucker frei.
DRUCKLAENGE = {(51, 26): 157, (30, 30): 192, (40, 40): 272, (40, 50): 352,
               (40, 60): 432, (50, 30): 192, (60, 60): 432}
# Endlosband: Rand (ESC i d) mindestens 3 mm, Laenge 12 mm bis 1 m.
ENDLOS_RAND = 24
ENDLOS_ZEILEN_MIN, ENDLOS_ZEILEN_MAX = 96, 7992


def _punkte(mm: float) -> int:
    return int(round(mm * 203 / 25.4))


def druckbreite(breite_mm: float) -> int:
    """Bedruckbare Pins quer; unbekannte Breite: 1,5 mm Rand je Seite."""
    bekannt = DRUCKBREITE.get(int(round(breite_mm)))
    if bekannt:
        return bekannt
    return max(8, min(KOPF_BYTES * 8, _punkte(breite_mm) - 24))


def druckzeilen(breite_mm: float, laenge_mm: float) -> int:
    """Druckzeilen eines gestanzten Etiketts; unbekannt: 3 mm Rand je Ende."""
    bekannt = DRUCKLAENGE.get((int(round(breite_mm)), int(round(laenge_mm))))
    return bekannt or max(24, _punkte(laenge_mm) - 48)

# Raster-Zeilenbefehle, wie sie in Brother-Druckdateien vorkommen:
#   "g00n"  g 00h n  Daten   (n Bytes, ein Laengenbyte - QL/RJ/TD)
#   "g2"    g nL nH  Daten   (zwei Laengenbytes)
#   "G2"    G nL nH  Daten   (zwei Laengenbytes, P-touch)
ZEILENBEFEHLE = ("g00n", "g2", "G2")

DREHUNGEN = ("keine", "180", "links", "rechts")

ZIEL = "brother"

# So lange darf der Printserver fuer eine Antwort brauchen. Die Annahme ist
# schnell (er speichert nur), er spoolt aber erst vollstaendig.
HTTP_TIMEOUT_S = 10

# Ein Durchgang mit offenen Auftraegen wiederholt sich nach so vielen
# Sekunden; ohne offene Auftraege schlaeft der Dienst, bis `anstossen()` kommt.
TAKT_OFFEN_S = 2
TAKT_RUHE_S = 60
TAKT_UNERREICHBAR_S = 20

# Zustaende des Printservers (print_queue.c) -> was das hier bedeutet.
FERTIG_GUT = ("printed",)
FERTIG_SCHLECHT = ("failed", "cancelled", "incomplete", "unknown")
UNTERWEGS = ("queued", "waiting", "printing")

# Testetikett: dasselbe Beispiel wie die Vorschau unter Etiketten.
BEISPIEL = {
    "label": "TEST",
    "name": "Testetikett Brother",
    "subcategory": "",
    "expiry_date": "2026-12-31",
    "added_date": "2026-10-21",
    "quantity": 500,
    "unit": "g",
    "location": "Kühlschrank",
}


class BrotherFehler(Exception):
    """Eine Treiberdatei, die sich nicht lesen laesst."""


# --------------------------------------------------------------- Zustaendig
def zustaendig(cfg: dict | None, haushalt: str) -> bool:
    """Druckt dieser Haushalt am Brother statt am Terminal?

    `haushalt` in den Einstellungen leer = alle Haushalte; mehrere Namen mit
    Komma getrennt.
    """
    cfg = cfg or {}
    if not cfg.get("aktiv") or not str(cfg.get("url", "")).strip():
        return False
    liste = [h.strip() for h in str(cfg.get("haushalt", "") or "").split(",") if h.strip()]
    return not liste or (haushalt or "") in liste


def basis_url(cfg: dict) -> str:
    url = str(cfg.get("url", "") or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def auftrag(item: dict) -> dict:
    """Payload eines Brother-Auftrags: nur der Artikel, gezeichnet wird beim Senden."""
    felder = ("label", "name", "brand", "subcategory", "expiry_date", "added_date",
              "quantity", "unit", "location", "household")
    return {"item": {k: item.get(k, "") for k in felder}}


# ------------------------------------------------------------------- Profil
def _esc_i_z(daten: bytes, stelle: int) -> dict:
    n = daten[stelle + 3:stelle + 13]
    return {
        "gueltig": n[0],
        "medium": n[1],
        "breite_mm": n[2],
        "laenge_mm": n[3],
        "zeilen": int.from_bytes(n[4:8], "little"),
    }


def standard_profil(breite_mm: float, laenge_mm: float) -> dict:
    """Profil ohne Treiberdatei, nach der Rasterreferenz (Kap. 2.1, 4).

    Invalidate (200 x 00h), ESC @, Rastermodus (ESC i a 01h), Druckinformation
    (ESC i z: Medientyp/Breite/Laenge gueltig, "printer recovery always on"),
    ESC i M 00h, Rand (ESC i d: gestanzt 0, Endlos mindestens 24 Punkte),
    keine Kompression (M 00h); jede Zeile `g 00h 38h` + 56 Bytes, Ende 1Ah.
    `laenge_mm` 0 = Endlosband, dann 30 mm je Etikett.
    """
    breite = int(round(breite_mm))
    laenge = int(round(laenge_mm))
    gestanzt = laenge > 0
    gueltig = 0x02 | 0x04 | 0x80 | (0x08 if gestanzt else 0)
    rand = 0 if gestanzt else ENDLOS_RAND
    kopf = (b"\x00" * 200 + b"\x1b@" + b"\x1bia\x01"
            + b"\x1biz" + bytes([gueltig, 0x0B if gestanzt else 0x0A,
                                 min(breite, 255), min(laenge, 255)])
            + b"\x00\x00\x00\x00" + b"\x00\x00"
            + b"\x1biM\x00" + b"\x1bid" + rand.to_bytes(2, "little") + b"M\x00")
    zeilen = (druckzeilen(breite, laenge) if gestanzt
              else min(ENDLOS_ZEILEN_MAX, max(ENDLOS_ZEILEN_MIN, 30 * DOTS_PER_MM)))
    return {
        "quelle": "standard",
        "kopf": kopf.hex(),
        "z_stelle": 200 + 2 + 4,
        "zeilenbefehl": "g00n",
        "bytes": KOPF_BYTES,
        "tiff": False,
        "leerzeile_z": False,
        "zeilen": zeilen,
        "ende": "1a",
        "breite_mm": breite,
        "laenge_mm": laenge,
    }


# Laengen der Parameter von ESC i <x>, soweit sie in Druckdateien vorkommen.
# ESC i U w 01h + 127 Bytes Medieninformation (Referenz, Kap. 4).
_ESC_I_LAENGE = {"a": 1, "z": 10, "M": 1, "A": 1, "K": 1, "d": 2, "S": 0, "!": 1, "U": 129}


def _zeilen_lesen(daten: bytes, start: int, form: str, tiff: bool) -> dict | None:
    """Rasterteil ab `start` in einer bestimmten Zeilenform lesen.

    Liefert None, wenn die Form nicht passt (unbekanntes Byte, Laenge ueber
    das Dateiende) - dann ist es eine andere.
    """
    i, n = start, len(daten)
    zeilen: list[bytes | None] = []   # None = Leerzeile (Z)
    leer_z = False
    while i < n:
        b = daten[i]
        if b == 0x5A:                                  # Z: Leerzeile
            zeilen.append(None)
            leer_z = True
            i += 1
        elif b == 0x4D and i + 1 < n:                  # M n: Kompression
            tiff = daten[i + 1] == 0x02
            i += 2
        elif b in (0x67, 0x47):                        # g / G
            if form == "g00n" and b == 0x67:
                if i + 2 >= n or daten[i + 1] != 0:
                    return None
                laenge, kopf = daten[i + 2], 3
            elif form == "g2" and b == 0x67 or form == "G2" and b == 0x47:
                if i + 2 >= n:
                    return None
                laenge, kopf = daten[i + 1] | (daten[i + 2] << 8), 3
            else:
                return None
            if laenge == 0 or i + kopf + laenge > n:
                return None
            roh = daten[i + kopf:i + kopf + laenge]
            zeilen.append(hochformat.unpackbits(roh, 4096) if tiff else roh)
            i += kopf + laenge
        elif b in (0x0C, 0x1A):                        # Seitenende / Druckende
            if not zeilen:
                return None
            return {"zeilen": zeilen, "tiff": tiff, "leer_z": leer_z,
                    "ende": i, "weitere_seiten": b == 0x0C and i + 1 < n}
        else:
            return None
    return None


def analysieren(daten: bytes) -> dict:
    """Druckdatei des Originaltreibers in ein Profil uebersetzen.

    Uebernommen wird alles vor der ersten Rasterzeile unveraendert (Kopf:
    Invalidate, ESC @, Moduswahl, Druckinformation, Raender, Kompression -
    genau das, womit der Drucker das Etikett des Treibers gedruckt hat). Aus
    den Rasterzeilen kommen Befehl, Laenge und Zahl; das Etikett selbst wird
    spaeter durch unseres ersetzt.
    """
    if len(daten) < 20:
        raise BrotherFehler("Datei ist zu kurz für einen Druckauftrag")
    # ESC i z, aber nicht innerhalb der 127 Bytes eines ESC i U w 01h davor.
    zusatz = [i for i in range(len(daten)) if daten.startswith(b"\x1biUw\x01", i)]
    z = daten.find(b"\x1biz")
    while z >= 0 and any(u < z < u + 3 + _ESC_I_LAENGE["U"] for u in zusatz):
        z = daten.find(b"\x1biz", z + 1)
    if z < 0 or z + 13 > len(daten):
        raise BrotherFehler(
            "Keine Druckinformation (ESC i z) gefunden – ist das eine Rasterdatei des "
            "Brother-Treibers? Im Treiber ggf. den Modus „Raster“ wählen.")

    # Hinter ESC i z: bekannte Befehle ueberspringen bis zur ersten Zeile.
    i = z + 13
    while i < len(daten):
        b = daten[i]
        if b == 0x00:
            i += 1
        elif b == 0x1B and daten[i + 1:i + 2] == b"@":
            i += 2
        elif b == 0x1B and daten[i + 1:i + 2] == b"i" and i + 2 < len(daten):
            befehl = chr(daten[i + 2])
            if befehl not in _ESC_I_LAENGE:
                raise BrotherFehler(
                    f"Unbekannter Befehl ESC i {befehl!r} (0x{daten[i + 2]:02X}) an Stelle {i} – "
                    "bitte die Datei als Fehlerbericht weitergeben")
            i += 3 + _ESC_I_LAENGE[befehl]
        elif b == 0x4D and i + 1 < len(daten):
            i += 2
        elif b in (0x67, 0x47, 0x5A):
            break
        else:
            raise BrotherFehler(f"Unerwartetes Byte 0x{b:02X} vor den Rasterzeilen (Stelle {i})")
    start = i
    # Ein "M 02h" vor den Zeilen gehoert in den Kopf, gilt aber fuer die Zeilen.
    tiff_kopf = b"M\x02" in daten[z + 13:start]

    gelesen, form = None, ""
    for form in ZEILENBEFEHLE:
        gelesen = _zeilen_lesen(daten, start, form, tiff_kopf)
        if gelesen is not None:
            break
    if gelesen is None:
        raise BrotherFehler("Die Rasterzeilen lassen sich in keiner bekannten Form lesen")
    tiff = gelesen["tiff"]

    laengen = {len(zl) for zl in gelesen["zeilen"] if zl is not None}
    bytes_je_zeile = max(laengen) if laengen else KOPF_BYTES
    info = _esc_i_z(daten, z)
    ende = daten[gelesen["ende"]:gelesen["ende"] + 1]
    return {
        "quelle": "treiber",
        "kopf": daten[:start].hex(),
        "z_stelle": z,
        "zeilenbefehl": form,
        "bytes": bytes_je_zeile,
        "tiff": tiff,
        "leerzeile_z": gelesen["leer_z"],
        "zeilen": len(gelesen["zeilen"]),
        "ende": "1a" if gelesen["weitere_seiten"] else ende.hex(),
        "breite_mm": info["breite_mm"],
        "laenge_mm": info["laenge_mm"] if info["gueltig"] & 0x08 else 0,
        "medium": {0x0A: "Endlos", 0x0B: "Etiketten"}.get(info["medium"], f"0x{info['medium']:02X}"),
        "zeilen_im_kopf": info["zeilen"],
        "weitere_seiten": gelesen["weitere_seiten"],
        "vorschau": _vorschau_png(gelesen["zeilen"], bytes_je_zeile),
    }


def _vorschau_png(zeilen: list[bytes | None], breite_bytes: int) -> str:
    """Das Etikett der Treiberdatei als PNG (data-URL) - so wie es auf dem
    Papier liegt, erste Zeile oben. Zeigt, ob die Datei richtig gelesen wurde
    und ob der Drucker die Zeilen gespiegelt erwartet."""
    hoehe = max(1, len(zeilen))
    bild = Image.new("1", (breite_bytes * 8, hoehe), 1)
    roh = b"".join((zl or b"").ljust(breite_bytes, b"\x00")[:breite_bytes] for zl in zeilen)
    if roh:
        bild.frombytes(bytes(b ^ 0xFF for b in roh))
    puffer = io.BytesIO()
    bild.convert("L").save(puffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(puffer.getvalue()).decode("ascii")


def mit_medium(cfg: dict, medium: dict | None) -> dict:
    """Ohne Treiberdatei und ohne eingetragene Masse: was der Drucker meldet.

    Der TD-2020A nennt im Status (ESC i S, ueber den Printserver) Breite und
    bei gestanzten Etiketten auch die Laenge der eingelegten Rolle.
    """
    if (cfg.get("profil") or {}).get("kopf") or not medium:
        return cfg
    neu = dict(cfg)
    if not float(neu.get("breite_mm") or 0) and medium.get("media_width_mm"):
        neu["breite_mm"] = medium["media_width_mm"]
    if not float(neu.get("laenge_mm") or 0) and medium.get("media_length_mm"):
        neu["laenge_mm"] = medium["media_length_mm"]
    # Endlosband meldet Laenge 0 - dann nicht auf 26-mm-Etiketten zurueckfallen.
    if medium.get("media_type") == "continuous_tape" and not float(cfg.get("laenge_mm") or 0):
        neu["endlos"] = True
    return neu


def profil_aus(cfg: dict) -> dict:
    """Gespeichertes Profil oder das Standardprofil zu den eingestellten Massen."""
    profil = cfg.get("profil") or {}
    if profil.get("kopf") and profil.get("zeilen"):
        return profil
    laenge = 0.0 if cfg.get("endlos") else float(cfg.get("laenge_mm") or 26)
    return standard_profil(float(cfg.get("breite_mm") or 51), laenge)


# ------------------------------------------------------------------ Zeichnen
def masse(profil: dict, cfg: dict) -> tuple[int, int]:
    """(Breite quer zum Druckkopf, Zeilen in Laufrichtung) in Punkten."""
    kopf = int(profil.get("bytes") or KOPF_BYTES) * 8
    breite_mm = float(cfg.get("breite_mm") or 0) or float(profil.get("breite_mm") or 0) or 51
    return min(kopf, druckbreite(breite_mm)), int(profil["zeilen"])


def _zeichnen(item: dict, printer_cfg: dict, w: int, h: int) -> Image.Image:
    """Etikett in Leserichtung. Hochkant wie das Hochformat am Terminal (Code
    unten); quer steht der Code rechts mit der Nummer darunter - unten
    angehaengt bliebe auf 26 mm Hoehe fuer Name und MHD kein Platz."""
    label = item.get("label", "")
    if w <= h * 1.2 or not label:
        return hochformat.zeichnen(item, printer_cfg, w, h)
    rand = hochformat.RAND
    schrift = hochformat._schrift(16, True)
    nummer_h = hochformat._zeilenhoehe(schrift)
    modul = hochformat.QR_MODUL.get(printer_cfg.get("label_code_size", "mittel"), 4)
    qr = hochformat._qr(label, modul)
    while modul > 2 and qr.height - 2 * hochformat.QR_RUHE * modul > h - 2 * rand - nummer_h:
        modul -= 1
        qr = hochformat._qr(label, modul)
    ruhe = hochformat.QR_RUHE * modul
    code_w = max(qr.width - 2 * ruhe, int(schrift.getlength(label)))
    links = w - code_w - rand
    bild = Image.new("L", (w, h), 255)
    bild.paste(hochformat.zeichnen({**item, "label": ""}, printer_cfg, links, h), (0, 0))
    x = w - rand - code_w
    y = max(0, (h - (qr.height - 2 * ruhe) - nummer_h) // 2)
    bild.paste(qr, (x + (code_w - qr.width + 2 * ruhe) // 2 - ruhe, y - ruhe))
    zeichner = ImageDraw.Draw(bild)
    zeichner.text((x + (code_w - schrift.getlength(label)) / 2, y + qr.height - 2 * ruhe + 2),
                  label, font=schrift, fill=0)
    return bild


def etikett_bild(item: dict, profil: dict, cfg: dict, printer_cfg: dict | None = None,
                 rahmen: bool = False) -> Image.Image:
    """Das Etikett in Druckrichtung: Breite = Etikett quer, Hoehe = Zeilen.

    Gezeichnet wird wie das Hochformat am Terminal (`hochformat.zeichnen`).
    `drehung` legt fest, wie man das Etikett nachher liest: "keine" mit der
    ersten Druckzeile oben, "180" umgekehrt, "links"/"rechts" hochkant.
    """
    breite, zeilen = masse(profil, cfg)
    drehung = cfg.get("drehung", "keine")
    hochkant = drehung in ("links", "rechts")
    w, h = (zeilen, breite) if hochkant else (breite, zeilen)
    bild = _zeichnen(item, printer_cfg or {}, w, h)
    if rahmen:
        ImageDraw.Draw(bild).rectangle([0, 0, w - 1, h - 1], outline=0, width=2)
    if drehung == "180":
        bild = bild.transpose(Image.Transpose.ROTATE_180)
    elif hochkant:
        bild = hochformat.gedreht(bild, drehung)
    return hochformat.einfarbig(bild)


def rasterdaten(bild: Image.Image, profil: dict, cfg: dict) -> bytes:
    """Bild (Modus "1", Breite <= Druckkopf) -> vollstaendiger Brother-Auftrag."""
    zeilen_bytes = int(profil.get("bytes") or KOPF_BYTES)
    kopf_punkte = zeilen_bytes * 8
    zeilen = int(profil["zeilen"])

    leinwand = Image.new("1", (kopf_punkte, zeilen), 1)
    versatz = int(round(float(cfg.get("versatz_mm") or 0) * DOTS_PER_MM))
    x = (kopf_punkte - bild.width) // 2 + versatz
    leinwand.paste(bild.crop((0, 0, bild.width, min(bild.height, zeilen))), (x, 0))
    if cfg.get("spiegeln"):
        leinwand = leinwand.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    # Schwarz = 1 fuer den Drucker; Pillow speichert Weiss als 1.
    roh = bytes(b ^ 0xFF for b in leinwand.tobytes())

    kopf = bytearray(bytes.fromhex(profil["kopf"]))
    z = int(profil.get("z_stelle", -1))
    if 0 <= z and kopf[z:z + 3] == b"\x1biz":
        kopf[z + 7:z + 11] = zeilen.to_bytes(4, "little")

    aus = bytearray(kopf)
    form = profil.get("zeilenbefehl", "g00n")
    for y in range(zeilen):
        zeile = roh[y * zeilen_bytes:(y + 1) * zeilen_bytes]
        tiff = bool(profil.get("tiff"))
        # Z gilt nur mit TIFF-Kompression (Referenz, "Zero raster graphics").
        if tiff and profil.get("leerzeile_z") and not any(zeile):
            aus += b"Z"
            continue
        daten = zeile
        if tiff:
            daten = hochformat.packbits(zeile)
            # Laenger als die Zeile: als ein woertlicher Block (n-1, 56 Bytes).
            if len(daten) > zeilen_bytes:
                daten = bytes([zeilen_bytes - 1]) + zeile
        if form == "g00n":
            aus += b"g\x00" + bytes([len(daten)])
        else:
            aus += (b"G" if form == "G2" else b"g") + len(daten).to_bytes(2, "little")
        aus += daten
    aus += bytes.fromhex(profil.get("ende") or "1a")
    return bytes(aus)


def auftrag_bauen(payload: dict, cfg: dict, printer_cfg: dict | None = None) -> bytes:
    profil = profil_aus(cfg)
    if payload.get("test"):
        bild = etikett_bild(BEISPIEL, profil, cfg, printer_cfg, rahmen=True)
    else:
        bild = etikett_bild(payload.get("item") or {}, profil, cfg, printer_cfg)
    return rasterdaten(bild, profil, cfg)


def vorschau_png(cfg: dict, printer_cfg: dict | None = None) -> str:
    """Das Testetikett so, wie es zum Drucker geht (erste Druckzeile oben)."""
    profil = profil_aus(cfg)
    bild = etikett_bild(BEISPIEL, profil, cfg, printer_cfg, rahmen=True)
    kopf = int(profil.get("bytes") or KOPF_BYTES) * 8
    leinwand = Image.new("L", (kopf, bild.height), 235)
    versatz = int(round(float(cfg.get("versatz_mm") or 0) * DOTS_PER_MM))
    leinwand.paste(bild.convert("L"), ((kopf - bild.width) // 2 + versatz, 0))
    puffer = io.BytesIO()
    leinwand.save(puffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(puffer.getvalue()).decode("ascii")


# --------------------------------------------------------------- Printserver
# Fuer Tests: ein httpx-Transport, der den Printserver nachstellt.
_transport: httpx.AsyncBaseTransport | None = None


def _client(cfg: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=basis_url(cfg), timeout=HTTP_TIMEOUT_S,
                             transport=_transport)


async def printserver_status(cfg: dict) -> dict:
    """Was der Printserver ueber sich und den Drucker weiss (ohne Druckerzugriff)."""
    if not basis_url(cfg):
        return {"erreichbar": False, "fehler": "Keine Adresse eingetragen"}
    try:
        async with _client(cfg) as client:
            r = await client.get("/api/status")
            r.raise_for_status()
            status = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"erreichbar": False, "fehler": _fehlertext(e)}
    drucker = status.get("printer") or {}
    brother = (status.get("brother") or {}).get("status") or {}
    api = status.get("print_api") or {}
    return {
        "erreichbar": True,
        "firmware": (status.get("firmware") or {}).get("version", ""),
        "drucker_verbunden": bool(drucker.get("connected")),
        "drucker": drucker.get("product", ""),
        "medium": brother,
        "warteschlange": status.get("queue") or {},
        # Ohne job_key koennte ein wiederholter Versand doppelt drucken.
        "job_key": bool(api.get("job_key")),
        "ip": (status.get("wifi") or {}).get("ip", ""),
    }


def _fehlertext(e: Exception) -> str:
    if isinstance(e, httpx.TimeoutException):
        return "Printserver antwortet nicht (Zeitüberschreitung)"
    if isinstance(e, httpx.ConnectError):
        return "Printserver nicht erreichbar – Adresse prüfen"
    if isinstance(e, httpx.HTTPStatusError):
        try:
            text = e.response.json().get("message", "")
        except ValueError:
            text = ""
        return f"Printserver: HTTP {e.response.status_code} {text}".strip()
    return f"Printserver: {e}"[:200]


def _schluessel(job: PrintJob) -> str:
    """Idempotenzschluessel: die Auftragsnummer allein reicht nicht - nach
    einer neuen Datenbank faengt sie wieder bei 1 an, und der Printserver
    hielte einen alten Auftrag fuer denselben."""
    stempel = int(job.created_at.timestamp()) if job.created_at else 0
    return f"lm-{job.id}-{stempel}"


async def _senden(client: httpx.AsyncClient, job: PrintJob, cfg: dict,
                  printer_cfg: dict) -> bool:
    """Einen Auftrag abgeben. False = Printserver nicht erreichbar (abbrechen)."""
    payload = dict(job.payload or {})
    try:
        daten = await asyncio.to_thread(auftrag_bauen, payload, cfg, printer_cfg)
    except Exception as e:   # Zeichnen darf den Dienst nie anhalten
        log.exception("Brother-Etikett %s laesst sich nicht zeichnen", job.label)
        job.status, job.error = "failed", f"Etikett nicht darstellbar: {e}"[:255]
        return True
    schluessel = _schluessel(job)
    try:
        r = await client.post(
            "/api/print/raw", content=daten,
            headers={"Content-Type": "application/octet-stream",
                     "X-Job-Name": f"Etikett {job.label}"[:47],
                     "X-Job-Key": schluessel})
    except httpx.HTTPError as e:
        job.error = _fehlertext(e)[:255]
        return False
    if r.status_code == 503:          # Warteschlange voll - spaeter weiter
        job.error = "Warteschlange des Printservers voll"
        return False
    if r.status_code not in (200, 202):
        job.attempts += 1
        job.status = "failed" if job.attempts >= 3 else "queued"
        job.error = _fehlertext(httpx.HTTPStatusError("", request=r.request, response=r))[:255]
        return True
    antwort = r.json()
    job.payload = {**payload, "brother": {"id": int(antwort.get("job_id", 0)), "key": schluessel}}
    job.status = "sent"
    job.attempts += 1
    job.error = ""
    return True


async def _abgleichen(client: httpx.AsyncClient, job: PrintJob) -> bool:
    """Stand eines abgegebenen Auftrags holen. False = nicht erreichbar."""
    info = (job.payload or {}).get("brother") or {}
    try:
        r = await client.get(f"/api/jobs/{int(info.get('id', 0))}")
    except httpx.HTTPError as e:
        job.error = _fehlertext(e)[:255]
        return False
    daten = r.json().get("job") if r.status_code == 200 else None
    if not daten or (info.get("key") and daten.get("key") != info.get("key")):
        # Der Printserver kennt den Auftrag nicht mehr: neu gestartet (die
        # Liste lebt im RAM) oder ueberlaufen. Ob gedruckt wurde, weiss
        # niemand - lieber fragen als doppelt drucken.
        job.status = "failed"
        job.error = "Printserver neu gestartet – ob gedruckt wurde, ist unbekannt"
        return True
    zustand, meldung = daten.get("state", ""), daten.get("message", "")
    if zustand in FERTIG_GUT:
        job.status, job.error = "done", ""
    elif zustand == "unknown":
        job.status = "failed"
        job.error = "Drucker hat den Druck nicht bestätigt – Etikett prüfen"
    elif zustand in FERTIG_SCHLECHT:
        job.status = "failed"
        if "Wrong media" in meldung:
            meldung = "eingelegte Etiketten passen nicht zu den gesendeten Maßen"
        job.error = f"Drucker: {meldung or zustand}"[:255]
    elif zustand == "waiting":
        job.error = f"wartet auf Drucker: {meldung}"[:255]
    return True


# ------------------------------------------------------------ Lueckensensor
#
# Gestanzte Etiketten positioniert der TD-2020 selbst: steht in ESC i z der
# Medientyp 0Bh mit gueltiger Breite und Laenge, registriert er jedes Etikett
# an der Luecke, und 1Ah faehrt bis zur naechsten (Referenz 2.3.2/ESC i z;
# einen Rand gibt es bei Etiketten nicht, ESC i d muss 0 sein). Es gibt also
# keine Vorschubrechnung, die wandern koennte - anders als am Terminaldrucker.
#
# Das gilt aber nur, wenn die eingelegte Rolle zu den gesendeten Angaben
# passt. Deshalb wird vor dem Senden verglichen, was der Drucker als
# eingelegt meldet; passt es nicht, bleibt der Auftrag liegen und sagt warum,
# statt ein verrutschtes oder verworfenes Etikett zu riskieren.
def erwartet(profil: dict, cfg: dict) -> dict:
    """Was der Auftrag dem Drucker als Medium ankuendigt."""
    breite = int(round(float(cfg.get("breite_mm") or 0) or float(profil.get("breite_mm") or 0)))
    laenge = int(profil.get("laenge_mm") or 0)
    return {"typ": "die_cut_labels" if laenge else "continuous_tape",
            "breite_mm": breite, "laenge_mm": laenge}


def _medium_text(typ: str, breite: int, laenge: int) -> str:
    if typ == "die_cut_labels":
        return f"Etiketten {breite} × {laenge} mm"
    return f"Endlosband {breite} mm"


def medium_abweichung(soll: dict, medium: dict | None) -> str:
    """Leer, wenn die eingelegte Rolle passt (oder der Drucker nichts meldet)."""
    if not medium or not medium.get("media_type"):
        return ""
    typ = medium.get("media_type")
    if typ == "none" or "no_media" in (medium.get("errors") or []):
        return "Keine Etiketten eingelegt"
    if typ not in ("die_cut_labels", "continuous_tape"):
        return ""
    ist_b, ist_l = int(medium.get("media_width_mm") or 0), int(medium.get("media_length_mm") or 0)
    passt = typ == soll["typ"] and abs(ist_b - soll["breite_mm"]) <= 1 and (
        typ == "continuous_tape" or abs(ist_l - soll["laenge_mm"]) <= 1)
    if passt:
        return ""
    return (f"Eingelegt: {_medium_text(typ, ist_b, ist_l)} – eingestellt: "
            f"{_medium_text(soll['typ'], soll['breite_mm'], soll['laenge_mm'])}. "
            "Rolle oder Einstellung (bzw. Treiberdatei) anpassen")


async def _medium_live(client: httpx.AsyncClient) -> dict | None:
    """Frische Statusabfrage (ESC i S) - nur wenn der Printserver frei ist."""
    try:
        r = await client.get("/api/printer/status")
        if r.status_code != 200:
            return None
        return ((r.json().get("printer") or {}).get("status")) or None
    except (httpx.HTTPError, ValueError):
        return None


async def durchgang() -> bool:
    """Offene Brother-Auftraege abgeben und abgleichen.

    Gibt zurueck, ob danach noch etwas offen ist. Die Reihenfolge bleibt die
    der Auftraege; ist der Printserver nicht erreichbar, endet der Durchgang
    sofort (der Fehler steht am Auftrag, damit man ihn in der Liste sieht).
    """
    from ..db import session_scope
    from . import settings_store

    async with session_scope() as session:
        cfg = await settings_store.get(session, "brother", {}) or {}
        jobs = (await session.execute(
            select(PrintJob).where(PrintJob.ziel == ZIEL,
                                   PrintJob.status.in_(("queued", "sent")))
            .order_by(PrintJob.id))).scalars().all()
        if not jobs or not basis_url(cfg):
            return False
        printer_cfg = await settings_store.get(session, "printer", {}) or {}
        vorher = {j.id: (j.status, j.error) for j in jobs}
        erreichbar, abweichung = True, ""
        async with _client(cfg) as client:
            if any(j.status == "queued" for j in jobs):
                status = await printserver_status(cfg)
                medium = status.get("medium")
                cfg = mit_medium(cfg, medium)
                soll = erwartet(profil_aus(cfg), cfg)
                abweichung = medium_abweichung(soll, medium)
                schlange = status.get("warteschlange") or {}
                if abweichung and not schlange.get("active_id") and not schlange.get("queued"):
                    # Der gemerkte Stand kann von vor dem Rollenwechsel sein.
                    frisch = await _medium_live(client)
                    if frisch is not None:
                        abweichung = medium_abweichung(soll, frisch)
            for job in jobs:
                if job.status == "sent":
                    erreichbar = await _abgleichen(client, job)
                elif abweichung:
                    job.error = abweichung[:255]
                    continue
                else:
                    erreichbar = await _senden(client, job, cfg, printer_cfg)
                if not erreichbar:
                    break
        geaendert = any(vorher[j.id] != (j.status, j.error) for j in jobs)
        await session.commit()
        offen = any(j.status in ("queued", "sent") for j in jobs)
    if geaendert:
        from ..device.hub import hub
        await hub.notify_ui("labels")
    if not erreichbar or abweichung:
        # Beides heilt nicht in zwei Sekunden: seltener nachsehen.
        raise ConnectionError
    return offen


# ------------------------------------------------------------------- Dienst
_task: asyncio.Task | None = None
_wecker: asyncio.Event | None = None


def anstossen() -> None:
    """Nach dem Anlegen eines Brother-Auftrags (nach dem commit) aufrufen."""
    if _wecker is not None:
        _wecker.set()


async def _schleife() -> None:
    assert _wecker is not None
    while True:
        try:
            takt = TAKT_OFFEN_S if await durchgang() else TAKT_RUHE_S
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            takt = TAKT_UNERREICHBAR_S
        except Exception:
            # Der Dienst darf nie sterben - sonst bleiben Etiketten liegen.
            log.exception("Fehler im Brother-Druckdienst")
            takt = TAKT_UNERREICHBAR_S
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wecker.wait(), timeout=takt)
        _wecker.clear()


def start() -> None:
    global _task, _wecker
    if _task is None or _task.done():
        _wecker = asyncio.Event()
        _task = asyncio.create_task(_schleife(), name="brother")


async def stop() -> None:
    global _task, _wecker
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
    _task, _wecker = None, None


def oeffentlich(cfg: dict) -> dict[str, Any]:
    """Einstellungen ohne die grossen Profildaten - fuer die Oberflaeche."""
    profil = dict(cfg.get("profil") or {})
    profil.pop("kopf", None)
    return {**cfg, "profil": profil}
