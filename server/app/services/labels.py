"""Vergabe der Etikettennummern (LebNummer) und Etiketten-Rendering.

Der Zaehler liegt in der Datenbank und wird unter einem asyncio-Lock
hochgezaehlt. Im ersten Projekt lag er im NVS des ESP32 - nach einem
Factory-Reset begann die Nummerierung wieder bei 1 und kollidierte mit
Etiketten, die physisch noch im Schrank klebten.
"""

from __future__ import annotations

import asyncio
import base64
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import mengen
from . import settings_store
from . import categories
from . import hochformat
from .dates import to_display
from .zeichen import latin1, latin1_tief

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


async def next_labels(session: AsyncSession, count: int = 1) -> list[str]:
    """`count` fortlaufende, garantiert eindeutige Etikettennummern.

    Zaehlt bewusst **nicht** die Rolle mit: eine Nummer zu vergeben und Papier
    zu verbrauchen sind zwei verschiedene Dinge. Dafuer gibt es consume_roll().
    """
    async with _lock:
        start = int(await settings_store.get(session, "label_counter", 0))
        labels = [f"{settings.label_prefix}{start + i + 1:06d}" for i in range(count)]
        await settings_store.put(session, "label_counter", start + count)
        await session.commit()
    return labels


async def consume_roll(session: AsyncSession, count: int = 1) -> None:
    """Verbrauchte Etiketten der Rolle anrechnen - je Druckauftrag eines.

    Das haing frueher an next_labels(), also am Vergeben der Nummer. Damit
    zaehlte die Rolle in beide Richtungen falsch: ein Eintrag, der ohne Haken
    bei "drucken" angelegt wurde, verbrauchte Papier, das nie durch den
    Drucker lief - und ein Nachdruck verbrauchte keins, obwohl er es tat. Bei
    abgeschaltetem Drucker lief der Zaehler sogar durchgehend mit.

    Kein Commit hier: der Aufrufer schreibt die Bestandsaenderung und den
    Verbrauch zusammen fest, sonst steht die Rolle auf einem Stand, zu dem es
    keinen Eintrag gibt.
    """
    if count <= 0:
        return
    async with _lock:
        used = int(await settings_store.get(session, "roll_used", 0))
        await settings_store.put(session, "roll_used", used + count)


async def roll_state(session: AsyncSession) -> dict:
    size = int(await settings_store.get(session, "roll_size", 0))
    used = int(await settings_store.get(session, "roll_used", 0))
    return {
        "size": size,
        "used": used,
        "remaining": (size - used) if size > 0 else -1,
    }


async def new_roll(session: AsyncSession, size: int) -> dict:
    await settings_store.put(session, "roll_size", max(0, size))
    await settings_store.put(session, "roll_used", 0)
    await session.commit()
    return await roll_state(session)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word[:width]
            while len(word) > width:
                word = word[width:]
                if word:
                    lines.append(current)
                    current = word[:width]
    if current:
        lines.append(current)
    return lines or [""]


# --------------------------------------------------------------------- Masse
#
# Ein Thermodrucker rechnet in Punkten, nicht in Millimetern: 203 dpi sind
# 8 Punkte je Millimeter. Ein 30 mm hohes Etikett hat damit 240 Punkte, und
# mehr passt eben nicht drauf. Genau diese Rechnung fehlte bisher - das
# Layout wurde einfach ausgegeben und lief ueber drei Etiketten.
DOTS_PER_MM = 8

# Zeilenhoehen in Punkten.
#
# Diese Zahlen waren bisher eine Hoffnung: der Server rechnete mit ihnen, der
# Drucker nahm aber seinen eigenen Zeilenabstand. `ESC @` (Printer::reset)
# stellt den Standard ein - laut Spezifikation 1/6 Zoll, bei 203 dpi also rund
# 34 Punkte. 24 ist exakt die Schrifthoehe von Font A, also die Annahme, der
# Drucker setze voellig ohne Durchschuss. Das tut keiner.
#
# Beim klassischen Zuschnitt auf 50x30 mm ergab das 314 statt 240 Punkten -
# 9,2 mm Ueberlauf je Etikett, nach drei Stueck ein ganzes Etikett Versatz.
# Genau das war "Folgeetiketten wandern", und Totbereich, Rueckzug und
# SAFETY_DOTS waren allesamt Gegenmittel gegen ein Symptom, dessen Ursache
# diese Rechnung selbst war.
#
# Jetzt sind sie verbindlich: render_label() schreibt die hier berechnete
# Hoehe in jeden Block, und die Firmware stellt den Zeilenabstand vor jeder
# Zeile ausdruecklich darauf ein (`ESC 3 n`). Damit stimmt total_dots() nicht
# mehr ungefaehr, sondern per Konstruktion.
LINE_DOTS = 24        # Schrift A, 12x24 Punkte
LINE_DOTS_LARGE = 48  # doppelte Hoehe
SEP_DOTS = 24

# Der Strichcode-Vorschub stand auf 10 und war geraten - tatsaechlich kam die
# volle Zeilenhoehe des `\r\n` dazu. Die Firmware setzt den Abstand jetzt fuer
# die Dauer des Strichcodes auf 0; `GS k` schiebt selbst genau seine Hoehe
# vor, mehr nicht.
CODE128_FEED = 0

# QR: Ruhezone je Seite in Modulen, und die Bandhoehe von `ESC *`.
#
# Die Firmware druckte je Modulzeile fest 4 Punkte (0xF0/0x0F), waagerecht
# aber `scale` Punkte. Quadratisch war der Code damit nur bei Skalierung 4;
# bei der Vorgabe "mittel" war er 3:4 gestaucht, bei "klein" 2:4. Eine
# Ruhezone wurde gar nicht gedruckt - die "+4" in der alten Rechnung tat nur
# so. Beides zusammen erklaert Codes, die kein Scanner liest.
QR_QUIET = 2
QR_BAND = 8

# Der Etikettenspender trifft die Perforation nicht auf den Punkt genau.
# Ohne Reserve rutscht die letzte Zeile auf das naechste Etikett.
SAFETY_DOTS = 16

# Zwischen Druckkopf und Abrisskante liegt ein Stueck Papier, das nie bedruckt
# werden kann - beim Abreissen geht es verloren. Faehrt der Drucker vor dem
# Druck ein Stueck zurueck, laesst sich der Bereich zurueckholen.
#
# Rueckwaerts kann aber nicht jeder ESC/POS-Drucker: viele ignorieren ESC j,
# manche verhaken das Papier dabei. Deshalb aus und nur auf Ansage. Mehr als
# 100 Punkte (12,5 mm) ist bei keinem Totbereich noetig und erhoeht nur das
# Risiko, dass das Etikett aus der Fuehrung rutscht.
MAX_BACKFEED_DOTS = 100



