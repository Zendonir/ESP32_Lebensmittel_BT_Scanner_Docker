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


def render_label(item: dict, printer_cfg: dict) -> dict:
    """Etikett als geraeteunabhaengige Feldliste rendern.

    Die Firmware kennt keine Etiketten-Semantik mehr: sie bekommt eine Liste
    von Zeichenbefehlen und schiebt sie auf die UART. Layout-Aenderungen sind
    damit ein Server-Deploy statt eines OTA-Flashs.
    """
    chars = int(printer_cfg.get("paper_chars", settings.label_paper_chars))
    household = printer_cfg.get("household") or settings.household
    blocks: list[dict] = []

    if household:
        blocks.append({"t": "text", "v": household, "align": 1, "bold": False})

    # Unterkategorie gehoert an den Namen, nicht in eine eigene Zeile: auf dem
    # Etikett soll "Filet - Schwein" stehen, damit im Schrank erkennbar ist,
    # was fuer ein Filet das ist.
    title = categories.display_name(item.get("name", ""), item.get("subcategory", ""))
    for line in _wrap(title, chars // 2):
        blocks.append({"t": "text", "v": line, "align": 1, "bold": True, "large": True})

    brand = item.get("brand", "")
    if brand:
        blocks.append({"t": "text", "v": brand, "align": 1})

    blocks.append({"t": "sep"})

    expiry = to_display(item.get("expiry_date", ""))
    if expiry:
        blocks.append({"t": "row", "k": "MHD", "v": expiry, "underline": True})

    qty, unit = item.get("quantity", 1), item.get("unit", "")
    if unit:
        qty_text = f"{qty:g} {unit}"
    else:
        qty_text = f"{qty:g}"
    blocks.append({"t": "row", "k": "Menge", "v": qty_text})

    location = item.get("location", "")
    if location:
        blocks.append({"t": "row", "k": "Ort", "v": location})

    blocks.append({"t": "row", "k": "Eingang", "v": to_display(item.get("added_date", ""))})
    blocks.append({"t": "sep"})

    label = item.get("label", "")
    if printer_cfg.get("code128", True):
        blocks.append({"t": "code128", "v": label})
    if printer_cfg.get("qr", True):
        blocks.append({"t": "qr", "v": label})
    blocks.append({"t": "text", "v": label, "align": 1})
    blocks.append({"t": "feed", "dots": int(printer_cfg.get("post_feed_dots", 86))})

    return {"chars": chars, "blocks": blocks}
