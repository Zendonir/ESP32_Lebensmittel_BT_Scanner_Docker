"""Persistente Laufzeit-Einstellungen (Key/Value in der Datenbank)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting

DEFAULTS: dict[str, Any] = {
    "label_counter": 0,
    "roll_size": 0,
    "roll_used": 0,
    "ui": {
        "theme": "auto",
        "expiring_days": 7,
        "sort": "expiry",
    },
    "device_ui": {
        "brightness": 80,
        "beep": True,
        "idle_seconds": 60,
    },
    "printer": {
        "enabled": True,
        "baud": 9600,
        "paper_chars": 32,
        "post_feed_dots": 86,
        "qr": True,
        "code128": True,
        "household": "",
        # Etikettenmasse in Millimetern. Der Drucker rechnet in Punkten
        # (203 dpi = 8 je mm); daraus ergibt sich, wie viel ueberhaupt
        # draufpasst - siehe services/labels.py.
        "label_width_mm": 50,
        "label_height_mm": 30,
        "label_layout": "standard",
        # Welche Etikettenkante in Papierrichtung laeuft. Sie begrenzt, wie
        # viel Inhalt auf ein Etikett passt. Gibt die Rolle vor, nicht der
        # Geschmack - deshalb getrennt vom Drehen des Textes.
        "label_feed_edge": "hoehe",    # hoehe | breite
        "label_rotate": True,          # Text um 90 Grad drehen
        "label_orientation": "quer",   # Altbestand, wird nur noch gelesen
        # Welcher Code aufs Etikett kommt und wie gross. Auf einem 30-mm-
        # Etikett ist das die groesste Stellschraube: ein QR ist quadratisch
        # und kostet so viel Hoehe wie Breite, ein Strichcode ist flach.
        "label_code": "auto",          # auto | qr | code128
        "label_code_size": "mittel",   # klein | mittel | gross
        # Streifen am Etikettenanfang, den der Druckkopf nicht erreicht.
        # Bestimmt, wie viel vom Etikett wirklich bedruckbar ist - ohne diese
        # Angabe rechnet der Server mit der vollen Hoehe und der letzte Block
        # rutscht aufs naechste Etikett.
        "label_dead_zone_mm": 0,
        # Rueckzug vor dem Druck, in Punkten. Holt den Totbereich zwischen
        # Druckkopf und Abrisskante zurueck. 0 = aus, weil nicht jeder
        # ESC/POS-Drucker rueckwaerts fahren kann.
        "backfeed_dots": 0,
    },
}


async def get(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key, default)
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return row.value


async def put(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    encoded = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(Setting(key=key, value=encoded))
    else:
        row.value = encoded
    await session.flush()


async def merge(session: AsyncSession, key: str, patch: dict) -> dict:
    """Teilweise Aktualisierung eines Dict-Settings."""
    current = await get(session, key, {})
    if not isinstance(current, dict):
        current = {}
    merged = {**DEFAULTS.get(key, {}), **current, **patch}
    await put(session, key, merged)
    return merged


async def all_settings(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars().all()
    out = dict(DEFAULTS)
    for row in rows:
        try:
            out[row.key] = json.loads(row.value)
        except (ValueError, TypeError):
            out[row.key] = row.value
    return out
