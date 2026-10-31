"""GS1-Codes lesen: PDF417, DataMatrix, GS1-128 und QR mit Datenbezeichnern.

Frischetheke und abgepacktes Fleisch tragen zunehmend einen 2D-Code statt
oder neben der EAN, z. B. (PDF417 auf einer Fleischschale):

    010430652852811517261102310300115939220926
    (01) 04306528528115  GTIN       -> EAN 4306528528115
    (17) 261102          MHD        -> 2026-11-02
    (3103) 001159        Gewicht    -> 1,159 kg
    (3922) 0926          Preis      -> 9,26 €

Gelesen wird nur, was fuer das Einlagern zaehlt: Produkt (fuer die
Nachschlage wie bei einer EAN), MHD, Menge, Charge. Was ein Scanner als
Trennzeichen (FNC1) schickt, ist uneinheitlich: GS (0x1D), als Tastatur
oft "]" (Strg+] ohne Strg) - beides gilt als Trenner; "]" kommt im
GS1-Zeichensatz nicht vor.

Auch GS1 Digital Link wird erkannt (https://…/01/<GTIN>/10/<Charge>?17=…),
wie ihn die neuen QR-Codes auf Verpackungen tragen.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from urllib.parse import parse_qs, urlparse

GS = "\x1d"

# Datenbezeichner mit fester Laenge der Daten. Alle anderen sind variabel
# (bis zum Trenner oder Ende). Vierstellige 31xx-36xx: 6 Ziffern.
_FEST = {"00": 18, "01": 14, "02": 14, "03": 14, "11": 6, "12": 6, "13": 6, "15": 6,
         "16": 6, "17": 6, "20": 2, "410": 13, "411": 13, "412": 13, "413": 13,
         "414": 13, "415": 13, "416": 13, "422": 3, "7003": 10}
# Variable Laenge, Hoechstlaenge
_VARIABEL = {"10": 20, "21": 20, "22": 20, "240": 30, "241": 30, "250": 30, "251": 30,
             "30": 8, "37": 8, "400": 30, "401": 30, "403": 30, "420": 20, "421": 12,
             "7002": 30, "8008": 12}
# Vierstellig, je mit Dezimalstelle an Stelle 4: 31xx-36xx (6 Ziffern),
# 390x-393x (variabel, Preise).
_VIERSTELLIG = re.compile(r"^(3[1-6]\d\d|39[0-3]\d|7003|7002|8008)")
_SYMBOLOGIE = re.compile(r"^\](C1|e0|d2|Q3|L2|J1)")


def _ai_bei(s: str, i: int) -> tuple[str, int | None] | None:
    """Datenbezeichner an Stelle i und Laenge seiner Daten (None = variabel)."""
    vier = _VIERSTELLIG.match(s[i:])
    if vier:
        ai = vier.group(1)
        if ai[:2] in ("31", "32", "33", "34", "35", "36"):
            return ai, 6
        if ai == "7003":
            return ai, 10
        return ai, None
    for laenge in (2, 3):
        ai = s[i:i + laenge]
        if ai in _FEST:
            return ai, _FEST[ai]
        if ai in _VARIABEL:
            return ai, None
    return None


def _elemente(s: str) -> dict[str, str]:
    """Element-String in {AI: Wert}. Bricht bei Unbekanntem ab und behaelt,
    was bis dahin gelesen war."""
    werte: dict[str, str] = {}
    i = 0
    while i < len(s):
        if s[i] == GS:
            i += 1
            continue
        treffer = _ai_bei(s, i)
        if treffer is None:
            break
        ai, laenge = treffer
        i += len(ai)
        if laenge:
            wert = s[i:i + laenge]
            if len(wert) < laenge:
                break
            i += laenge
        else:
            ende = s.find(GS, i)
            ende = len(s) if ende < 0 else ende
            wert = s[i:ende]
            i = ende
        werte[ai] = wert
    return werte


def _datum(yymmdd: str) -> str:
    """GS1-Datum (JJMMTT, TT=00 = Monatsende) als ISO."""
    if not re.fullmatch(r"\d{6}", yymmdd):
        return ""
    jj, mm, tt = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    jahr = 2000 + jj
    if not 1 <= mm <= 12:
        return ""
    if tt == 0:
        tt = calendar.monthrange(jahr, mm)[1]
    try:
        return date(jahr, mm, tt).isoformat()
    except ValueError:
        return ""


def _dezimal(ai: str, wert: str) -> float | None:
    """31xx/39xx: letzte Stelle des AI = Nachkommastellen."""
    try:
        return int(wert) / 10 ** int(ai[3])
    except (ValueError, IndexError):
        return None


def _digital_link(code: str) -> dict[str, str] | None:
    """https://id.gs1.org/01/04306528528115/10/ABC?17=261102 -> {AI: Wert}."""
    try:
        url = urlparse(code)
    except ValueError:
        return None
    if url.scheme not in ("http", "https"):
        return None
    teile = [t for t in url.path.split("/") if t]
    if "01" not in teile[:-1]:
        return None
    # Ab der GTIN steht der Pfad paarweise: /01/<GTIN>/10/<Charge>/21/<Serie>
    rest = teile[teile.index("01"):]
    werte = {rest[k]: rest[k + 1] for k in range(0, len(rest) - 1, 2)}
    for ai, liste in parse_qs(url.query).items():
        if liste:
            werte.setdefault(ai, liste[0])
    return werte


