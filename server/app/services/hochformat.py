"""Etikett im Hochformat: als Bild gezeichnet, gedreht, als Raster gedruckt.

Ein 50x30-mm-Etikett laeuft quer durch den Drucker - die 50 mm liegen unter
dem Druckkopf, die 30 mm in Laufrichtung. Gelesen werden soll es aber
hochkant, 30 mm breit und 50 mm hoch.

Mit den Mitteln des Druckers geht das nicht. `ESC V` dreht jedes Zeichen fuer
sich, die Zeile laeuft aber weiter quer ueber den Kopf: gedreht gelesen stehen
die Buchstaben dann untereinander statt nebeneinander. Deshalb zeichnet der
Server das Etikett hier selbst - in Leserichtung, mit echter Schrift - dreht
das Bild um 90 Grad und schickt es als Punktraster.

Was das kostet: ein Etikett ist rund 11 KB Bilddaten. Bei 9600 Baud sind das
etwa zehn Sekunden auf der Leitung. Damit die Nachricht klein bleibt, geht
das Bild PackBits-komprimiert hinaus (Text ist zum grossen Teil weiss); die
Firmware entpackt Streifen fuer Streifen und schickt sie verteilt ueber
mehrere Loop-Durchlaeufe, damit das Terminal waehrenddessen bedienbar bleibt.

Welcher Rasterbefehl auf einem namenlosen Drucker masshaltig druckt, ist
nicht vorher zu wissen. Der Messstreifen druckt dafuer drei Quadrate in den
drei Modi; der passende steht dann in `printer.raster_mode`.
"""

from __future__ import annotations

import base64
import io
import pathlib
from functools import lru_cache

import segno
from PIL import Image, ImageDraw, ImageFont

from . import categories
from .dates import to_display

DOTS_PER_MM = 8

# Zeilen je Rasterblock. Ein Vielfaches von 24, weil `ESC * 33` in Baendern
# von 24 Punktzeilen druckt, und klein genug, dass ein Block (48 x 48 Bytes)
# mit Luft in den Sendepuffer der Firmware passt.
BLOCK_ROWS = 48
BAND = 24

# Rand rundherum, in Punkten (1,5 mm). Weniger schneidet der Etikettenspender
# an, und der Druckkopf reicht bei 50 mm Etikett ohnehin nur ueber 48 mm.
RAND = 12
ABSTAND = 6

RASTER_MODI = ("gsv0", "esc33", "esc0")
DEFAULT_MODUS = "gsv0"

# Drehung des Hochformat-Bildes gegenueber der Druckrichtung. Welche richtig
# herum aus dem Drucker kommt, haengt davon ab, wie man das Etikett nachher
# haelt - deshalb eine Einstellung. "links": die Oberkante des Etiketts liegt
# am linken Ende des Druckkopfs.
DREHUNGEN = ("links", "rechts")

# QR-Modulgroesse in Punkten, nach der Einstellung "Codegroesse".
QR_MODUL = {"klein": 3, "mittel": 4, "gross": 5}
QR_RUHE = 2

SCHRIFTEN = pathlib.Path(__file__).resolve().parents[1] / "fonts"


@lru_cache(maxsize=64)
def _schrift(groesse: int, fett: bool) -> ImageFont.FreeTypeFont:
    datei = "DejaVuSans-Bold.ttf" if fett else "DejaVuSans.ttf"
    return ImageFont.truetype(str(SCHRIFTEN / datei), groesse)


# ------------------------------------------------------------------ PackBits
#
# Dasselbe Verfahren wie in TIFF: ein Kopfbyte n, dann bei 0..127 n+1
# woertliche Bytes, bei -127..-1 ein Byte, das 1-n mal wiederholt wird. Die
# Gegenstelle in der Firmware ist ein Dutzend Zeilen lang (Printer::unpack).
def packbits(daten: bytes) -> bytes:
    aus = bytearray()
    i, n = 0, len(daten)
    while i < n:
        # Lauf gleicher Bytes?
        j = i + 1
        while j < n and j - i < 128 and daten[j] == daten[i]:
            j += 1
        lauf = j - i
        if lauf >= 3:
            aus.append((257 - lauf) & 0xFF)
            aus.append(daten[i])
            i = j
            continue
        # Woertlich bis zum naechsten Lauf von mindestens drei Bytes.
        start = i
        while i < n and i - start < 128:
            if i + 2 < n and daten[i] == daten[i + 1] == daten[i + 2]:
                break
            i += 1
        aus.append(i - start - 1)
        aus += daten[start:i]
    return bytes(aus)


