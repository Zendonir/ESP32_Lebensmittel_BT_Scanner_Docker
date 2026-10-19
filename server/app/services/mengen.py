"""Mengen: gespeichert in g, ml oder Stueck - angezeigt ab 1000 als kg/l.

Eingegeben wird immer in g oder ml, damit "Menge" in jeder Liste dieselbe
Groessenordnung hat und sich Packungen zusammenzaehlen lassen (500 g + 1 kg
war vorher nicht zu summieren). Erst beim Ausgeben wird aus 1500 g "1,5 kg".

Die Produktdatenbank liefert Mengen als freien Text ("1l", "0,5 L",
"6 x 330 ml", "500g e") - `aus_angabe()` macht daraus (Zahl, Einheit).
"""

from __future__ import annotations

import re

# Was hereinkommt -> (Faktor, gespeicherte Einheit)
_EINHEITEN = {
    "g": (1, "g"), "gr": (1, "g"), "gramm": (1, "g"),
    "kg": (1000, "g"), "kilo": (1000, "g"),
    "mg": (0.001, "g"),
    "ml": (1, "ml"), "cl": (10, "ml"), "dl": (100, "ml"),
    "l": (1000, "ml"), "liter": (1000, "ml"), "ltr": (1000, "ml"),
    "st": (1, "St."), "st.": (1, "St."), "stk": (1, "St."), "stk.": (1, "St."),
    "stueck": (1, "St."), "stück": (1, "St."),
}

EINHEITEN = ("", "St.", "g", "ml")


def normalisieren(menge: float | int | None, einheit: str | None) -> tuple[float, str]:
    """kg/l (und alte Schreibweisen) auf g/ml bzw. "St." bringen."""
    menge = float(menge or 0)
    schluessel = (einheit or "").strip().lower()
    if not schluessel:
        return menge, ""
    faktor, ziel = _EINHEITEN.get(schluessel, (1, (einheit or "").strip()))
    return round(menge * faktor, 3), ziel


def einheit(einheit_: str | None) -> str:
    """Nur die Einheit (Vorlagen haben keine Menge): kg -> g, l -> ml."""
    return normalisieren(1, einheit_)[1]


def _zahl(x: float) -> str:
    return f"{round(x, 3):g}".replace(".", ",")


def text(menge: float | int | None, einheit_: str | None) -> str:
    """Zum Anzeigen und Drucken: 1500 g -> "1,5 kg", 330 ml -> "330 ml"."""
    if menge in (None, ""):
        return ""
    menge, einheit_ = normalisieren(menge, einheit_)
    if einheit_ == "g" and menge >= 1000:
        return f"{_zahl(menge / 1000)} kg"
    if einheit_ == "ml" and menge >= 1000:
        return f"{_zahl(menge / 1000)} l"
    return f"{_zahl(menge)} {einheit_}".strip()


_ANGABE = re.compile(
    r"(?:(\d+)\s*[x×*]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|mg|g|gr|gramm|ml|cl|dl|l|liter|ltr)\b",
    re.IGNORECASE,
)


def aus_angabe(angabe: str | None) -> tuple[float, str] | None:
    """ "1l" -> (1000, "ml"), "6 x 330 ml" -> (330, "ml") - die Menge **einer**
    Packung, wie beim Einlagern gemeint. Nichts Lesbares -> None."""
    if not angabe:
        return None
    treffer = _ANGABE.search(angabe.replace(" ", " "))
    if not treffer:
        return None
    menge, einheit_ = normalisieren(float(treffer.group(2).replace(",", ".")), treffer.group(3))
    return (menge, einheit_) if menge > 0 else None
