"""MHD vom Foto lesen - lokal im Container, ohne Dienst im Netz.

Zwei Schritte, bewusst getrennt:

1. Texterkennung (RapidOCR: die PaddleOCR-Modelle ueber ONNX), rund eine
   halbe Sekunde je Durchgang auf dem Prozessor. Sie liefert alles, was auf
   der Verpackung steht: Naehrwerte, Chargen, Uhrzeiten - und irgendwo
   dazwischen das Datum.
2. `mhd_finden()` - entscheidet, welcher Text das MHD ist. Reine Regeln, ohne
   Modell, und deshalb ohne Kamera testbar (tests/test_mhd_ocr.py, mit den
   Aufdrucken echter Verpackungen).

Die Erkennung laeuft gestuft und hoert beim ersten Datum auf (`_DURCHGAENGE`).
Gefunden an echten Fotos (System -> "MHD-Erkennung testen"):

* Nadeldruck aus einzelnen Tintenpunkten (Joghurtdeckel, Flaschen) sieht das
  Modell oft gar nicht als Text. Die dunklen Punkte um ein Pixel ausweiten
  (`erode`) macht Striche daraus - dann wird er gelesen.
* Schraeger Aufdruck: Das Datum ist oft anders geneigt als der uebrige Text
  (separat aufgestempelt). Gedreht wird deshalb zuerst in die Richtung der
  Textstuecke, die nach einem Datum aussehen, dann in die Hauptrichtung.
* Fuer Einzelfotos (`gruendlich`) zusaetzlich feste Winkel - im Sekundentakt
  der Live-Kamera waere das zu langsam, dort haelt man die Packung eben gerade.

Die Texterkennung ist optional: fehlt das Paket (Entwicklungsrechner ohne
requirements-ocr.txt), meldet `verfuegbar()` False und das Handy bietet nur
den Weg ueber die KI an.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from datetime import date

from .dates import mhd_aus_teilen, mhd_ohne_jahr

log = logging.getLogger(__name__)

_engine = None
_engine_lock = threading.Lock()
# Ein Bild nach dem anderen: die Erkennung nutzt ohnehin alle Kerne, und das
# Handy schickt im Sekundentakt - parallel waere nur jedes einzelne langsamer.
_reihe = asyncio.Semaphore(1)

# Zusaetzliche Winkel fuer Einzelfotos, nach Haeufigkeit sortiert.
_WINKEL_GRUENDLICH = (20, -20, 40, -40, 60, -60, 90, -90)
# Danach gibt ein Einzelfoto auf - ein Durchgang dauert gut eine halbe Sekunde.
_GRUENDLICH_HOECHSTENS_S = 10.0


def verfuegbar() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _ocr():
    """Die Modelle einmal laden (rund eine Sekunde) und dann behalten."""
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        return _engine


# --------------------------------------------------------------- Erkennung
def _laden(bild: bytes):
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(bild, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    # Groesser als 1600 Pixel bringt nichts ausser Rechenzeit.
    faktor = 1600 / max(img.shape[:2])
    if faktor < 1:
        img = cv2.resize(img, None, fx=faktor, fy=faktor, interpolation=cv2.INTER_AREA)
    return img


def _punkte(img):
    """Nadeldruck zu Strichen: dunkle Punkte um ein Pixel ausweiten."""
    import cv2
    import numpy as np

    grau = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(cv2.erode(grau, np.ones((3, 3), np.uint8)), cv2.COLOR_GRAY2BGR)


def _drehen(img, winkel: float):
    """Um `winkel` Grad drehen, ohne Ecken abzuschneiden."""
    import cv2

    h, b = img.shape[:2]
    mitte = (b / 2, h / 2)
    m = cv2.getRotationMatrix2D(mitte, winkel, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nb, nh = int(h * sin + b * cos), int(h * cos + b * sin)
    m[0, 2] += nb / 2 - mitte[0]
    m[1, 2] += nh / 2 - mitte[1]
    return cv2.warpAffine(img, m, (nb, nh), borderValue=(255, 255, 255))


def _lauf(img) -> list[tuple[str, float, float, float]]:
    """(Text, Sicherheit, Winkel der Zeile, Laenge der Zeile) je erkannter Zeile."""
    ergebnis, _ = _ocr()(img)
    zeilen = []
    for box, text, sicher in ergebnis or []:
        dx, dy = box[1][0] - box[0][0], box[1][1] - box[0][1]
        zeilen.append((str(text), float(sicher), math.degrees(math.atan2(dy, dx)), math.hypot(dx, dy)))
    return zeilen


_BRUCHSTUECK = re.compile(r"\d{1,2}\s*[./:]\s*\d")


def _winkel_aus(zeilen) -> list[float]:
    """Drehwinkel zum Ausprobieren: erst Datums-Bruchstuecke, dann die Hauptrichtung."""
    winkel = [w for text, _, w, laenge in zeilen if laenge > 15 and _BRUCHSTUECK.search(text)]
    lang = [(w, laenge) for _, _, w, laenge in zeilen if laenge > 20]
    if lang:
        summe = sum(laenge for _, laenge in lang)
        winkel.append(sum(w * laenge for w, laenge in lang) / summe)
    ergebnis: list[float] = []
    for w in winkel:
        if abs(w) > 4 and all(abs(w - x) > 6 for x in ergebnis):
            ergebnis.append(round(w, 1))
    return ergebnis[:3]


def _lesen_sync(bild: bytes, gruendlich: bool) -> dict:
    """Gestuft lesen bis zum ersten Datum. Liefert auch jeden Durchgang (Testseite)."""
    img = _laden(bild)
    if img is None:
        return {"datum": "", "gelesen": "", "zeilen": [], "durchgaenge": [], "groesse": [0, 0]}
    groesse = [int(img.shape[1]), int(img.shape[0])]
    durchgaenge = []
    gesehen: list = []
    erste: list = []

    def versuch(name, bildchen):
        zeilen = _lauf(bildchen)
        if not durchgaenge:
            erste.extend(zeilen)
        gesehen.extend(zeilen)
        kand = kandidaten([z[0] for z in zeilen])
        durchgaenge.append({"name": name, "datum": kand[0]["datum"] if kand else "",
                            "texte": len(zeilen)})
        return zeilen, kand

    plan = [("original", lambda: img), ("punkte", lambda: _punkte(img))]
    start = time.perf_counter()
    i = 0
    while i < len(plan) and time.perf_counter() - start < _GRUENDLICH_HOECHSTENS_S:
        name, erzeugen = plan[i]
        zeilen, kand = versuch(name, erzeugen())
        if kand:
            return {"datum": kand[0]["datum"], "gelesen": kand[0]["gelesen"], "kandidaten": kand,
                    "zeilen": zeilen, "durchgaenge": durchgaenge, "groesse": groesse}
        if i == 1:
            # Nach den ersten beiden: Richtungen aus dem, was bisher erkannt wurde.
            for w in _winkel_aus(gesehen):
                plan.append((f"punkte, {w:+.0f}°", lambda w=w: _punkte(_drehen(img, w))))
            if gruendlich:
                schon = [float(n.split(", ")[1].rstrip("°")) for n, _ in plan[2:]]
                for w in _WINKEL_GRUENDLICH:
                    if all(abs(w - x) > 8 for x in schon):
                        plan.append((f"punkte, {w:+d}°", lambda w=w: _punkte(_drehen(img, w))))
        i += 1
    # Nichts gefunden: die Zeilen des ersten Durchgangs zeigen, was im Bild steht.
    return {"datum": "", "gelesen": "", "kandidaten": [], "zeilen": erste,
            "durchgaenge": durchgaenge, "groesse": groesse}


async def texte_lesen(bild: bytes) -> list[str]:
    """Alle erkannten Textzeilen eines JPEG/PNG (nur der erste Durchgang)."""
    img = _laden(bild)
    if img is None:
        return []
    async with _reihe:
        zeilen = await asyncio.to_thread(_lauf, img)
    return [z[0] for z in zeilen]


# ------------------------------------------------------------------- Regeln
# Trennzeichen eines Datums. Kein Doppelpunkt: "07:08" ist eine Uhrzeit.
_T = r"\s*[./\-]\s*"
_MUSTER = [
    # 16.11.2026 · 28/10/2027 · 07/10/27
    ("voll", re.compile(rf"(?<!\d)(\d{{1,2}}){_T}(\d{{1,2}}){_T}(\d{{4}}|\d{{2}})(?!\d)")),
    # 2810/2027 - die Erkennung verschluckt bei Nadeldruck gern das erste Trennzeichen
    ("voll", re.compile(rf"(?<!\d)(\d{{2}})(\d{{2}}){_T}(\d{{4}}|\d{{2}})(?!\d)")),
    # 28102027 - alle Trennzeichen weg; nur mit 20xx, sonst ist es eine Charge
    ("voll", re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")),
    # 01.2027 - nur Monat und Jahr: bis Monatsende
    ("monat", re.compile(rf"(?<!\d)(\d{{1,2}}){_T}(20\d{{2}})(?!\d)")),
    # 31.10. - ohne Jahr (Frischware). Nur mit Hinweiswort, siehe kandidaten(),
    # und nur zweistellig wie gedruckt: "1.1" war ein falsch gelesenes "1,15 g".
    ("ohne_jahr", re.compile(r"(?<![\d.,])(\d{2})\.\s*(\d{2})\.?(?![\d.]|\s*\d)")),
]
# Worte, die ein MHD ankuendigen.
_HINWEIS = re.compile(r"mhd|haltbar|verbrauch|best|bis|exp|use by|bbe|ddm", re.I)
# Wie weit ein Hinweiswort vor einem Datum ohne Jahr stehen darf. Die
# Erkennung liefert Zeilen nach Hoehe sortiert; Spalten daneben ("Salz",
# "Frische Rezepte") schieben sich dazwischen.
_HINWEIS_ZEILEN_OHNE_JAHR = 4


def _kandidaten(text: str, heute: date, ohne_jahr: bool) -> list[tuple[str, str]]:
    """(ISO-Datum, gelesener Ausschnitt) fuer jedes Datum im Text."""
    # Die Erkennung liest Nadeldruck-Nullen manchmal als O.
    sauber = re.sub(r"(?<=\d)[oO]|[oO](?=\d)", "0", text)
    gefunden: list[tuple[str, str]] = []
    belegt: list[tuple[int, int]] = []
    for art, muster in _MUSTER:
        if art == "ohne_jahr" and not ohne_jahr:
            continue
        for m in muster.finditer(sauber):
            if any(a < m.end() and m.start() < b for a, b in belegt):
                continue        # schon als vollstaendiges Datum gelesen
            if art == "voll":
                iso = mhd_aus_teilen(m.group(1), m.group(2), m.group(3), heute)
            elif art == "monat":
                iso = mhd_aus_teilen(0, m.group(1), m.group(2), heute)
            else:
                iso = mhd_ohne_jahr(m.group(1), m.group(2), heute)
            if iso:
                gefunden.append((iso, m.group(0)))
                belegt.append((m.start(), m.end()))
    return gefunden


def kandidaten(texte: list[str], heute: date | None = None) -> list[dict]:
    """Alle Daten in den Texten, das gewaehlte zuerst - fuer mhd_finden und die Testseite."""
    heute = heute or date.today()
    liste = []
    for i, text in enumerate(texte):
        hinweis = bool(_HINWEIS.search(text + " " + (texte[i - 1] if i else "")))
        # Ein Datum ohne Jahr ist nur eins, wenn ein Hinweiswort in der Naehe
        # steht - sonst waere jede Angabe wie "22.5" in den Naehrwerten verdaechtig.
        nah = any(_HINWEIS.search(t) for t in texte[max(0, i - _HINWEIS_ZEILEN_OHNE_JAHR):i + 1])
        for iso, gelesen in _kandidaten(text, heute, ohne_jahr=nah):
            liste.append({"datum": iso, "gelesen": gelesen.strip(), "zeile": i, "hinweis": hinweis or nah})
    # Stehen mehrere Daten da, gewinnt eins mit Hinweiswort davor
    # ("mindestens haltbar bis"), sonst das spaeteste - ein Herstell- oder
    # Abpackdatum liegt immer vor dem MHD.
    liste.sort(key=lambda k: (k["hinweis"], k["datum"]), reverse=True)
    return liste


def mhd_finden(texte: list[str], heute: date | None = None) -> dict:
    """Das MHD unter den erkannten Texten: {"datum": ISO oder "", "gelesen": …}."""
    liste = kandidaten(texte, heute)
    if not liste:
        return {"datum": "", "gelesen": ""}
    return {"datum": liste[0]["datum"], "gelesen": liste[0]["gelesen"]}


# ------------------------------------------------------------------ Aufrufe
async def mhd_lesen(bild: bytes, gruendlich: bool = False) -> dict | None:
    """Foto -> {"datum", "gelesen"}; None, wenn die Erkennung nicht laeuft."""
    if not verfuegbar():
        return None
    try:
        async with _reihe:
            r = await asyncio.to_thread(_lesen_sync, bild, gruendlich)
    except Exception:            # kaputtes Bild, Modell nicht ladbar - nie den Server stoeren
        log.exception("MHD: Texterkennung fehlgeschlagen")
        return None
    return {"datum": r["datum"], "gelesen": r["gelesen"]}


async def pruefen(bild: bytes, gruendlich: bool = True) -> dict | None:
    """Alles, was die Erkennung sieht - fuer die Testseite unter System."""
    if not verfuegbar():
        return None
    start = time.perf_counter()
    async with _reihe:
        r = await asyncio.to_thread(_lesen_sync, bild, gruendlich)
    return {
        "datum": r["datum"],
        "gelesen": r["gelesen"],
        "kandidaten": r.get("kandidaten", []),
        "texte": [{"text": z[0], "sicher": round(z[1], 2)} for z in r["zeilen"]],
        "durchgaenge": r["durchgaenge"],
        "groesse": r["groesse"],
        "ms": round((time.perf_counter() - start) * 1000),
    }