def unpackbits(daten: bytes, groesse: int) -> bytes:
    aus = bytearray()
    i = 0
    while i < len(daten) and len(aus) < groesse:
        kopf = daten[i]
        i += 1
        if kopf < 128:
            aus += daten[i:i + kopf + 1]
            i += kopf + 1
        elif kopf > 128:
            aus += bytes([daten[i]]) * (257 - kopf)
            i += 1
    return bytes(aus[:groesse])


# ------------------------------------------------------------------ Layout
def _umbrechen(text: str, schrift: ImageFont.FreeTypeFont, breite: int) -> tuple[list[str], bool]:
    """Nach Woertern umbrechen; ein zu langes Wort wird mit Trennstrich geteilt.

    Gibt die Zeilen zurueck und ob dabei ein Wort getrennt werden musste.
    """
    getrennt = False
    zeilen: list[str] = []
    aktuell = ""
    for wort in text.split():
        versuch = f"{aktuell} {wort}".strip()
        if schrift.getlength(versuch) <= breite:
            aktuell = versuch
            continue
        if aktuell:
            zeilen.append(aktuell)
            aktuell = ""
        while schrift.getlength(wort) > breite:
            getrennt = True
            teil = wort
            while len(teil) > 1 and schrift.getlength(teil + "-") > breite:
                teil = teil[:-1]
            zeilen.append(teil + "-")
            wort = wort[len(teil):]
        aktuell = wort
    if aktuell:
        zeilen.append(aktuell)
    return (zeilen or [""]), getrennt


def _zeilenhoehe(schrift: ImageFont.FreeTypeFont) -> int:
    oben, unten = schrift.getmetrics()
    return oben + unten


def _passend(text: str, breite: int, groessen, fett: bool, max_zeilen: int,
             trennen: bool = True):
    """Die groesste Schrift, in der `text` vollstaendig in `max_zeilen` passt.

    Erst ohne ein Wort zu trennen - lieber eine Stufe kleiner als
    "Schwein-efilet". Erst wenn das in keiner Groesse geht, darf getrennt
    werden. Gibt (schrift, zeilen) zurueck, oder None, wenn der Text in
    keiner der Groessen vollstaendig passt.
    """
    for darf_trennen in ((False, True) if trennen else (False,)):
        for groesse in groessen:
            schrift = _schrift(groesse, fett)
            zeilen, getrennt = _umbrechen(text, schrift, breite)
            if len(zeilen) <= max_zeilen and (darf_trennen or not getrennt):
                return schrift, zeilen
    return None


def _gekuerzt(text: str, schrift, breite: int, max_zeilen: int) -> list[str]:
    """Letzter Ausweg: sichtbar kuerzen, nie stillschweigend."""
    zeilen, _ = _umbrechen(text, schrift, breite)
    if len(zeilen) <= max_zeilen:
        return zeilen
    zeilen = zeilen[:max_zeilen]
    letzte = zeilen[-1].rstrip("-")
    while letzte and schrift.getlength(letzte + "…") > breite:
        letzte = letzte[:-1]
    zeilen[-1] = letzte.rstrip() + "…"
    return zeilen


def _menge(item: dict) -> str:
    menge = item.get("quantity")
    if menge in (None, "", 0):
        return ""
    zahl = f"{menge:g}" if isinstance(menge, (int, float)) else str(menge)
    return f"{zahl} {item.get('unit', '')}".strip()


