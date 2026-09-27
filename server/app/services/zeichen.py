"""Text so aufbereiten, dass Drucker und Terminal ihn darstellen koennen.

Beide kennen nur Latin-1 (0x20-0xFF): die Schrift des Terminals ist genau fuer
diesen Bereich erzeugt (firmware/scripts/gen_gfx_font.py), und der Drucker
bekommt seinen Text ueber Printer::toCp1252(), das nur Zeichen bis 0xFF
durchreicht. Alles darueber wird zu "?".

Das trifft ausgerechnet die Zeichen, die ein Telefon beim Tippen von selbst
einsetzt: das iPhone macht aus "Omas Suppe" in Anfuehrungszeichen „Omas
Suppe“ und aus einem Bindestrich zwischen Leerzeichen einen Gedankenstrich.
Auf dem Etikett stand dann ?Omas Suppe?.

Die gespeicherten Daten bleiben, wie sie eingegeben wurden - im Browser sind
die Zeichen richtig. Umgesetzt wird erst auf dem Weg zur Hardware, und nur
das, wofuer es eine naheliegende Entsprechung gibt.
"""

from __future__ import annotations

from typing import Any

_ERSATZ = str.maketrans({
    "„": '"', "“": '"', "”": '"', "‟": '"', "″": '"',
    "«": "«", "»": "»",           # Latin-1, bleiben
    "‚": "'", "‘": "'", "’": "'", "‛": "'", "′": "'",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    "…": "...",
    "€": "EUR",
    "•": "*",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‍": "", "﻿": "",
})


def latin1(text: str) -> str:
    """Typografische Zeichen durch darstellbare ersetzen."""
    if not text or text.isascii():
        return text
    return text.translate(_ERSATZ)


def latin1_tief(wert: Any) -> Any:
    """Wie `latin1`, fuer verschachtelte Nachrichten an das Terminal."""
    if isinstance(wert, str):
        return latin1(wert)
    if isinstance(wert, list):
        return [latin1_tief(v) for v in wert]
    if isinstance(wert, dict):
        return {k: latin1_tief(v) for k, v in wert.items()}
    return wert
