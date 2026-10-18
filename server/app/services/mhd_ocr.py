"""MHD vom Foto lesen - lokal im Container, ohne Dienst im Netz.

Zwei Schritte, bewusst getrennt:

1. `texte_lesen()` - Texterkennung (RapidOCR: die PaddleOCR-Modelle ueber
   ONNX). Laeuft auf dem Prozessor, rund eine halbe Sekunde je Bildausschnitt.
   Sie liefert alles, was auf der Verpackung steht: Naehrwerte, Chargen,
   Uhrzeiten - und irgendwo dazwischen das Datum.
2. `mhd_finden()` - entscheidet, welcher Text das MHD ist. Reine Regeln, ohne
   Modell, und deshalb ohne Kamera testbar (tests/test_mhd_ocr.py, mit den
   Aufdrucken echter Verpackungen).

Die Texterkennung ist optional: fehlt das Paket (Entwicklungsrechner ohne
requirements-ocr.txt), meldet `verfuegbar()` False und das Handy bietet nur
den Weg ueber die KI an.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import date

from .dates import mhd_aus_teilen

log = logging.getLogger(__name__)

_engine = None
_engine_lock = threading.Lock()
# Ein Bild nach dem anderen: die Erkennung nutzt ohnehin alle Kerne, und das
# Handy schickt im Sekundentakt - parallel waere nur jedes einzelne langsamer.
_reihe = asyncio.Semaphore(1)


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


def _texte_sync(bild: bytes) -> list[str]:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(bild, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    # Groesser als 1600 Pixel bringt nichts ausser Rechenzeit.
    faktor = 1600 / max(img.shape[:2])
    if faktor < 1:
        img = cv2.resize(img, None, fx=faktor, fy=faktor, interpolation=cv2.INTER_AREA)
    ergebnis, _ = _ocr()(img)
    return [str(r[1]) for r in (ergebnis or [])]


async def texte_lesen(bild: bytes) -> list[str]:
    """Alle erkannten Textzeilen eines JPEG/PNG, oben nach unten."""
    async with _reihe:
        return await asyncio.to_thread(_texte_sync, bild)


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
]
# Worte, die ein MHD ankuendigen - auf derselben oder der vorigen Zeile.
_HINWEIS = re.compile(r"mhd|haltbar|verbrauch|best|bis|exp|use by|bbe|ddm", re.I)


def _kandidaten(text: str, heute: date) -> list[tuple[str, str]]:
    """(ISO-Datum, gelesener Ausschnitt) fuer jedes Datum im Text."""
    # Die Erkennung liest Nadeldruck-Nullen manchmal als O.
    sauber = re.sub(r"(?<=\d)[oO]|[oO](?=\d)", "0", text)
    gefunden: list[tuple[str, str]] = []
    belegt: list[tuple[int, int]] = []
    for art, muster in _MUSTER:
        for m in muster.finditer(sauber):
            if any(a < m.end() and m.start() < b for a, b in belegt):
                continue        # schon als vollstaendiges Datum gelesen
            if art == "voll":
                iso = mhd_aus_teilen(m.group(1), m.group(2), m.group(3), heute)
            else:
                iso = mhd_aus_teilen(0, m.group(1), m.group(2), heute)
            if iso:
                gefunden.append((iso, m.group(0)))
                belegt.append((m.start(), m.end()))
    return gefunden


def mhd_finden(texte: list[str], heute: date | None = None) -> dict:
    """Das MHD unter den erkannten Texten: {"datum": ISO oder "", "gelesen": …}.

    Stehen mehrere Daten da, gewinnt eins mit Hinweiswort davor ("mindestens
    haltbar bis"), sonst das spaeteste - ein Herstell- oder Abpackdatum liegt
    immer vor dem MHD.
    """
    heute = heute or date.today()
    beste: tuple[bool, str, str] | None = None
    for i, text in enumerate(texte):
        umfeld = text + " " + (texte[i - 1] if i else "")
        hinweis = bool(_HINWEIS.search(umfeld))
        for iso, gelesen in _kandidaten(text, heute):
            kandidat = (hinweis, iso, gelesen)
            if beste is None or kandidat[:2] > beste[:2]:
                beste = kandidat
    if beste is None:
        return {"datum": "", "gelesen": ""}
    return {"datum": beste[1], "gelesen": beste[2].strip()}


async def mhd_lesen(bild: bytes) -> dict | None:
    """Foto -> {"datum", "gelesen"}; None, wenn die Erkennung nicht laeuft."""
    if not verfuegbar():
        return None
    try:
        texte = await texte_lesen(bild)
    except Exception:            # kaputtes Bild, Modell nicht ladbar - nie den Server stoeren
        log.exception("MHD: Texterkennung fehlgeschlagen")
        return None
    return mhd_finden(texte)
