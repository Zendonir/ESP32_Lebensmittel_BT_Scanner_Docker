"""Testknoepfe fuer den Drucker - einzelne Befehle direkt ausprobieren.

Der Messstreifen (calibration.py) stellt feste Fragen. Bei einem namenlosen
Drucker reicht das nicht immer: ob er rueckwaerts fahren kann, haengt davon
ab, *welchen* Befehl er dafuer versteht, und dafuer gibt es mehrere, je nach
Hersteller. Hier steht je Variante ein kleiner Test, ein Etikett lang, den
die Oberflaeche per Knopf ausloest.

Die Bytes gehen als Block "raw" an die Firmware. Die setzt nichts um, sie
reicht sie nur durch - welche Befehle es sind, steht ausschliesslich hier.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Callable

from . import calibration as K

DOTS_PER_MM = 8
LINE = 24
FAEHIGKEIT = "raw"

ESC, GS = 0x1B, 0x1D


def raw(daten: bytes, vorschub: int = 0) -> dict:
    """Rohe Bytes. `h` ist nur fuer die Rechnung: so weit schieben sie vor."""
    return {"t": "raw", "d": base64.b64encode(daten).decode("ascii"), "h": vorschub}


def _text(v: str, bold: bool = False) -> dict:
    return {"t": "text", "v": v, "align": 0, "bold": bold, "h": LINE}


def _mm(dots: float) -> str:
    return f"{dots / DOTS_PER_MM:.1f}".replace(".", ",")


def _hoehe(b: dict) -> int:
    if b["t"] == "raw":
        return int(b.get("h", 0))
    return K._hoehe(b)


# ------------------------------------------------------------------ Rueckzug
VOR = 64          # 8 mm vor ...
ZURUECK = 40      # ... 5 mm zurueck
BALKEN = 8


def _rueckzug(befehl: bytes) -> Callable[[int], list[dict]]:
    def bauen(breite: int) -> list[dict]:
        return [
            K._raster(breite, BALKEN),
            {"t": "feed", "dots": VOR},
            raw(befehl, -ZURUECK),
            K._raster(breite // 2, BALKEN),
            {"t": "feed", "dots": ZURUECK},
            _text(f"Oberkanten {_mm(VOR + BALKEN - ZURUECK)} mm = geht"),
            _text(f"{_mm(VOR + BALKEN)} mm = geht nicht"),
        ]
    return bauen


def _vorschub(befehl: bytes, dots: int) -> Callable[[int], list[dict]]:
    def bauen(breite: int) -> list[dict]:
        return [
            K._raster(breite, BALKEN),
            raw(befehl, dots),
            K._raster(breite, BALKEN),
            _text(f"Oberkanten Soll {_mm(dots + BALKEN)} mm"),
        ]
    return bauen


def _ende(befehl: bytes) -> Callable[[int], list[dict]]:
    def bauen(breite: int) -> list[dict]:
        return [
            _text("Danach kommt der Befehl."),
            raw(befehl),
            _text("<<< oben am Etikett?", bold=True),
        ]
    return bauen


def _quadrat(modus: str) -> Callable[[int], list[dict]]:
    def bauen(breite: int) -> list[dict]:
        return [_text(f"Soll {_mm(K.BOX_DOTS)} x {_mm(K.BOX_DOTS)} mm"),
                K._raster(K.BOX_DOTS, K.BOX_DOTS, modus=modus)]
    return bauen


def _umlaute(breite: int) -> list[dict]:
    return [_text("ÄÖÜ äöü ß"), _text("Grüße aus der Küche", bold=True),
            _text("0123456789 ½ ° µ")]


@dataclass(frozen=True)
class Test:
    id: str
    gruppe: str
    titel: str
    befehl: str
    hinweis: str
    bauen: Callable[[int], list[dict]]
    # Tests, nach denen der Drucker nicht weiss, wo er steht (Lueckensuche):
    # nicht auf eine Teilung auffuellen - das stimmte ohnehin nicht.
    auffuellen: bool = True


TESTS: tuple[Test, ...] = (
    Test("rueck_escj", "Rückzug", "ESC j", "1B 6A 28",
         "Der übliche Befehl (Epson): 40 Punkte zurück.", _rueckzug(bytes([ESC, 0x6A, 40]))),
    Test("rueck_escj_stueck", "Rückzug", "ESC j in Stücken", "5 × 1B 6A 08",
         "Manche Drucker fahren je Befehl nur ein kleines Stück zurück.",
         _rueckzug(bytes([ESC, 0x6A, 8]) * 5)),
    Test("rueck_escj_leer", "Rückzug", "ESC J 0 + ESC j", "1B 4A 00 1B 6A 28",
         "Erst den Druckpuffer leeren, dann zurück - manche Drucker fahren "
         "nur zurück, wenn nichts mehr ungedruckt im Puffer steht.",
         _rueckzug(bytes([ESC, 0x4A, 0, ESC, 0x6A, 40]))),
    Test("rueck_esck", "Rückzug", "ESC K", "1B 4B 28",
         "Rückwärts in Punkten bei vielen chinesischen Druckern.",
         _rueckzug(bytes([ESC, 0x4B, 40]))),
    Test("rueck_esce", "Rückzug", "ESC e", "1B 33 14 1B 65 02",
         "Rückwärts in Zeilen: Zeilenabstand 20 Punkte, 2 Zeilen zurück.",
         _rueckzug(bytes([ESC, 0x33, 20, ESC, 0x65, 2]))),
    Test("vor_escJ", "Vorschub", "ESC J", "1B 4A 50",
         "80 Punkte vor - so schiebt der Server zwischen Etiketten.",
         _vorschub(bytes([ESC, 0x4A, 80]), 80)),
    Test("vor_escd", "Vorschub", "ESC d", "1B 33 18 1B 64 03",
         "3 Zeilen à 24 Punkte.", _vorschub(bytes([ESC, 0x33, 24, ESC, 0x64, 3]), 72)),
    Test("vor_lf", "Vorschub", "LF", "1B 33 18 0A 0A 0A",
         "3 Zeilenumbrüche à 24 Punkte.", _vorschub(bytes([ESC, 0x33, 24, 10, 10, 10]), 72)),
    # Gedruckter Vorschub: laeuft im Drucktempo statt mit voller
    # Motorgeschwindigkeit - gegen eine Walze, die beim ESC J durchdreht.
    Test("vor_leerzeilen", "Vorschub", "Leerzeilen", "1B 33 18 + 4 × \" \" 0A",
         "4 gedruckte Leerzeilen à 24 Punkte – hörbar langsamer als ESC J?",
         _vorschub(bytes([ESC, 0x33, 24]) + b" \n" * 4, 96)),
    Test("vor_bild33", "Vorschub", "Leeres Bild ESC * 33", "4 × 1B 2A 21 08 00 …",
         "Leeres Bild in 4 Bändern à 24 Punkte (Bildmodus B).",
         _vorschub(bytes([ESC, 0x33, 24]) + (bytes([ESC, 0x2A, 33, 8, 0]) + bytes(24) + b"\n") * 4,
                   96)),
    Test("vor_bildgsv0", "Vorschub", "Leeres Bild GS v 0", "1D 76 30 00 01 00 60 00 …",
         "Leeres Bild mit 96 Zeilen (Bildmodus A).",
         _vorschub(bytes([GS, 0x76, 0x30, 0, 1, 0, 96, 0]) + bytes(96), 96)),
    Test("ende_gsff", "Etikettenende", "GS FF", "1D 0C",
         "Zum Anfang des nächsten Etiketts (Lückensensor).", _ende(bytes([GS, 0x0C])),
         auffuellen=False),
    Test("ende_ff", "Etikettenende", "FF", "0C",
         "Seitenvorschub - manche Etikettendrucker suchen damit die Lücke.",
         _ende(bytes([0x0C])), auffuellen=False),
    Test("ende_gsv", "Etikettenende", "GS V 66", "1D 56 42 00",
         "Bis zur Abrissposition vorschieben.", _ende(bytes([GS, 0x56, 66, 0])),
         auffuellen=False),
    *(Test(f"bild_{m}", "Bildmodus", f"{b}: {n}", n, "Quadrat 9 × 9 mm.", _quadrat(m))
      for b, n, m in K.BOX_MODI),
    Test("text_umlaute", "Text", "Umlaute", "ESC t 16",
         "Codepage 1252: alle Zeichen müssen richtig dastehen.", _umlaute),
)

BY_ID = {t.id: t for t in TESTS}


def katalog() -> list[dict]:
    return [{"id": t.id, "gruppe": t.gruppe, "titel": t.titel, "befehl": t.befehl,
             "hinweis": t.hinweis} for t in TESTS]


def build(test_id: str, stack_mm: float, line_mm: float, chars: int = 32) -> dict:
    """Einen Test als Druckauftrag: Ueberschrift, Muster, auf eine Teilung."""
    test = BY_ID[test_id]
    pitch = int(stack_mm * DOTS_PER_MM)
    inhalt = [_text(f"{test.gruppe}: {test.titel}", bold=True), _text(test.befehl),
              *test.bauen(K.breite_dots(line_mm))]
    blocks = list(inhalt)
    if test.auffuellen:
        blocks.append({"t": "feed", "dots": max(0, pitch - sum(_hoehe(b) for b in inhalt)),
                       "teilung": True})
    else:
        blocks.append({"t": "feed", "dots": pitch // 2})
    return {"chars": chars, "rotate": False, "blocks": blocks, "braucht": FAEHIGKEIT}