def block_dots(block: dict) -> int:
    """Hoehe eines Blocks in Punkten - dieselbe Rechnung wie in der Firmware."""
    kind = block.get("t")
    if kind == "text":
        if block.get("vorschub"):
            # Gedruckter Vorschub: eine Leerzeile mit genau dem Rest als
            # Zeilenabstand (siehe _vorschub_bloecke).
            return int(block.get("h", LINE_DOTS))
        return LINE_DOTS_LARGE if block.get("large") else LINE_DOTS
    if kind == "row":
        return LINE_DOTS
    if kind == "sep":
        return SEP_DOTS
    if kind == "code128":
        return int(block.get("height", 40)) + CODE128_FEED
    if kind == "qr":
        # Version 1 (21 Module) fasst eine Etikettennummer bequem. Dazu die
        # Ruhezone, und aufgerundet auf ganze Baender - `ESC *` druckt immer
        # acht Punktzeilen auf einmal, weniger geht nicht.
        side = (21 + 2 * QR_QUIET) * int(block.get("scale", 3))
        return -(-side // QR_BAND) * QR_BAND
    if kind == "raster":
        # `ESC *` druckt in Baendern von acht Punktzeilen; ein angefangenes
        # Band kostet trotzdem die vollen acht.
        return -(-int(block.get("h", 0)) // QR_BAND) * QR_BAND
    if kind in ("feed", "form"):
        return int(block.get("dots", 0))
    if kind == "raw":
        # Rohe Bytes (Rueckzug mit ESC K): `h` ist, was sie vorschieben.
        return int(block.get("h", 0))
    if kind == "back":
        # Rueckzug zaehlt negativ. Dadurch stimmt total_dots() weiterhin mit
        # dem Papiervorschub ueberein, den ein Etikett insgesamt verursacht -
        # und genau daran haengt, ob das naechste Etikett wieder oben anfaengt.
        return -int(block.get("dots", 0))
    return 0


def total_dots(blocks: list[dict]) -> int:
    return sum(block_dots(b) for b in blocks)


def _fit(blocks: list[dict], budget: int) -> list[dict]:
    """Blocks von hinten kuerzen, bis das Etikett passt.

    Die Reihenfolge ist die Rangfolge: was hinten steht, faellt zuerst weg.
    Der Code zum Auslagern ist davon ausgenommen - ohne ihn ist das Etikett
    wertlos, weil sich der Eintrag dann nicht mehr wegscannen laesst.
    """
    def geschuetzt(block: dict) -> bool:
        # Der Code zum Auslagern und alles, was ein Layout ausdruecklich
        # behalten will (das MHD im klassischen Zuschnitt).
        return block.get("t") in ("qr", "code128") or bool(block.get("keep"))

    out = list(blocks)
    while total_dots(out) > budget:
        droppable = [
            i for i, b in enumerate(out) if not geschuetzt(b) and b.get("t") != "feed"
        ]
        if not droppable:
            break
        out.pop(droppable[-1])
    return out


# ------------------------------------------------------------------ Layouts
#
# Vier Zuschnitte fuer dasselbe Etikett. Sie unterscheiden sich darin, was bei
# 240 Punkten Platz Vorrang hat - alles zugleich geht nicht.
# Den Code nennen die Beschreibungen bewusst nicht: welcher gedruckt wird,
# haengt an der Ausrichtung (siehe _code_blocks) und stuende hier sonst falsch.
LAYOUTS: dict[str, str] = {
    "klassisch": "Wie im Vorgängerprojekt: Name groß, feste Zeilen, Code.",
    "zeile": "Name und MHD in einer Zeile. Der kürzeste Zuschnitt.",
    "sparsam": "Name und MHD untereinander, sonst nichts.",
    "kompakt": "Name und MHD groß. Aus zwei Metern lesbar.",
    "standard": "Name groß, dazu MHD, Menge und Ort. Der Allrounder.",
    "vollstaendig": "Alle Angaben in normaler Schrift.",
    "hochformat": "Um 90° gedreht: hochkant gelesen, Name und MHD groß, QR unten. "
                  "Als Bild gedruckt – bei 9600 Baud rund 10 s.",
    "eigen": "Selbst zusammengestellt: welche Angaben, in welcher Reihenfolge, wie groß.",
}

# Anzeigename fuer die Oberflaeche - der Schluessel bleibt umlautfrei, damit
# er unveraendert in der Datenbank stehen kann.
TITLES: dict[str, str] = {
    "klassisch": "Klassisch",
    "zeile": "Einzeiler",
    "kompakt": "Kompakt",
    "standard": "Standard",
    "vollstaendig": "Vollständig",
    "sparsam": "Sparsam",
    "hochformat": "Hochformat",
    "eigen": "Eigene",
}

# Layouts, die als Bild gedruckt werden. Sie brauchen eine Firmware, die
# komprimierte Rasterstreifen versteht und ueber mehrere Durchlaeufe
# verteilt sendet - eine aeltere druckt statt dessen leere Etiketten.
BILD_LAYOUTS = ("hochformat",)
BILD_FAEHIGKEIT = "raster2"
RAW_FAEHIGKEIT = "raw"
DEFAULT_LAYOUT = "klassisch"

# Geschwindigkeit der Leitung zum Drucker. 9600 ist der Werkszustand der
# meisten Bondrucker und alles, was eine Firmware ohne die Faehigkeit "baud"
# kann. Die Zahl muss zu der passen, auf die der Drucker selbst eingestellt
# ist (steht auf seiner Selbsttestseite) - sonst kommt nur Zeichensalat.
BAUDRATEN = (9600, 19200, 38400, 57600, 115200)
BAUD_STANDARD = 9600
BAUD_FAEHIGKEIT = "baud"


def baudrate(printer_cfg: dict) -> int:
    """Die eingestellte Baudrate - oder 9600, wenn dort Unbrauchbares steht."""
    try:
        baud = int(printer_cfg.get("baud", BAUD_STANDARD))
    except (TypeError, ValueError):
        return BAUD_STANDARD
    return baud if baud in BAUDRATEN else BAUD_STANDARD


def is_rotated(cfg: dict) -> bool:
    """Soll der Text um 90 Grad gedreht werden?

    Frueher hing das an einer Einstellung "Quer/Hoch". Das war irrefuehrend,
    weil dieses eine Wort zwei voellig verschiedene Dinge zusammenwarf: ob der
    Text gedreht wird, und welche Etikettenkante durch den Drucker laeuft. Die
    beiden haben nichts miteinander zu tun - das eine ist Geschmack, das andere
    haengt daran, wie die Rolle gewickelt ist. Jetzt sind es zwei Schalter.
    """
    if "label_rotate" in cfg:
        return bool(cfg["label_rotate"])
    return cfg.get("label_orientation", "quer") != "hoch"


def feed_and_line_mm(cfg: dict) -> tuple[float, float]:
    """(Vorschub, Zeilenlaenge) in Millimetern.

    Die Vorschubkante ist die Kante, die in Papierrichtung laeuft - sie
    begrenzt, wie viel Inhalt auf ein Etikett passt. Die andere Kante laeuft
    ueber den Druckkopf und begrenzt die Zeilenlaenge. Welche das ist, gibt die
    Rolle vor und nicht die Textrichtung; deshalb steht es als eigene
    Einstellung da, statt aus der Ausrichtung geraten zu werden.
    """
    breite = float(cfg.get("label_width_mm", 50))
    hoehe = float(cfg.get("label_height_mm", 30))

    kante = cfg.get("label_feed_edge")
    if kante == "breite":
        return breite, hoehe
    if kante == "hoehe":
        return hoehe, breite

    # Altbestand ohne die neue Einstellung: aus der alten Ausrichtung ableiten.
    if is_rotated(cfg):
        return hoehe, breite
    return breite, hoehe


def _title(item: dict) -> str:
    # Unterkategorie gehoert an den Namen, nicht in eine eigene Zeile: auf dem
    # Etikett soll "Filet - Schwein" stehen, damit im Schrank erkennbar ist,
    # was fuer ein Filet das ist.
    return categories.display_name(item.get("name", ""), item.get("subcategory", ""))


def _title_blocks(title: str, chars: int, large: bool) -> list[dict]:
    """Den Namen setzen - gross, wenn er gross passt.

    Doppelte Hoehe bedeutet beim ESC/POS-Drucker auch doppelte Breite: in eine
    grosse Zeile passt nur die Haelfte der Zeichen. "Schweinefilet - Schwein"
    wurde deshalb hart auf "Schweinefilet - " abgeschnitten. Passt der Name
    nicht in eine grosse Zeile, wird er lieber normal gross und dafuer
    vollstaendig gesetzt - der Name ist die wichtigste Angabe auf dem Etikett.
    """
    if large and len(title) <= max(1, chars // 2):
        return [{"t": "text", "v": title, "align": 1, "bold": True, "large": True}]
    return [
        {"t": "text", "v": line, "align": 1, "bold": True}
        for line in _wrap(title, chars)[:2]
    ]


def _quantity(item: dict) -> str:
    return mengen.text(item.get("quantity", 1), item.get("unit", ""))


# Wie gross der Code ausfaellt. Der Unterschied ist auf einem 30-mm-Etikett
# erheblich: ein QR ist quadratisch und kostet so viel Hoehe wie Breite, ein
# Strichcode ist flach und kostet nur seine Hoehe.
#
#   Groesse   QR            Strichcode
#   klein     50 Pt (6,2mm) 34 Pt (4,2mm)
#   mittel    75 Pt (9,4mm) 40 Pt (5,0mm)
#   gross    100 Pt (12,5mm) 50 Pt (6,2mm)
CODE_SIZES: dict[str, tuple[int, int]] = {
    # Name: (QR-Skalierung, Strichcodehoehe)
    "klein": (2, 24),
    "mittel": (3, 30),
    "gross": (4, 40),
}
DEFAULT_CODE_SIZE = "mittel"


def code_choice(cfg: dict) -> str:
    """Welcher Code gedruckt wird: "qr" oder "code128".

    "automatisch" heisst Strichcode: er kostet ein Drittel der Hoehe eines
    QR-Codes. Das Hochformat nimmt immer einen QR (services/hochformat.py),
    weil ein Strichcode auf 30 mm Breite nicht lesbar hinpasst.

    Frueher hing "automatisch" am Schalter "Text um 90 Grad drehen". Der kam
    beim Drucker aber nie an - `rotate` stand nicht in der Druckauftrag-
    Nachricht (protocol.print_job), ESC V wurde also nie gesendet. Der
    Schalter waehlte nur heimlich den Code um.
    """
    wahl = cfg.get("label_code", "auto")
    if wahl in ("qr", "code128"):
        return wahl
    return "code128"


def _code_blocks(item: dict, cfg: dict) -> list[dict]:
    """Der Code zum Auslagern - QR oder Strichcode, nie beides.

    Beides nebeneinander geht auf einem ESC/POS-Drucker nicht (er kennt nur
    Zeilen), und beides untereinander frisst die halbe Etikettenhoehe fuer
    dieselbe Information.
    """
    label = item.get("label", "")
    if not label:
        return []

    scale, height = CODE_SIZES.get(
        cfg.get("label_code_size", DEFAULT_CODE_SIZE), CODE_SIZES[DEFAULT_CODE_SIZE]
    )
    if code_choice(cfg) == "qr":
        return [{"t": "qr", "v": label, "scale": scale}]
    return [{"t": "code128", "v": label, "height": height}]


# ------------------------------------------------------- eigene Aufteilung
# Felder, die man auf ein eigenes Etikett setzen kann: Schluessel ->
# (Bezeichnung auf dem Etikett, Wert aus dem Artikel).
def _feld_werte(item: dict, cfg: dict) -> dict[str, tuple[str, str]]:
    menge = item.get("quantity", 1)
    return {
        "name": ("Name", item.get("name", "") or "Unbekanntes Produkt"),
        "name_sorte": ("Name", _title(item) or "Unbekanntes Produkt"),
        "subcategory": ("Sorte", item.get("subcategory", "")),
        "category": ("Kategorie", item.get("category", "")),
        "brand": ("Marke", item.get("brand", "")),
        "expiry_date": ("MHD", to_display(item.get("expiry_date", ""))),
        "added_date": ("Eingelagert", to_display(item.get("added_date", ""))),
        "count": ("Stück", f"{menge:g}" if isinstance(menge, (int, float)) else str(menge)),
        "quantity": ("Menge", _quantity(item)),
        "location": ("Ort", item.get("location", "")),
        "household": ("Haushalt", item.get("household", "")
                      or latin1(cfg.get("household") or settings.household)),
        "label": ("Nummer", item.get("label", "")),
    }


# Fuer die Oberflaeche: was waehlbar ist. Reihenfolge = Reihenfolge im Menue.
EIGEN_FELDER: dict[str, str] = {
    "name": "Lebensmittelname",
    "name_sorte": "Name mit Sorte",
    "name_marke": "Name + Marke (zwei Zeilen, falls Marke da)",
    "subcategory": "Sorte",
    "category": "Kategorie",
    "brand": "Marke",
    "expiry_date": "MHD",
    "added_date": "Einlagerungsdatum",
    "daten": "Eingelagert + MHD (eine Zeile)",
    "count": "Stückzahl",
    "quantity": "Menge mit Einheit",
    "location": "Ort",
    "menge_ort": "Menge + Ort (eine Zeile)",
    "household": "Haushalt",
    "label": "Etikettennummer",
    "linie": "— Trennlinie —",
    "code": "Code (Strich/QR)",
}
EIGEN_STILE: dict[str, str] = {
    "zeile": "Bezeichnung … Wert",
    "gross": "groß, mittig",
    "fett": "fett, mittig",
    "text": "normal, mittig",
    "links": "normal, links",
}
EIGEN_STANDARD: list[dict] = [
    {"feld": "name_sorte", "stil": "gross"},
    {"feld": "expiry_date", "stil": "zeile", "unterstrichen": True},
    {"feld": "quantity", "stil": "zeile"},
    {"feld": "location", "stil": "zeile"},
    {"feld": "added_date", "stil": "zeile"},
    {"feld": "code", "stil": "zeile"},
]


# Kombinierte Felder: zwei Angaben, die sich eine Zeile teilen oder sich je
# nach Artikel anders setzen. Auf einem 30-mm-Etikett ist jede gesparte Zeile
# 3 mm - das ist der ganze Zweck.
def _kurzdatum(wert: str) -> str:
    """`16.10.2026` -> `16.10.26`, wenn die Zeile sonst nicht passt."""
    return wert[:6] + wert[8:] if len(wert) == 10 and wert[2] == "." else wert


def _kombi_name_marke(item: dict, cfg: dict, chars: int, stil: str, _u: bool) -> list[dict]:
    # Ohne Marke wie ein gewoehnlicher Name im gewaehlten Stil (Vorgabe gross).
    # Mit Marke zwei normale Zeilen: zusammen so hoch wie ein grosser Name,
    # aber mit einer Angabe mehr.
    name = _title(item) or "Unbekanntes Produkt"
    marke = item.get("brand", "")
    align = 0 if stil == "links" else 1
    if not marke:
        if stil in ("gross", "zeile"):
            return _title_blocks(name, chars, large=True)
        return [{"t": "text", "v": name, "align": align, "bold": stil != "text"}]
    # Ein ueberlanger Name darf zwei Zeilen nehmen - abgeschnitten waere er
    # wertlos. Die Marke ist Beiwerk und bleibt bei einer.
    return [*({"t": "text", "v": z, "align": align, "bold": True} for z in _wrap(name, chars)[:2]),
            {"t": "text", "v": _wrap(marke, chars)[0], "align": align, "bold": False}]


def _kombi_daten(item: dict, cfg: dict, chars: int, _s: str, unterstrichen: bool) -> list[dict]:
    mhd = to_display(item.get("expiry_date", ""))
    ein = to_display(item.get("added_date", ""))
    if not ein:
        zeile = {"t": "row", "k": "MHD", "v": mhd or "-"}
    else:
        k, v = f"Ein {ein}", f"MHD {mhd or '-'}"
        if len(k) + len(v) + 1 > chars:
            k, v = f"Ein {_kurzdatum(ein)}", f"MHD {_kurzdatum(mhd) or '-'}"
        zeile = {"t": "row", "k": k, "v": v}
    zeile.update(underline=unterstrichen, keep=True)
    return [zeile]


def _kombi_menge_ort(item: dict, cfg: dict, chars: int, _s: str, unterstrichen: bool) -> list[dict]:
    menge, ort = _quantity(item), item.get("location", "")
    if menge and ort:
        return [{"t": "row", "k": menge, "v": ort, "underline": unterstrichen}]
    if menge or ort:
        return [{"t": "row", "k": "Menge" if menge else "Ort", "v": menge or ort,
                 "underline": unterstrichen}]
    return []


KOMBI = {
    "name_marke": _kombi_name_marke,
    "daten": _kombi_daten,
    "menge_ort": _kombi_menge_ort,
}


def _eigen(item: dict, cfg: dict, chars: int) -> list[dict]:
    """Die eigene Aufteilung aus `printer.label_custom`.

    Leere Felder fallen weg, statt eine Zeile "Ort ...... " zu drucken. Was
    nicht passt, kuerzt _fit() wie bei den festen Aufteilungen von hinten -
    das MHD und der Code bleiben stehen.
    """
    zeilen = cfg.get("label_custom")
    if not isinstance(zeilen, list) or not zeilen:
        zeilen = EIGEN_STANDARD
    werte = _feld_werte(item, cfg)
    blocks: list[dict] = []
    code_gesetzt = False
    for zeile in zeilen:
        if not isinstance(zeile, dict):
            continue
        feld, stil = zeile.get("feld"), zeile.get("stil", "zeile")
        if feld == "linie":
            blocks.append({"t": "sep"})
            continue
        if feld == "code":
            if not code_gesetzt:
                blocks += _code_blocks(item, cfg)
                code_gesetzt = True
            continue
        if feld in KOMBI:
            blocks += KOMBI[feld](item, cfg, chars, stil, bool(zeile.get("unterstrichen")))
            continue
        if feld not in werte:
            continue
        bezeichnung, wert = werte[feld]
        if not wert:
            continue
        schutz = feld == "expiry_date"
        if stil == "gross":
            neu = _title_blocks(wert, chars, large=True)
        elif stil in ("fett", "text", "links"):
            neu = [{"t": "text", "v": wert, "align": 0 if stil == "links" else 1,
                    "bold": stil == "fett"}]
        else:
            neu = [{"t": "row", "k": bezeichnung, "v": wert,
                    "underline": bool(zeile.get("unterstrichen"))}]
        if schutz:
            for b in neu:
                b["keep"] = True
        blocks += neu
    return blocks


def _build(layout: str, item: dict, cfg: dict, chars: int) -> list[dict]:
    title = _title(item)
    expiry = to_display(item.get("expiry_date", ""))
    label = item.get("label", "")
    location = item.get("location", "")
    brand = item.get("brand", "")

    if layout == "eigen":
        return _eigen(item, cfg, chars)

    if layout == "klassisch":
        # 1:1 der Zuschnitt aus dem Vorgaengerprojekt (LabelRenderer.cpp):
        # Name fett und doppelt hoch, darunter eine feste Zeilenfolge, unten
        # ein Code 128 ohne Klartextzeile. Kategorie und Sorte stehen dort
        # zusammen in einer Zeile ("Fleisch & Fisch / Schwein") und nicht am
        # Namen - deshalb hier _title() nicht verwendet.
        #
        # Auf Endlospapier war die Hoehe damals egal. Auf einem 30-mm-Etikett
        # ist sie es nicht: passt die Folge nicht, faellt sie von unten nach
        # weg (Haushalt zuerst, MHD und Name zuletzt). Der Code bleibt immer.
        blocks = _title_blocks(item.get("name", "") or "Unbekanntes Produkt",
                               chars, large=True)

        kategorie = " / ".join(
            teil for teil in (item.get("category", ""), item.get("subcategory", ""))
            if teil and teil != "Barcode"
        )
        if kategorie:
            blocks.append({"t": "row", "k": "Kategorie", "v": kategorie})
        blocks.append({"t": "row", "k": "Einlagerung",
                       "v": to_display(item.get("added_date", ""))})
        # Reihenfolge wie im Original. Das MHD steht darin in der Mitte und
        # fiele beim Kuerzen vor Menge und Haushalt weg - dabei ist es der
        # Grund, warum das Etikett ueberhaupt klebt. Deshalb geschuetzt.
        blocks.append({"t": "row", "k": "MHD", "v": expiry or "-",
                       "underline": True, "keep": True})
        blocks.append({"t": "row", "k": "Menge", "v": _quantity(item) or "1 St."})
        # Der Haushalt des Artikels - seit es mehrere gibt, ist das nicht
        # mehr eine Einstellung fuer alle.
        haushalt = latin1(item.get("household") or cfg.get("household") or settings.household)
        if haushalt:
            blocks.append({"t": "row", "k": "Haushalt", "v": haushalt})
        return blocks + _code_blocks(item, cfg)

    if layout == "zeile":
        # Sparsamster Zuschnitt: Name und MHD teilen sich eine Zeile. Der Code
        # steht direkt darunter, die Etikettennummer entfaellt - sie steckt im
        # Code, und als Klartext kostet sie 3 mm.
        #
        # Passt der Name nicht daneben, bekommt er eine eigene Zeile. Drei
        # Millimeter mehr sind das kleinere Uebel: ein abgeschnittener Name
        # macht das Etikett im Schrank wertlos, und genau dafuer klebt es dort.
        platz = chars - len(expiry or "kein MHD") - 2
        if len(title) <= platz:
            blocks: list[dict] = [{"t": "row", "k": title, "v": expiry or "kein MHD"}]
        else:
            blocks = _title_blocks(title, chars, large=False)
            blocks.append({"t": "row", "k": "MHD", "v": expiry or "-", "underline": True})
        return blocks + _code_blocks(item, cfg)

    if layout == "kompakt":
        blocks = _title_blocks(title, chars, large=True)
        if expiry:
            blocks.append({"t": "text", "v": expiry, "align": 1, "bold": True, "large": True})
        blocks += _code_blocks(item, cfg)
        return blocks

    if layout == "sparsam":
        blocks = _title_blocks(title, chars, large=False)
        if expiry:
            blocks.append({"t": "row", "k": "MHD", "v": expiry, "underline": True})
        return blocks + _code_blocks(item, cfg)

    if layout == "vollstaendig":
        blocks = _title_blocks(title, chars, large=False)
        if brand:
            blocks.append({"t": "text", "v": brand, "align": 1})
        if expiry:
            blocks.append({"t": "row", "k": "MHD", "v": expiry, "underline": True})
        blocks.append({"t": "row", "k": "Menge", "v": _quantity(item)})
        if location:
            blocks.append({"t": "row", "k": "Ort", "v": location})
        blocks.append({"t": "row", "k": "Eingang", "v": to_display(item.get("added_date", ""))})
        blocks += _code_blocks(item, cfg)
        blocks.append({"t": "text", "v": label, "align": 1})
        return blocks

    # standard
    blocks = _title_blocks(title, chars, large=True)
    if expiry:
        blocks.append({"t": "row", "k": "MHD", "v": expiry, "underline": True})
    info = " · ".join(x for x in (_quantity(item), location) if x)
    if info:
        blocks.append({"t": "text", "v": info, "align": 1})
    blocks += _code_blocks(item, cfg)
    blocks.append({"t": "text", "v": label, "align": 1})
    return blocks



# Ein leeres Bild als Vorschub: so schmal wie moeglich (8 Punkte = 1 Byte je
# Zeile), in ganzen Baendern zu 24 Zeilen - `ESC * 33` druckt nur ganze Baender.
LEERBILD_BREITE = 8
LEERBILD_BAND = 24


def _leeres_bild(zeilen: int, cfg: dict) -> dict:
    roh = bytes((LEERBILD_BREITE // 8) * zeilen)
    return {"t": "raster", "w": LEERBILD_BREITE, "h": zeilen,
            "mode": cfg.get("raster_mode", "gsv0"), "z": "rle",
            "d": base64.b64encode(hochformat.packbits(roh)).decode("ascii")}


def _vorschub_bloecke(dots: int, art: str, schritt: int, cfg: dict) -> list[dict]:
    """Einen Vorschub von `dots` Punkten so ausdruecken, wie eingestellt.

    leerzeilen: Zeilen aus einem Leerzeichen, je 24 Punkte - genau die Hoehe
                der Schrift, die ganze Bewegung ist also Druckbewegung.
    bild:       ein leeres Rasterbild im eingestellten Bildmodus.
    Was nicht in ganze 24 Punkte aufgeht (hoechstens 23), wird ebenfalls
    gedruckt: eine Leerzeile, deren Zeilenabstand genau der Rest ist. Frueher
    blieb der Rest ein ESC J - und genau der lief am Ende jedes Etiketts als
    kurzer schneller Ruck hinterher.
    """
    out: list[dict] = []
    rest = dots
    if art == "leerzeilen":
        zeilen, rest = divmod(dots, LINE_DOTS)
        out += [{"t": "text", "v": " ", "align": 0, "h": LINE_DOTS} for _ in range(zeilen)]
    elif art == "bild":
        baender, rest = divmod(dots, LEERBILD_BAND)
        if baender:
            out.append(_leeres_bild(baender * LEERBILD_BAND, cfg))
    if art in ("leerzeilen", "bild") and rest > 0:
        out.append({"t": "text", "v": " ", "align": 0, "h": rest})
        rest = 0
    # Gedruckt, aber Vorschub: die Vorschau darf das nicht als Inhalt zaehlen,
    # sonst "passt nicht" jedes Etikett. Die Firmware uebergeht das Merkmal.
    for b in out:
        b["vorschub"] = True
    while rest > 0:
        stueck = min(schritt, rest) if schritt else rest
        out.append({"t": "feed", "dots": stueck})
        rest -= stueck
    return out


def ablauf_einfach(cfg: dict) -> dict:
    """Rueckzug -> Etikett -> Vorschub: wie viel Papier ein Etikett verbraucht.

    Je Etikett bewegt sich das Papier um  Hoehe + Vorschub danach - Rueckzug.
    Das muss *genau* der Abstand von einem Etikettanfang zum naechsten sein
    (Etikett + Luecke). Schon ein Millimeter daneben summiert sich: nach ein
    paar Etiketten druckt er in die Luecke, und der Rueckzug zieht die Kante
    des naechsten Etiketts unter die Walze - das rattert und klingt nach
    Durchdrehen. Genau das ergab "Vorschub danach 12 und 14 gehen nicht,
    13 geht" bei 25 mm Hoehe und 6 mm Rueckzug auf einer 32-mm-Rolle.

    Ist der Etikettenabstand (`label_pitch_mm`) eingetragen, ergibt sich der
    Vorschub danach daraus - dann darf man Hoehe und Rueckzug aendern, ohne
    dass die Etiketten zu wandern beginnen. Sonst gilt `label_gap_mm`.
    Alles in Punkten (8 je mm).
    """
    stack_mm, _ = feed_and_line_mm(cfg)
    hoehe = int(stack_mm * DOTS_PER_MM)
    rueckzug = max(0, min(MAX_BACKFEED_DOTS, int(cfg.get("backfeed_dots", 0) or 0)))
    abstand = int(round(float(cfg.get("label_pitch_mm", 0) or 0) * DOTS_PER_MM))
    if abstand > 0:
        vorschub = abstand - hoehe + rueckzug
    else:
        vorschub = int(round(float(cfg.get("label_gap_mm", 0) or 0) * DOTS_PER_MM))
    zu_lang = max(0, -vorschub)       # Hoehe passt nicht in den Abstand
    vorschub = max(0, vorschub)
    return {
        "hoehe": hoehe,
        "rueckzug": rueckzug,
        "vorschub": vorschub,
        "abstand": abstand,
        "papier": hoehe + vorschub - rueckzug,
        "zu_lang": zu_lang,
        "aus_abstand": abstand > 0,
    }


def render_label(item: dict, printer_cfg: dict) -> dict:
    """Etikett als geraeteunabhaengige Feldliste rendern.

    Die Firmware kennt keine Etiketten-Semantik mehr: sie bekommt eine Liste
    von Zeichenbefehlen und schiebt sie auf die UART. Layout-Aenderungen sind
    damit ein Server-Deploy statt eines OTA-Flashs.
    """
    # Vor jeder Breitenrechnung: aus "…" werden drei Zeichen, und die muessen
    # beim Umbrechen schon mitzaehlen. Siehe services/zeichen.py.
    item = latin1_tief(item)

    layout = printer_cfg.get("label_layout", DEFAULT_LAYOUT)
    if layout not in LAYOUTS:
        layout = DEFAULT_LAYOUT
    rotate = is_rotated(printer_cfg)
    stack_mm, line_mm = feed_and_line_mm(printer_cfg)

    chars = max(8, int(line_mm * DOTS_PER_MM) // 12)
    declared = int(printer_cfg.get("paper_chars", settings.label_paper_chars))
    chars = min(chars, declared)

    # Der zurueckgeholte Totbereich ist zusaetzlich bedruckbare Flaeche.
    backfeed = max(0, min(MAX_BACKFEED_DOTS, int(printer_cfg.get("backfeed_dots", 0))))
    pitch = int(stack_mm * DOTS_PER_MM)

    # Der Streifen am Etikettenanfang, den der Drucker nicht erreicht: zwischen
    # Druckkopf und Abrisskante liegt Papier, das nach dem Abreissen schon
    # durch ist. Er zaehlt nicht zur bedruckbaren Hoehe.
    #
    # Das fehlte, und deshalb rutschte das Etikett aufs naechste: die Rechnung
    # ging von der vollen Etikettenteilung aus, tatsaechlich stand der Anfang
    # aber schon hinter dem Kopf. Was hinten nicht mehr passte - der Code -
    # landete auf dem Folgeetikett. Ein Rueckzug holt den Bereich zurueck und
    # gibt ihn hier wieder frei.
    dead = max(0, int(float(printer_cfg.get("label_dead_zone_mm", 0)) * DOTS_PER_MM))
    verloren = max(0, dead - backfeed)

    budget = pitch + backfeed - dead - SAFETY_DOTS

    # Ablauf "einfach": Rueckzug, dann das Etikett in genau der eingestellten
    # Laenge, dann ein Leervorschub. Kein Rechnen mit dem Totbereich - wer
    # seinen Drucker kennt, stellt die Werte direkt ein. Wie viel Papier das
    # je Etikett ergibt, rechnet ablauf_einfach() an einer Stelle aus.
    einfach = printer_cfg.get("label_sequence") == "einfach"
    ablauf = ablauf_einfach(printer_cfg) if einfach else None
    luecke = ablauf["vorschub"] if ablauf else 0
    if einfach:
        dead = verloren = 0
        budget = pitch - SAFETY_DOTS
    if layout in BILD_LAYOUTS:
        # Der Druckkopf, nicht das Etikett, begrenzt die Hoehe des Bildes:
        # ein 58-mm-Drucker hat 384 Punkte (48 mm), auch unter 50 mm Etikett.
        kopf = min(int(line_mm * DOTS_PER_MM), int(printer_cfg.get(
            "paper_chars", settings.label_paper_chars)) * 12)
        blocks = hochformat.bloecke(item, printer_cfg, budget, kopf)
    else:
        alle = _build(layout, item, printer_cfg, chars)
        blocks = _fit(alle, budget)
        # Was _fit() aus Platzgruenden weggelassen hat - sonst fehlt eine
        # Zeile auf dem Etikett, ohne dass irgendwo steht, warum.
        weggelassen = [
            b.get("k") or b.get("v") or b.get("t")
            for b in alle if b.get("t") != "feed" and not any(b is x for x in blocks)
        ]

    # Passt es immer noch nicht, muss das jemand erfahren.
    #
    # `_fit` wirft von hinten weg, aber Code und geschuetzte Zeilen bleiben
    # stehen - auf einem sehr kleinen Etikett oder mit einem grossen QR kann
    # das Ergebnis trotzdem zu hoch sein. Bisher lief es dann stillschweigend
    # aufs Folgeetikett; ab hier steht die Zahl im Auftrag und im Log.
    overflow = max(0, total_dots(blocks) - budget)
    if overflow:
        log.warning(
            "Etikett %s passt nicht: %d Punkte zu viel (Zuschnitt %s, "
            "Teilung %d Punkte). Kleinerer Code oder kuerzerer Zuschnitt noetig.",
            item.get("label", "?"), overflow, layout, pitch,
        )
    if layout in BILD_LAYOUTS:
        weggelassen = []
    noetig: list[str] = []
    if backfeed:
        if printer_cfg.get("backfeed_cmd") == "esck":
            # ESC K kennt die Firmware nicht als eigenen Block; er geht als
            # rohe Bytes hinaus. Ein Merkmal am "back"-Block haette eine
            # neue Firmware gebraucht - und eine aeltere haette stattdessen
            # ESC j geschickt, das dieser Drucker ignoriert.
            blocks.insert(0, {"t": "raw", "h": -backfeed, "d": base64.b64encode(
                bytes([0x1B, 0x4B, backfeed])).decode("ascii")})
            noetig.append(RAW_FAEHIGKEIT)
        else:
            blocks.insert(0, {"t": "back", "dots": backfeed})

    # Rest bis zur Perforation vorschieben, damit das naechste Etikett oben
    # anfaengt - aber nur den Rest, nicht pauschal. total_dots() enthaelt den
    # Rueckzug negativ, der Vorschub gleicht ihn damit von selbst wieder aus.
    #
    # Bei gestanzten Etiketten geht es besser: hat der Drucker einen
    # Luecken- oder Markensensor, faehrt `GS FF` von selbst bis zum Anfang des
    # naechsten Etiketts. Dann haengt die Registrierung nicht mehr an unserer
    # Rechnung, sondern der Drucker findet die Perforation bei *jedem* Etikett
    # neu - ein Rest von ein paar Punkten kann sich nicht mehr aufsummieren.
    # Das ist der eigentlich richtige Weg fuer Rollen mit Trennluecke, aber
    # nicht jeder Drucker kann es; deshalb eine Einstellung und nicht die
    # Vorgabe.
    remaining = pitch - total_dots(blocks)
    if einfach:
        # Der Rueckzug zaehlt hier nicht zur Laenge: das Etikett beginnt
        # dort, wo der Rueckzug endet, und ist danach genau `pitch` lang.
        remaining = pitch - (total_dots(blocks) + backfeed)
    if remaining > 0 and einfach:
        blocks.append({"t": "feed", "dots": remaining})
    elif remaining > 0:
        # Der Formularvorschub ist ein *Merkmal* des Vorschubs, kein eigener
        # Blocktyp.
        #
        # Als eigener Typ war er eine Falle: eine Firmware, die ihn nicht
        # kennt, laesst den Block stillschweigend fallen - und schiebt dann
        # gar nicht vor. Die Etiketten liefen uebereinander, und schuld waere
        # scheinbar der Drucker gewesen. So herum versteht jede Firmware
        # wenigstens `dots` und macht das Richtige; wer `form` kennt, sucht
        # stattdessen die Luecke.
        vorschub = {"t": "feed", "dots": remaining}
        if printer_cfg.get("label_end") == "formfeed":
            vorschub["form"] = True
        blocks.append(vorschub)

    if einfach and luecke:
        vorschub = {"t": "feed", "dots": luecke}
        if printer_cfg.get("label_end") == "formfeed":
            vorschub["form"] = True
        blocks.append(vorschub)

    # Wie vorgeschoben wird. ESC J laeuft bei vielen Druckern mit voller
    # Motorgeschwindigkeit - auf Etikettenrollen dreht die Walze dabei durch,
    # beim Drucken (langsameres Drucktempo) dagegen nicht. Deshalb laesst sich
    # der Vorschub auch *drucken*: als Leerzeilen oder als leeres Bild. Und
    # ein ESC J laesst sich in kleine Schritte zerlegen. Die Summe bleibt in
    # jedem Fall gleich, die Teilung stimmt weiter.
    art = printer_cfg.get("feed_mode", "escj")
    schritt = max(0, int(printer_cfg.get("feed_step_dots", 0) or 0))
    if art in ("leerzeilen", "bild") or schritt:
        zerlegt: list[dict] = []
        for b in blocks:
            if b.get("t") == "feed" and not b.get("form") and int(b.get("dots", 0)) > 0:
                zerlegt += _vorschub_bloecke(int(b["dots"]), art, schritt, printer_cfg)
            else:
                zerlegt.append(b)
        blocks = zerlegt
        if art == "bild":
            noetig.append(BILD_FAEHIGKEIT)

    # Jedem Block seine Hoehe mitgeben.
    #
    # Das ist der Kern der Sache: die Firmware raet den Zeilenabstand nicht
    # mehr und der Drucker nimmt auch nicht mehr seinen eigenen, sondern
    # beide halten sich an genau die Zahl, mit der hier gerechnet wurde.
    # `total_dots()` beschreibt damit wirklich den Papiervorschub und nicht
    # nur eine Hoffnung.
    #
    # `feed` und `back` bekommen nichts: sie tragen ihre Laenge schon in
    # `dots` und laufen ueber `ESC J` / `ESC j`, die vom Zeilenabstand
    # unabhaengig sind.
    for block in blocks:
        # Beim Rasterblock ist `h` die Zahl der Bildzeilen und damit schon
        # die Hoehe - sie hier zu ueberschreiben (aufgerundet auf ganze
        # Baender) haette die Firmware mehr Daten erwarten lassen, als da sind.
        if block.get("t") not in ("feed", "form", "back", "raster", "raw"):
            block["h"] = block_dots(block)

    payload = {
        "chars": chars,
        "layout": layout,
        # ESC V gilt nur fuer Text; ein gedrehtes Bild ist schon gedreht.
        "rotate": rotate and layout not in BILD_LAYOUTS,
        "backfeed": backfeed,
        # Kantenlaengen so, wie das Etikett hinterher gelesen wird - die
        # Vorschau zeichnet danach und muss die Drehung nicht nachrechnen.
        "line_mm": line_mm,
        "stack_mm": stack_mm,
        # Bedruckbare Hoehe: die Etikettenteilung, abzueglich des Streifens,
        # den der Drucker nicht erreicht, zuzueglich dem, was der Rueckzug
        # davon zurueckholt.
        "height_dots": pitch - verloren,
        "dead_dots": dead,
        # Wie viele Punkte ueber die bedruckbare Hoehe hinausgehen. 0 heisst:
        # das Etikett endet genau an der Perforation.
        "overflow": overflow,
        "weggelassen": weggelassen,
        # Ablauf "einfach": Papier je Etikett und ob der Etikettenabstand
        # ueberhaupt reicht - fuer die Anzeige unter Etiketten -> Einrichten.
        "ablauf": ablauf,
        "blocks": blocks,
    }
    if layout in BILD_LAYOUTS:
        # Der Server prueft das vor dem Senden: ein Terminal ohne diese
        # Faehigkeit bekaeme sonst leere Etiketten statt einer Meldung.
        noetig.insert(0, BILD_FAEHIGKEIT)
    noetig = list(dict.fromkeys(noetig))   # jede Faehigkeit nur einmal
    if noetig:
        payload["braucht"] = noetig[0] if len(noetig) == 1 else noetig
    return payload


# ------------------------------------------------------------------ Vorschau
def render_preview_svg(payload: dict, cfg: dict) -> str:
    """Dieselbe Blockliste als massstabsgetreue SVG-Vorschau.

    Bewusst aus dem fertigen Payload gezeichnet und nicht aus dem Item: was
    die Vorschau zeigt, ist genau das, was der Drucker bekommt. Eine zweite
    Layout-Implementierung fuer die Anzeige waere die naechste Stelle, an der
    Bildschirm und Papier auseinanderlaufen.
    """
    # Die Kantenlaengen stehen im Payload, in Leserichtung. Die Vorschau zeigt
    # das Etikett damit so, wie es hinterher im Schrank klebt, ohne die
    # Drehung ein zweites Mal nachzurechnen.
    view_w = float(payload.get("line_mm") or cfg.get("label_width_mm", 50))
    backfeed = int(payload.get("backfeed", 0))

    dots_w = int(view_w * DOTS_PER_MM)
    # Mit Rueckzug ist das bedruckbare Feld hoeher als ohne - genau das ist ja
    # der Zweck. Die Vorschau waechst deshalb mit.
    dots_h = int(payload.get("height_dots") or 0) or int(
        float(payload.get("stack_mm") or cfg.get("label_height_mm", 30)) * DOTS_PER_MM
    )
    view_h = dots_h / DOTS_PER_MM
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dots_w} {dots_h}" '
        f'width="{view_w * 4}" height="{view_h * 4}" '
        'role="img" aria-label="Etikettenvorschau">',
        '<rect width="100%" height="100%" rx="12" fill="#fff" stroke="#c8d0d6"/>',
    ]
    if backfeed:
        # Die Linie zeigt, wo der Druck ohne Rueckzug angefangen haette -
        # alles darueber ist zurueckgeholter Totbereich.
        parts.append(
            f'<line x1="0" y1="{backfeed}" x2="{dots_w}" y2="{backfeed}" '
            'stroke="#43a047" stroke-width="2" stroke-dasharray="8 5"/>'
        )

    y = 0
    for block in payload.get("blocks", []):
        kind = block.get("t")
        dots = block_dots(block)
        # Rueckzug (ESC j als "back", ESC K als "raw") verschiebt nichts in
        # der Vorschau: die gestrichelte Linie zeigt ihn schon. Als Vorschub
        # gerechnet rutschte die erste Zeile ueber den oberen Rand.
        if kind in ("back", "form", "feed", "raw"):
            y += dots if kind in ("form", "feed") else 0
            continue
        if kind == "text":
            large = block.get("large")
            size = 34 if large else 19
            anchor = {0: "start", 1: "middle", 2: "end"}.get(block.get("align", 0), "start")
            x = {"start": 8, "middle": dots_w // 2, "end": dots_w - 8}[anchor]
            weight = "bold" if block.get("bold") else "normal"
            parts.append(
                f'<text x="{x}" y="{y + dots - 6}" text-anchor="{anchor}" '
                f'font-family="DejaVu Sans, sans-serif" font-size="{size}" '
                f'font-weight="{weight}">{_svg_escape(block.get("v", ""))}</text>'
            )
        elif kind == "row":
            decoration = "underline" if block.get("underline") else "none"
            parts.append(
                f'<text x="8" y="{y + dots - 6}" font-family="DejaVu Sans, sans-serif" '
                f'font-size="19">{_svg_escape(block.get("k", ""))}</text>'
                f'<text x="{dots_w - 8}" y="{y + dots - 6}" text-anchor="end" '
                f'font-family="DejaVu Sans, sans-serif" font-size="19" '
                f'text-decoration="{decoration}"'
                f'>{_svg_escape(block.get("v", ""))}</text>'
            )
        elif kind == "sep":
            parts.append(
                f'<line x1="8" y1="{y + dots // 2}" x2="{dots_w - 8}" y2="{y + dots // 2}" '
                'stroke="#888" stroke-dasharray="6 4"/>'
            )
        elif kind == "code128":
            height = int(block.get("height", 40))
            bar_w = 3
            total = min(dots_w - 16, 60 * bar_w)
            x = (dots_w - total) // 2
            # Nur eine Andeutung: die echten Striche rechnet der Drucker.
            for i in range(0, total, bar_w * 2):
                parts.append(f'<rect x="{x + i}" y="{y}" width="{bar_w}" height="{height}" fill="#111"/>')
        elif kind == "qr":
            side = dots
            x = (dots_w - side) // 2
            parts.append(
                f'<rect x="{x}" y="{y}" width="{side}" height="{side}" fill="#fff" stroke="#111"/>'
                f'<rect x="{x + 6}" y="{y + 6}" width="{side // 4}" height="{side // 4}" fill="#111"/>'
                f'<rect x="{x + side - side // 4 - 6}" y="{y + 6}" width="{side // 4}" '
                f'height="{side // 4}" fill="#111"/>'
                f'<rect x="{x + 6}" y="{y + side - side // 4 - 6}" width="{side // 4}" '
                f'height="{side // 4}" fill="#111"/>'
                f'<text x="{x + side // 2}" y="{y + side // 2 + 8}" text-anchor="middle" '
                'font-family="DejaVu Sans, sans-serif" font-size="16" fill="#666">QR</text>'
            )
        y += dots

    parts.append("</svg>")
    return "".join(parts)


def _svg_escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