def lesen(code: str) -> dict | None:
    """Ein gescannter Code als GS1-Inhalt - oder None, wenn er keiner ist.

    Ergebnis: {"gtin", "ean", "mhd" (ISO), "menge", "einheit", "charge",
    "preis", "roh": {AI: Wert}}. Eine gewoehnliche EAN ist *kein* GS1-Inhalt
    in diesem Sinn (dafuer gibt es nichts zu lesen ausser ihr selbst).
    """
    code = (code or "").strip()
    if not code:
        return None
    werte = _digital_link(code) if "://" in code else None
    if werte is None:
        s = _SYMBOLOGIE.sub("", code)
        # Tastatur-Scanner schicken FNC1 oft als "]" (Strg+]) - kommt im
        # GS1-Zeichensatz nicht vor, ist also immer ein Trenner.
        s = s.replace("]", GS).lstrip(GS)
        # Mindestens GTIN plus ein weiteres Element - eine EAN-14 allein
        # (01 + 14 Ziffern ohne Rest) bleibt ein gewoehnlicher Code.
        if not re.match(r"^(01|02)\d{14}", s) or len(s.replace(GS, "")) < 18:
            return None
        werte = _elemente(s)
        if len(werte) < 2:
            return None
    gtin = werte.get("01") or werte.get("02") or ""
    if not re.fullmatch(r"\d{14}", gtin):
        return None
    # GTIN-14 mit fuehrender 0 = EAN-13 (so steht sie bei OpenFoodFacts).
    ean = gtin[1:] if gtin.startswith("0") else gtin
    ergebnis: dict = {"gtin": gtin, "ean": ean, "mhd": "", "menge": None, "einheit": "",
                      "charge": werte.get("10", ""), "preis": None, "roh": werte}
    # Verbrauchsdatum (17) vor MHD (15) vor Verkaufsdatum (16).
    for ai in ("17", "15", "16"):
        if werte.get(ai):
            ergebnis["mhd"] = _datum(werte[ai])
            if ergebnis["mhd"]:
                break
    for ai, wert in werte.items():
        if ai.startswith("310"):            # Nettogewicht kg
            kg = _dezimal(ai, wert)
            if kg:
                ergebnis["menge"], ergebnis["einheit"] = round(kg * 1000), "g"
        elif ai.startswith("315") and ergebnis["menge"] is None:   # Volumen l
            liter = _dezimal(ai, wert)
            if liter:
                ergebnis["menge"], ergebnis["einheit"] = round(liter * 1000), "ml"
        elif ai.startswith("392"):          # zu zahlender Preis
            ergebnis["preis"] = _dezimal(ai, wert)
    if ergebnis["menge"] is None and werte.get("30", "").isdigit():
        ergebnis["menge"], ergebnis["einheit"] = int(werte["30"]), "St."
    return ergebnis
