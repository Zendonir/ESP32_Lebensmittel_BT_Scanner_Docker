"""Etikett-Layouts fuer den Brother: Name, Marke, MHD, Menge, Ort,
Einlagerdatum und QR-Code.

Jedes Layout zeichnet in Leserichtung auf `breite` x `hoehe` Punkte
(Graustufen); gedreht und gerastert wird danach in `brother.etikett_bild`.
Hochkant (Breite <= 1,2 x Hoehe) gilt immer `hochkant`.

Passt nicht alles, wird erst die Schrift kleiner (`_stapel`), dann fallen
Angaben von hinten weg. Name, MHD und Code bleiben immer.

"standard" ist das bisherige Etikett (`brother._zeichnen`) und steht hier
nicht.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

from . import categories
from .dates import to_display
from .hochformat import QR_RUHE, _gekuerzt, _menge, _passend, _qr, _schrift, _zeilenhoehe

# Name -> Beschreibung (Auswahl in der Oberflaeche).
LAYOUTS = {
    "standard": "Standard (bisher)",
    "klassisch": "A Klassisch – QR rechts, Fußzeile",
    "balken": "B MHD-Balken oben",
    "raster": "C QR links, Felder",
    "tabelle": "D Tabelle",
    "hochkant": "E Hochkant – alles untereinander",
}

# Der Druckbereich des Brother hat seinen Rand schon (je 3 mm an den Enden,
# Pins links frei) - hier nur noch Luft zur Kante.
RAND = 8
NUMMER = 14          # Schrift der Etikettennummer unter dem Code
FAKTOREN = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5)


def daten(item: dict) -> dict:
    ein = to_display(item.get("added_date", ""))
    return {
        "name": categories.display_name(item.get("name", ""), item.get("subcategory", ""))
        or "Unbekannt",
        "marke": str(item.get("brand") or "").strip(),
        "mhd": to_display(item.get("expiry_date", "")),
        "menge": _menge(item),
        "ort": str(item.get("location") or "").strip(),
        "ein": ein,
        "label": str(item.get("label") or ""),
    }


# ------------------------------------------------------------ Bausteine
@dataclass
class Eintrag:
    text: str
    groesse: int          # groesste Schrift
    klein: int = 12       # kleinste Schrift
    fett: bool = False
    zeilen: int = 1
    pflicht: bool = False
    abstand: int = 0      # Luft danach


def _stapel(eintraege: list[Eintrag], breite: int, hoehe: int) -> list[tuple]:
    """Eintraege untereinander in `breite` x `hoehe`: erst alle Schriften
    gemeinsam kleiner, dann fallen optionale Eintraege von hinten weg.
    Gibt [(schrift, zeilen, abstand)] zurueck."""
    eintraege = [e for e in eintraege if e.text]
    while True:
        for faktor in FAKTOREN:
            teile = []
            for e in eintraege:
                g = max(e.klein, int(e.groesse * faktor))
                treffer = _passend(e.text, breite, tuple(range(g, e.klein - 1, -2)) or (e.klein,),
                                   e.fett, e.zeilen)
                if treffer is None:
                    schrift = _schrift(e.klein, e.fett)
                    treffer = (schrift, _gekuerzt(e.text, schrift, breite, e.zeilen))
                teile.append((*treffer, e.abstand))
            if _hoehe(teile) <= hoehe:
                return teile
        optional = [i for i, e in enumerate(eintraege) if not e.pflicht]
        if not optional:
            return teile
        del eintraege[optional[-1]]


def _hoehe(teile) -> int:
    return sum(_zeilenhoehe(s) * len(z) + a for s, z, a in teile)


def _schreiben(zeichner, teile, x: int, y: int, fill: int = 0) -> int:
    for schrift, zeilen, abstand in teile:
        for zeile in zeilen:
            zeichner.text((x, y), zeile, font=schrift, fill=fill)
            y += _zeilenhoehe(schrift)
        y += abstand
    return y


def _code(label: str, max_b: int, max_h: int, nummer: bool = True) -> Image.Image | None:
    """QR-Code (mit Ruhezone), so gross es geht, darunter auf Wunsch die
    Etikettennummer - falls sich der Code einmal nicht lesen laesst."""
    if not label:
        return None
    schrift = _schrift(NUMMER, True)
    for modul in (6, 5, 4, 3, 2):
        qr = _qr(label, modul)
        # Die Nummer darf in die untere Ruhezone ragen.
        unten = max(0, _zeilenhoehe(schrift) - QR_RUHE * modul) if nummer else 0
        if qr.width <= max_b and qr.height + unten <= max_h:
            break
    breite = max(qr.width, int(schrift.getlength(label)) + 2) if nummer else qr.width
    bild = Image.new("L", (breite, qr.height + unten), 255)
    bild.paste(qr, ((breite - qr.width) // 2, 0))
    if nummer:
        ImageDraw.Draw(bild).text(((breite - schrift.getlength(label)) / 2,
                                   qr.height - QR_RUHE * modul), label, font=schrift, fill=0)
    return bild


def _code_min(label: str) -> int:
    """Hoehe des kleinsten Codes, der sich noch sicher lesen laesst (Modul 3)."""
    return _qr(label, 3).height if label else 0


def _mhd(d: dict) -> str:
    return f"MHD {d['mhd']}" if d["mhd"] else "kein MHD"


# --------------------------------------------------------------- Layouts
def klassisch(item: dict, breite: int, hoehe: int) -> Image.Image:
    """A: Name gross, Marke, MHD fett, QR rechts; unten Menge · Ort · Datum."""
    d = daten(item)
    bild = Image.new("L", (breite, hoehe), 255)
    z = ImageDraw.Draw(bild)
    fuss_text = " · ".join(t for t in (d["menge"], d["ort"],
                                        f"eingel. {d['ein']}" if d["ein"] else "") if t)
    fuss = _stapel([Eintrag(fuss_text, 18, 11)], breite - 2 * RAND, hoehe) if fuss_text else []
    fuss_h = _hoehe(fuss) + 6 if fuss else 0
    oben_h = hoehe - 2 * RAND - fuss_h
    code = _code(d["label"], int(breite * 0.36), oben_h)
    code_b = code.width if code else 0
    links = breite - 2 * RAND - code_b - (RAND if code else 0)
    teile = _stapel([Eintrag(d["name"], 40, 16, True, 2, True, 2),
                     Eintrag(d["marke"], 18, 11, abstand=4),
                     Eintrag(_mhd(d), 36, 16, True, 1, True)], links, oben_h)
    _schreiben(z, teile, RAND, RAND - 2)
    if code:
        bild.paste(code, (breite - RAND - code_b, RAND))
    if fuss:
        y = hoehe - RAND - _hoehe(fuss)
        z.line([RAND, y - 4, breite - RAND, y - 4], fill=0, width=2)
        _schreiben(z, fuss, RAND, y)
    return bild


def balken(item: dict, breite: int, hoehe: int) -> Image.Image:
    """B: MHD weiss auf schwarzem Balken oben - faellt im Regal sofort auf."""
    d = daten(item)
    bild = Image.new("L", (breite, hoehe), 255)
    z = ImageDraw.Draw(bild)
    groesste = max(16, min(40, int(hoehe * 0.24)))
    kopf = _stapel([Eintrag(_mhd(d), groesste, 14, True, 1, True)], breite - 2 * RAND, hoehe)
    schrift, zeilen, _ = kopf[0]
    balken_h = _zeilenhoehe(schrift) + 4
    z.rectangle([0, 0, breite, balken_h], fill=0)
    z.text(((breite - schrift.getlength(zeilen[0])) / 2, 2), zeilen[0], font=schrift, fill=255)
    rest = hoehe - balken_h - RAND - 4
    code = _code(d["label"], int(breite * 0.32), rest, nummer=rest >= 120)
    code_b = code.width if code else 0
    links = breite - 2 * RAND - code_b - (RAND if code else 0)
    teile = _stapel([Eintrag(d["name"], 30, 16, True, 2, True, 2),
                     Eintrag(d["marke"], 16, 11),
                     Eintrag(d["menge"], 16, 11),
                     Eintrag(d["ort"], 16, 11),
                     Eintrag(f"eingel. {d['ein']}" if d["ein"] else "", 16, 11)], links, rest)
    _schreiben(z, teile, RAND, balken_h + 4)
    if code:
        bild.paste(code, (breite - RAND - code_b, balken_h + 4))
    return bild


def raster(item: dict, breite: int, hoehe: int) -> Image.Image:
    """C: QR links, rechts Name/Marke und vier Felder mit Ueberschrift."""
    d = daten(item)
    bild = Image.new("L", (breite, hoehe), 255)
    z = ImageDraw.Draw(bild)
    code = _code(d["label"], int(breite * 0.34), hoehe - 2 * RAND)
    x0 = RAND + (code.width + 4 if code else 0)
    if code:
        bild.paste(code, (RAND - 6, RAND))
    rechts = breite - x0 - RAND
    ueber = _schrift(12, False)
    feld_min = 2 * (_zeilenhoehe(ueber) + _zeilenhoehe(_schrift(14, True)))
    kopf = _stapel([Eintrag(d["name"], 32, 14, True, 2, True),
                    Eintrag(d["marke"], 16, 11)], rechts, hoehe - 2 * RAND - feld_min - 6)
    y = _schreiben(z, kopf, x0, RAND - 4) + 2
    z.line([x0, y, breite - RAND, y], fill=0, width=2)
    y += 4
    zelle_b, zelle_h = rechts // 2, (hoehe - RAND - y) // 2
    felder = (("MHD", d["mhd"] or "–"), ("Menge", d["menge"] or "–"),
              ("Ort", d["ort"] or "–"), ("Eingelagert", d["ein"] or "–"))
    for i, (titel, wert) in enumerate(felder):
        cx, cy = x0 + (i % 2) * zelle_b, y + (i // 2) * zelle_h
        z.text((cx, cy), titel, font=ueber, fill=0)
        platz = zelle_h - _zeilenhoehe(ueber) + 2
        groesste = min(34 if i == 0 else 28, max(14, platz - 4))
        wert_teile = _stapel([Eintrag(wert, groesste, 11, True, 2 if titel == "Ort" else 1, True)],
                             zelle_b - 6, platz)
        _schreiben(z, wert_teile, cx, cy + _zeilenhoehe(ueber) - 2)
    return bild


def tabelle(item: dict, breite: int, hoehe: int) -> Image.Image:
    """D: Name und Marke ueber die ganze Breite, darunter beschriftete
    Zeilen, QR unten rechts."""
    d = daten(item)
    bild = Image.new("L", (breite, hoehe), 255)
    z = ImageDraw.Draw(bild)
    zeilen = [(k, v) for k, v in (("MHD", d["mhd"] or "–"), ("Menge", d["menge"]),
                                  ("Ort", d["ort"]), ("Eingel.", d["ein"])) if v]
    kopf = _stapel([Eintrag(d["name"], 34, 14, True, 2, True),
                    Eintrag(d["marke"], 18, 11)], breite - 2 * RAND,
                   hoehe - 2 * RAND - 5 - max(16 * len(zeilen), _code_min(d["label"])))
    y = _schreiben(z, kopf, RAND, RAND - 4)
    z.line([RAND, y + 1, breite - RAND, y + 1], fill=0, width=2)
    y += 5
    rest = hoehe - RAND - y
    # Wenig Platz: lieber ein lesbarer Code ohne Nummer als ein winziger.
    code = _code(d["label"], int(breite * 0.32), rest, nummer=rest >= 110)
    code_b = code.width if code else 0
    if code:
        bild.paste(code, (breite - RAND - code_b, y))
    zh = rest // len(zeilen)
    klein = _schrift(max(10, min(16, zh - 6)), False)
    spalte = int(klein.getlength("Eingel.")) + 10
    wert_b = breite - 2 * RAND - spalte - code_b - (RAND if code else 0)
    for i, (titel, wert) in enumerate(zeilen):
        cy = y + i * zh
        groesste = max(12, min(30, zh + 4) if titel == "MHD" else min(22, zh - 2))
        wert_teile = _stapel([Eintrag(wert, groesste, 10, True, 2 if titel == "Ort" else 1, True)],
                             wert_b, zh + 4)
        schrift = wert_teile[0][0]
        z.text((RAND, cy + max(0, _zeilenhoehe(schrift) - _zeilenhoehe(klein)) // 2),
               titel, font=klein, fill=0)
        _schreiben(z, wert_teile, RAND + spalte, cy)
    return bild


def hochkant(item: dict, breite: int, hoehe: int) -> Image.Image:
    """E: alles untereinander, MHD sehr gross, QR unten mittig."""
    d = daten(item)
    bild = Image.new("L", (breite, hoehe), 255)
    z = ImageDraw.Draw(bild)
    innen = breite - 2 * RAND
    # Mindestens ein Drittel bleibt dem Code.
    code_min = min(innen, hoehe // 3) if d["label"] else 0
    eintraege = [Eintrag(d["name"], 40, 16, True, 3, True, 2),
                 Eintrag(d["marke"], 20, 12, zeilen=2, abstand=4),
                 Eintrag("MHD" if d["mhd"] else "", 16, 12, pflicht=True),
                 Eintrag(d["mhd"] or "kein MHD", 52, 18, True, 1, True, 8),
                 Eintrag(d["menge"], 22, 12),
                 Eintrag(d["ort"], 22, 12, zeilen=2),
                 Eintrag(f"eingel. {d['ein']}" if d["ein"] else "", 22, 12)]
    teile = _stapel(eintraege, innen, hoehe - 2 * RAND - code_min)
    # Linie zwischen MHD und den uebrigen Angaben.
    y = RAND - 4
    for n, (schrift, zeilen, abstand) in enumerate(teile):
        y = _schreiben(z, [(schrift, zeilen, abstand)], RAND, y)
        if abstand == 8 and n + 1 < len(teile):
            z.line([RAND, y - 5, breite - RAND, y - 5], fill=0, width=2)
    code = _code(d["label"], innen, hoehe - RAND - y)
    if code:
        bild.paste(code, ((breite - code.width) // 2, hoehe - RAND - code.height))
    return bild


ZEICHNER = {"klassisch": klassisch, "balken": balken, "raster": raster,
            "tabelle": tabelle, "hochkant": hochkant}


def zeichnen(layout: str, item: dict, breite: int, hoehe: int) -> Image.Image | None:
    """Das Etikett im gewaehlten Layout - None fuer "standard"/unbekannt."""
    if layout not in ZEICHNER:
        return None
    if breite <= hoehe * 1.2:
        layout = "hochkant"
    return ZEICHNER[layout](item, breite, hoehe)