def _qr(label: str, modul: int) -> Image.Image:
    code = segno.make_qr(label, error="m")
    matrix = [list(zeile) for zeile in code.matrix]
    seite = (len(matrix) + 2 * QR_RUHE) * modul
    bild = Image.new("L", (seite, seite), 255)
    zeichner = ImageDraw.Draw(bild)
    for y, zeile in enumerate(matrix):
        for x, punkt in enumerate(zeile):
            if punkt:
                x0 = (x + QR_RUHE) * modul
                y0 = (y + QR_RUHE) * modul
                zeichner.rectangle([x0, y0, x0 + modul - 1, y0 + modul - 1], fill=0)
    return bild


def _anordnen(item: dict, cfg: dict, breite: int, hoehe: int):
    """Was wohin kommt - ohne zu zeichnen. Getrennt von `zeichnen`, damit
    sich pruefen laesst, welcher Text auf dem Etikett steht."""
    innen = breite - 2 * RAND

    # Unten: der Code. Er bestimmt, wie viel Platz fuer den Text bleibt.
    label = item.get("label", "")
    modul = QR_MODUL.get(cfg.get("label_code_size", "mittel"), 4)
    qr = _qr(label, modul) if label else None
    if qr is not None and qr.width > innen:
        qr = _qr(label, max(2, innen // (qr.width // modul)))
    code_hoehe = qr.height if qr is not None else 0
    unten = hoehe - RAND - code_hoehe - (ABSTAND if qr is not None else 0)

    # Oben: der Text. Name und MHD sind Pflicht; was danach noch Platz hat,
    # kommt in dieser Reihenfolge dazu: Menge, Ort, Einlagerung.
    #
    # Der Ort steht an letzter Stelle der Pflicht-Kuer: das Etikett klebt ja
    # schon dort. Er hilft erst, wenn der Artikel umzieht.
    titel = categories.display_name(item.get("name", ""), item.get("subcategory", "")) or "Unbekannt"
    mhd = to_display(item.get("expiry_date", ""))
    platz = unten - RAND

    def block(art, schrift=None, zeilen=()):
        hoehe = 4 if art == "linie" else _zeilenhoehe(schrift) * len(zeilen)
        return {"art": art, "schrift": schrift, "zeilen": list(zeilen), "hoehe": hoehe}

    def mhd_bloecke(groessen):
        if not mhd:
            return [block("text", _schrift(22, False), ["kein MHD"])]
        treffer = _passend(mhd, innen, groessen, True, 1)
        schrift, zeilen = treffer if treffer else (_schrift(groessen[-1], True), [mhd])
        return [block("text", _schrift(16, False), ["MHD"]), block("text", schrift, zeilen)]

    def summe(bloecke):
        return sum(b["hoehe"] for b in bloecke) + ABSTAND * max(0, len(bloecke) - 1)

    # Name und MHD: von gross nach klein, mit zwei bis vier Zeilen Name. Ein
    # Wort zu trennen ist der vorletzte Ausweg - erst wenn es in keiner
    # Groesse ungetrennt passt. Der letzte ist sichtbares Kuerzen.
    pflicht = None
    stufen = (
        ((44, 40, 36, 32), (40, 36, 32, 30, 28)),
        ((30, 28, 26, 24), (30, 28, 26, 24)),
        ((22, 20, 18, 16), (24, 22, 20)),
    )
    for trennen in (False, True):
        for titel_zeilen in (2, 3, 4):
            for titel_groessen, mhd_groessen in stufen:
                treffer = _passend(titel, innen, titel_groessen, True, titel_zeilen, trennen)
                if treffer is None:
                    continue
                kandidat = [block("text", *treffer)] + mhd_bloecke(mhd_groessen)
                if summe(kandidat) <= platz:
                    pflicht = kandidat
                    break
            if pflicht:
                break
        if pflicht:
            break
    if pflicht is None:
        schrift = _schrift(16, True)
        pflicht = [block("text", schrift, _gekuerzt(titel, schrift, innen, 4))] + mhd_bloecke((20,))

    # Die Kuer, solange Platz ist.
    kuer = []
    menge = _menge(item)
    if menge:
        kuer.append([block("text", _schrift(22, False), _gekuerzt(menge, _schrift(22, False), innen, 1))])
    ort = item.get("location", "")
    if ort:
        treffer = _passend(ort, innen, (22, 20, 18), False, 2)
        schrift, zeilen = treffer if treffer else (_schrift(18, False), _gekuerzt(ort, _schrift(18, False), innen, 2))
        kuer.append([block("text", schrift, zeilen)])
    eingang = to_display(item.get("added_date", ""))
    if eingang:
        kuer.append([block("text", _schrift(16, False), [f"eingel. {eingang}"])])

    teile = list(pflicht)
    if kuer:
        mit_linie = teile + [block("linie")]
        for stueck in kuer:
            if summe(mit_linie + stueck) <= platz:
                mit_linie += stueck
        if len(mit_linie) > len(teile) + 1:
            teile = mit_linie

    return teile, qr, modul, label


def textzeilen(item: dict, cfg: dict, breite: int, hoehe: int) -> list[str]:
    """Alle Textzeilen, die auf dem Etikett landen, in Lesereihenfolge."""
    teile, _, _, label = _anordnen(item, cfg, breite, hoehe)
    return [z for teil in teile for z in teil["zeilen"]] + ([label] if label else [])


def zeichnen(item: dict, cfg: dict, breite: int, hoehe: int) -> Image.Image:
    """Das Etikett in Leserichtung: `breite` x `hoehe` Punkte, Graustufen.

    Oben der Name, darunter gross das MHD, dann Menge, Ort und Einlagerung,
    unten links der QR-Code mit der Etikettennummer daneben. Passt nicht
    alles, fallen erst Einlagerung, dann Ort und Menge weg; Name, MHD und
    Code bleiben immer.
    """
    teile, qr, modul, label = _anordnen(item, cfg, breite, hoehe)
    bild = Image.new("L", (breite, hoehe), 255)
    zeichner = ImageDraw.Draw(bild)

    y = RAND
    for teil in teile:
        if teil["art"] == "linie":
            zeichner.line([RAND, y + 1, breite - RAND, y + 1], fill=0, width=2)
        else:
            yy = y
            for zeile in teil["zeilen"]:
                zeichner.text((RAND, yy), zeile, font=teil["schrift"], fill=0)
                yy += _zeilenhoehe(teil["schrift"])
        y += teil["hoehe"] + ABSTAND

    if qr is not None:
        y0 = hoehe - RAND - qr.height
        bild.paste(qr, (RAND - QR_RUHE * modul // 2, y0))
        # Die Etikettennummer neben dem Code - fuer den Fall, dass sich der
        # Code einmal nicht scannen laesst.
        rechts = RAND + qr.width
        frei = breite - RAND - rechts
        praefix, nummer = label.rstrip("0123456789"), label[len(label.rstrip("0123456789")):]
        if frei >= 48 and nummer:
            klein, gross = _schrift(16, False), _schrift(20, True)
            if gross.getlength(nummer) > frei:
                gross = _schrift(16, True)
            mitte = y0 + qr.height // 2
            if praefix:
                zeichner.text((rechts + 4, mitte - _zeilenhoehe(klein)), praefix, font=klein, fill=0)
            zeichner.text((rechts + 4, mitte), nummer, font=gross, fill=0)
    return bild


# ------------------------------------------------------------ zum Drucker
def einfarbig(bild: Image.Image) -> Image.Image:
    """Graustufen hart auf Schwarz/Weiss - ein Thermodrucker kennt nichts dazwischen."""
    return bild.point(lambda v: 0 if v < 150 else 255).convert("1", dither=Image.Dither.NONE)


def gedreht(bild: Image.Image, drehung: str) -> Image.Image:
    """Hochformat -> Druckrichtung. Breite des Ergebnisses = Druckkopf,
    Hoehe = Zeilen in Laufrichtung."""
    if drehung == "rechts":
        return bild.transpose(Image.Transpose.ROTATE_270)
    return bild.transpose(Image.Transpose.ROTATE_90)


def raster_bloecke(druckbild: Image.Image, modus: str) -> list[dict]:
    """Das Druckbild in Rasterbloecke zu BLOCK_ROWS Zeilen zerlegen.

    Leere Streifen werden zu einem Vorschub - das spart Leitungszeit, und bei
    9600 Baud ist Leitungszeit die Druckzeit. Jeder Block ist nur so breit,
    wie er Schwarz enthaelt (vom linken Rand aus gezaehlt, denn `GS v 0`
    beginnt immer dort).
    """
    # Schwarz = 1, wie der Drucker es erwartet. Im Modus "1" von Pillow ist
    # Weiss 1, also einmal umdrehen.
    breite, hoehe = druckbild.size
    zeilenbytes = (breite + 7) // 8
    roh = bytes(b ^ 0xFF for b in druckbild.tobytes())
    rest = breite % 8
    if rest:
        # Die aufgefuellten Bits am Zeilenende waren weiss und sind durch das
        # Umdrehen schwarz geworden.
        maske = (0xFF << (8 - rest)) & 0xFF
        roh = bytes(
            b & maske if (i % zeilenbytes) == zeilenbytes - 1 else b
            for i, b in enumerate(roh)
        )

    bloecke: list[dict] = []
    for y0 in range(0, hoehe, BLOCK_ROWS):
        zeilen = min(BLOCK_ROWS, hoehe - y0)
        streifen = roh[y0 * zeilenbytes:(y0 + zeilen) * zeilenbytes]
        genutzt = 0
        for z in range(zeilen):
            zeile = streifen[z * zeilenbytes:(z + 1) * zeilenbytes]
            for i in range(zeilenbytes - 1, -1, -1):
                if zeile[i]:
                    genutzt = max(genutzt, i + 1)
                    break
        if genutzt == 0:
            if bloecke and bloecke[-1]["t"] == "feed":
                bloecke[-1]["dots"] += zeilen
            else:
                bloecke.append({"t": "feed", "dots": zeilen})
            continue
        daten = b"".join(
            streifen[z * zeilenbytes:z * zeilenbytes + genutzt] for z in range(zeilen)
        )
        bloecke.append({
            "t": "raster",
            "w": genutzt * 8,
            "h": zeilen,
            "mode": modus,
            "z": "rle",
            "d": base64.b64encode(packbits(daten)).decode("ascii"),
        })
    return bloecke


def masse(zeilen_budget: int, kopf_dots: int) -> tuple[int, int]:
    """(Breite, Hoehe) des Hochformat-Bildes in Punkten.

    Die Breite ist die Laenge in Laufrichtung - abgerundet auf ganze 24er
    Baender, sonst druckt `ESC * 33` das letzte Band zu lang und verschiebt
    jedes Folgeetikett. Die Hoehe ist der Druckkopf.
    """
    breite = max(BAND, zeilen_budget - zeilen_budget % BAND)
    return breite, kopf_dots


def bloecke(item: dict, cfg: dict, zeilen_budget: int, kopf_dots: int) -> list[dict]:
    breite, hoehe = masse(zeilen_budget, kopf_dots)
    bild = einfarbig(zeichnen(item, cfg, breite, hoehe))
    modus = cfg.get("raster_mode", DEFAULT_MODUS)
    if modus not in RASTER_MODI:
        modus = DEFAULT_MODUS
    return raster_bloecke(gedreht(bild, cfg.get("label_portrait_turn", "links")), modus)


def vorschau_svg(item: dict, cfg: dict, zeilen_budget: int, kopf_dots: int) -> str:
    """Genau das Bild, das gedruckt wird - in Leserichtung, als SVG verpackt,
    damit die Oberflaeche es wie die anderen Vorschauen behandeln kann."""
    breite, hoehe = masse(zeilen_budget, kopf_dots)
    bild = einfarbig(zeichnen(item, cfg, breite, hoehe)).convert("L")
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG", optimize=True)
    daten = base64.b64encode(puffer.getvalue()).decode("ascii")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {breite} {hoehe}" '
        f'width="{breite / DOTS_PER_MM * 4}" height="{hoehe / DOTS_PER_MM * 4}" '
        'role="img" aria-label="Etikettenvorschau Hochformat">'
        '<rect width="100%" height="100%" rx="12" fill="#fff" stroke="#c8d0d6"/>'
        f'<image width="{breite}" height="{hoehe}" '
        f'href="data:image/png;base64,{daten}" style="image-rendering:pixelated"/>'
        "</svg>"
    )
