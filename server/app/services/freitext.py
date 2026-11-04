"""Freitext-Etiketten - wie ein Etikettendrucker, ohne Bestand dahinter.

Eingabe ist ein mehrzeiliger Text mit wenig Auszeichnung:

* ``**fett**`` - fett, auch mitten in der Zeile
* ``# Ueberschrift`` - die Zeile groesser (anderthalbfach)
* eine leere Zeile - halbe Zeile Abstand

Die Schrift waechst, bis der Text das Etikett fuellt (wie bei einem
Etikettendrucker mit "automatischer Groesse"); umgebrochen wird nur, wo der
Text selbst umbricht. Passt er auch in der kleinsten Groesse nicht, meldet
`zeichnen()` das in `ueberlauf`.

Gezeichnet wird in Leserichtung; drehen, einfarbig machen und in Rasterdaten
packen macht `brother` wie bei jedem anderen Etikett.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from PIL import Image, ImageDraw

from .hochformat import _schrift

STILE = {
    "einfach": "Einfach – alles gleich groß",
    "titel": "Titel – erste Zeile groß und fett, Linie darunter",
    "balken": "Balken – erste Zeile weiß auf schwarzem Balken",
    "rahmen": "Rahmen – mit Rand rundherum",
    "gross": "Groß – alles fett, so groß wie möglich",
    "datum": "Mit Datum – heutiges Datum klein unten rechts",
}
AUSRICHTUNGEN = ("links", "mitte", "rechts")
MIN_PT, MAX_PT = 9, 220
MAX_ZEICHEN = 600
RAND = 10


@dataclass
class _Zeile:
    teile: list[tuple[str, bool]]     # (Text, fett)
    faktor: float = 1.0               # Groesse relativ zur Grundgroesse
    leer: bool = False
    balken: bool = False              # weiss auf schwarz
    ausrichtung: str = ""             # leer = wie eingestellt


def _teile(zeile: str, alles_fett: bool) -> list[tuple[str, bool]]:
    """ "a **b** c" -> [("a ", False), ("b", True), (" c", False)]."""
    teile, fett = [], False
    for i, stueck in enumerate(re.split(r"\*\*", zeile)):
        if i:
            fett = not fett
        if stueck:
            teile.append((stueck, fett or alles_fett))
    return teile


def zeilen_aus(daten: dict) -> list[_Zeile]:
    text = str(daten.get("text") or "")[:MAX_ZEICHEN].replace("\r\n", "\n")
    stil = daten.get("stil") if daten.get("stil") in STILE else "einfach"
    zeilen: list[_Zeile] = []
    for roh in text.split("\n"):
        roh = roh.rstrip()
        if not roh.strip():
            zeilen.append(_Zeile([], leer=True))
            continue
        faktor = 1.0
        if roh.startswith("# "):
            roh, faktor = roh[2:], 1.5
        zeilen.append(_Zeile(_teile(roh, stil == "gross"), faktor))
    # Leere Zeilen am Anfang und Ende tragen nichts bei.
    while zeilen and zeilen[0].leer:
        zeilen.pop(0)
    while zeilen and zeilen[-1].leer:
        zeilen.pop()
    if not zeilen:
        return []
    erste = zeilen[0]
    if stil == "titel":
        erste.faktor = max(erste.faktor, 1.6)
        erste.teile = [(t, True) for t, _ in erste.teile]
    elif stil == "balken":
        erste.balken = True
        erste.teile = [(t, True) for t, _ in erste.teile]
    if stil == "datum":
        zeilen.append(_Zeile([(date.today().strftime("%d.%m.%Y"), False)], faktor=0.55,
                             ausrichtung="rechts"))
    return zeilen


def _breite(zeile: _Zeile, groesse: int) -> float:
    return sum(_schrift(max(1, round(groesse * zeile.faktor)), fett).getlength(t)
               for t, fett in zeile.teile)


def _hoehe(zeile: _Zeile, groesse: int) -> int:
    pt = max(1, round(groesse * zeile.faktor))
    if zeile.leer:
        return pt // 2
    hoehe = round(pt * 1.18)
    return hoehe + (round(pt * 0.3) if zeile.balken else 0)


def _passt(zeilen: list[_Zeile], groesse: int, w: int, h: int, extra: int) -> bool:
    gesamt = sum(_hoehe(z, groesse) for z in zeilen) + extra
    breit = max((_breite(z, groesse) + (round(groesse * 0.6) if z.balken else 0)
                 for z in zeilen if not z.leer), default=0)
    return gesamt <= h and breit <= w


def zeichnen(daten: dict, w: int, h: int) -> tuple[Image.Image, dict]:
    """Freitext-Etikett (w x h Punkte, Leserichtung) -> (Bild "L", Angaben)."""
    stil = daten.get("stil") if daten.get("stil") in STILE else "einfach"
    ausrichtung = daten.get("ausrichtung") if daten.get("ausrichtung") in AUSRICHTUNGEN else "mitte"
    bild = Image.new("L", (w, h), 255)
    stift = ImageDraw.Draw(bild)
    rand = RAND + (8 if stil == "rahmen" else 0)
    if stil == "rahmen":
        stift.rounded_rectangle([3, 3, w - 4, h - 4], radius=12, outline=0, width=4)
    zeilen = zeilen_aus(daten)
    if not zeilen:
        return bild, {"groesse": 0, "ueberlauf": False, "leer": True}
    innen_w, innen_h = w - 2 * rand, h - 2 * rand
    # Linie unter dem Titel braucht Platz.
    extra = 8 if stil == "titel" and len(zeilen) > 1 else 0

    unten, oben = MIN_PT, MAX_PT
    while unten < oben:                       # groesste passende Grundgroesse
        mitte = (unten + oben + 1) // 2
        if _passt(zeilen, mitte, innen_w, innen_h, extra):
            unten = mitte
        else:
            oben = mitte - 1
    groesse = unten
    ueberlauf = not _passt(zeilen, groesse, innen_w, innen_h, extra)

    gesamt = sum(_hoehe(z, groesse) for z in zeilen) + extra
    y = rand + max(0, (innen_h - gesamt) // 2)
    for nr, zeile in enumerate(zeilen):
        zh = _hoehe(zeile, groesse)
        if zeile.leer:
            y += zh
            continue
        pt = max(1, round(groesse * zeile.faktor))
        breite = _breite(zeile, groesse)
        wo = zeile.ausrichtung or ausrichtung
        if zeile.balken:
            stift.rectangle([0 if stil != "rahmen" else 3, y, w - 1 if stil != "rahmen" else w - 4,
                             y + zh - 1], fill=0)
        x = {"links": rand + (round(groesse * 0.3) if zeile.balken else 0),
             "rechts": w - rand - breite - (round(groesse * 0.3) if zeile.balken else 0),
             }.get(wo, (w - breite) / 2)
        # Grundlinie fuer alle Teile gleich - fett und normal stehen auf einer Linie.
        oberkante = y + (round(pt * 0.15) if zeile.balken else 0)
        for text, fett in zeile.teile:
            schrift = _schrift(pt, fett)
            stift.text((x, oberkante + schrift.getmetrics()[0]), text, font=schrift,
                       fill=255 if zeile.balken else 0, anchor="ls")
            x += schrift.getlength(text)
        y += zh
        if stil == "titel" and nr == 0 and len(zeilen) > 1:
            stift.line([rand, y + 3, w - rand, y + 3], fill=0, width=3)
            y += extra
    return bild, {"groesse": groesse, "ueberlauf": ueberlauf, "leer": False}
