"""Vergabe der Etikettennummern (LebNummer) und Etiketten-Rendering.

Der Zaehler liegt in der Datenbank und wird unter einem asyncio-Lock
hochgezaehlt. Im ersten Projekt lag er im NVS des ESP32 - nach einem
Factory-Reset begann die Nummerierung wieder bei 1 und kollidierte mit
Etiketten, die physisch noch im Schrank klebten.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import settings_store
from . import categories
from .dates import to_display

_lock = asyncio.Lock()


async def next_labels(session: AsyncSession, count: int = 1) -> list[str]:
    """`count` fortlaufende, garantiert eindeutige Etikettennummern."""
    async with _lock:
        start = int(await settings_store.get(session, "label_counter", 0))
        labels = [f"{settings.label_prefix}{start + i + 1:06d}" for i in range(count)]
        await settings_store.put(session, "label_counter", start + count)

        used = int(await settings_store.get(session, "roll_used", 0))
        await settings_store.put(session, "roll_used", used + count)
        await session.commit()
    return labels


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

LINE_DOTS = 24        # Schrift A, 12x24 Punkte
LINE_DOTS_LARGE = 48  # doppelte Hoehe
SEP_DOTS = 24
CODE128_FEED = 10     # Vorschub, den der Drucker nach dem Strichcode einfuegt

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


def label_dots(cfg: dict) -> int:
    """Nutzbare Hoehe eines Etiketts in Punkten."""
    height_mm = float(cfg.get("label_height_mm", 30))
    return max(0, int(height_mm * DOTS_PER_MM) - SAFETY_DOTS)


def block_dots(block: dict) -> int:
    """Hoehe eines Blocks in Punkten - dieselbe Rechnung wie in der Firmware."""
    kind = block.get("t")
    if kind == "text":
        return LINE_DOTS_LARGE if block.get("large") else LINE_DOTS
    if kind == "row":
        return LINE_DOTS
    if kind == "sep":
        return SEP_DOTS
    if kind == "code128":
        return int(block.get("height", 40)) + CODE128_FEED
    if kind == "qr":
        # Version 1 fasst 9 Zeichen alphanumerisch - fuer "LEB000123" reicht
        # das. 21 Module plus zwei Module Rand je Seite.
        return (21 + 4) * int(block.get("scale", 3))
    if kind == "feed":
        return int(block.get("dots", 0))
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
    keep = [b for b in blocks if b.get("t") in ("qr", "code128")]
    out = list(blocks)
    while total_dots(out) > budget:
        droppable = [i for i, b in enumerate(out) if b not in keep and b.get("t") != "feed"]
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
    "kompakt": "Name und MHD gross. Aus zwei Metern lesbar.",
    "standard": "Name gross, dazu MHD, Menge und Ort. Der Allrounder.",
    "vollstaendig": "Alle Angaben in normaler Schrift, kleiner Code.",
    "sparsam": "Nur Name und MHD, dafuer ein grosser Code.",
}

# Anzeigename fuer die Oberflaeche - der Schluessel bleibt umlautfrei, damit
# er unveraendert in der Datenbank stehen kann.
TITLES: dict[str, str] = {
    "kompakt": "Kompakt",
    "standard": "Standard",
    "vollstaendig": "Vollständig",
    "sparsam": "Sparsam",
}
DEFAULT_LAYOUT = "standard"


def is_rotated(cfg: dict) -> bool:
    """Muss der Text um 90 Grad gedreht werden?

    Der Druckkopf schreibt seine Zeilen immer ueber die kurze Kante. Wer das
    Etikett quer lesen will - Text der langen 50-mm-Kante entlang -, braucht
    dafuer die Drehung. Hochkant ist der ungedrehte Fall.
    """
    return cfg.get("label_orientation", "quer") != "hoch"


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
    qty, unit = item.get("quantity", 1), item.get("unit", "")
    return f"{qty:g} {unit}".strip()


def _code_blocks(item: dict, cfg: dict, prefer_qr: bool, height: int, scale: int) -> list[dict]:
    """Der Code zum Auslagern - QR oder Strichcode, nie beides.

    Beides nebeneinander geht auf einem ESC/POS-Drucker nicht (er kennt nur
    Zeilen), und beides untereinander frisst die halbe Etikettenhoehe fuer
    dieselbe Information.
    """
    label = item.get("label", "")
    if not label:
        return []

    # ESC V dreht nur Zeichen. Strichcode (GS k) und Bitmap laufen weiter in
    # Papierrichtung - bei gedrehtem Text steht der Strichcode also quer zur
    # Schrift. Genau das ist auf dem ersten Querformat-Ausdruck passiert.
    #
    # Im gedrehten Fall ist der QR-Code deshalb keine Vorliebe, sondern
    # Bedingung: er ist quadratisch und aus jeder Richtung lesbar. Der Schalter
    # "QR" in den Einstellungen wird hier bewusst uebergangen - sonst waehlt
    # man Querformat und bekommt stillschweigend ein unbrauchbares Etikett.
    if is_rotated(cfg):
        return [{"t": "qr", "v": label, "scale": scale}]

    if prefer_qr and cfg.get("qr", True):
        return [{"t": "qr", "v": label, "scale": scale}]
    if cfg.get("code128", True):
        return [{"t": "code128", "v": label, "height": height}]
    return [{"t": "qr", "v": label, "scale": scale}]


def _build(layout: str, item: dict, cfg: dict, chars: int) -> list[dict]:
    title = _title(item)
    expiry = to_display(item.get("expiry_date", ""))
    label = item.get("label", "")
    location = item.get("location", "")
    brand = item.get("brand", "")

    if layout == "kompakt":
        blocks: list[dict] = _title_blocks(title, chars, large=True)
        if expiry:
            blocks.append({"t": "text", "v": expiry, "align": 1, "bold": True, "large": True})
        blocks += _code_blocks(item, cfg, prefer_qr=False, height=40, scale=3)
        blocks.append({"t": "text", "v": label, "align": 1})
        return blocks

    if layout == "sparsam":
        blocks = _title_blocks(title, chars, large=False)
        if expiry:
            blocks.append({"t": "row", "k": "MHD", "v": expiry, "underline": True})
        blocks += _code_blocks(item, cfg, prefer_qr=True, height=40, scale=5)
        return blocks

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
        blocks += _code_blocks(item, cfg, prefer_qr=False, height=32, scale=2)
        blocks.append({"t": "text", "v": label, "align": 1})
        return blocks

    # standard
    blocks = _title_blocks(title, chars, large=True)
    if expiry:
        blocks.append({"t": "row", "k": "MHD", "v": expiry, "underline": True})
    info = " · ".join(x for x in (_quantity(item), location) if x)
    if info:
        blocks.append({"t": "text", "v": info, "align": 1})
    blocks += _code_blocks(item, cfg, prefer_qr=True, height=40, scale=4)
    blocks.append({"t": "text", "v": label, "align": 1})
    return blocks


def render_label(item: dict, printer_cfg: dict) -> dict:
    """Etikett als geraeteunabhaengige Feldliste rendern.

    Die Firmware kennt keine Etiketten-Semantik mehr: sie bekommt eine Liste
    von Zeichenbefehlen und schiebt sie auf die UART. Layout-Aenderungen sind
    damit ein Server-Deploy statt eines OTA-Flashs.
    """
    layout = printer_cfg.get("label_layout", DEFAULT_LAYOUT)
    if layout not in LAYOUTS:
        layout = DEFAULT_LAYOUT
    rotate = is_rotated(printer_cfg)

    # Der Druckkopf schreibt immer ueber die kurze Kante des Etiketts. Soll der
    # Text der langen Kante entlang laufen - also quer gelesen werden - muss er
    # um 90 Grad gedreht werden. Hochkant kommt er dagegen ungedreht heraus.
    width_mm = float(printer_cfg.get("label_width_mm", 50))
    height_mm = float(printer_cfg.get("label_height_mm", 30))
    line_mm, stack_mm = (width_mm, height_mm) if rotate else (height_mm, width_mm)

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
    blocks = _fit(_build(layout, item, printer_cfg, chars), budget)
    if backfeed:
        blocks.insert(0, {"t": "back", "dots": backfeed})

    # Rest bis zur Perforation vorschieben, damit das naechste Etikett oben
    # anfaengt - aber nur den Rest, nicht pauschal. total_dots() enthaelt den
    # Rueckzug negativ, der Vorschub gleicht ihn damit von selbst wieder aus.
    remaining = pitch - total_dots(blocks)
    if remaining > 0:
        blocks.append({"t": "feed", "dots": remaining})

    return {
        "chars": chars,
        "layout": layout,
        "rotate": rotate,
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
        "blocks": blocks,
    }


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
        if kind == "back":
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
