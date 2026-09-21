"""Kalibrierdruck - misst, was der Drucker wirklich tut.

Bei einem Markengeraet steht im Datenblatt, wie es `ESC 3` auslegt und ob es
rueckwaerts fahren kann. Bei einem namenlosen Geraet steht das nirgends, und
raten hilft nicht: das ganze Etikettenlayout rechnet in Punkten, und wenn der
Drucker diese Punkte anders auslegt als angenommen, stimmt keine einzige
Zeile.

Deshalb dieser Streifen. Er stellt fuenf Fragen, deren Antworten man mit einem
Lineal vom Papier ablesen kann, und jede davon entscheidet ueber eine
Einstellung. Er ist bewusst *kein* Etikett: er laeuft ueber mehrere Teilungen
und benutzt render_label() nicht, damit die Messung nicht schon durch das
Zuschneiden verfaelscht wird.
"""

from __future__ import annotations

import base64

DOTS_PER_MM = 8

# Breite der Markierungsbalken in Punkten. 200 Punkte sind 25 mm - breit
# genug, um sie mit dem Lineal sauber anzulegen, und schmal genug, dass der
# ganze Streifen in den Sendepuffer der Firmware passt. 384 ist die Obergrenze
# (uebliche Kopfbreite eines 58-mm-Druckers); breiter schneidet der Drucker ab
# und die Messung waere Unsinn.
MARK_WIDTH_DOTS = 200
MAX_WIDTH_DOTS = 384

# Der Zeilentest: so viele Zeilen mit dieser Hoehe. 10 x 24 = 240 Punkte, also
# exakt 30,0 mm - eine Zahl, die sich mit einem gewoehnlichen Lineal ablesen
# laesst. Waere sie krumm, wuerde die Messung schon am Ablesen scheitern.
LINE_COUNT = 10
LINE_HEIGHT = 24
LINE_SOLL_MM = LINE_COUNT * LINE_HEIGHT / DOTS_PER_MM

# Das Rasterquadrat. Quadratisch, damit ein Unterschied zwischen waagerechter
# und senkrechter Punktdichte sofort auffaellt - genau der Fehler, der den
# QR-Code unlesbar gemacht hat.
BOX_DOTS = 80
BOX_SOLL_MM = BOX_DOTS / DOTS_PER_MM

# Rueckzug fuer den Test. 40 Punkte = 5 mm, weit genug, um mit blossem Auge
# zu sehen, ob er stattgefunden hat.
BACKFEED_DOTS = 40


def _raster(width: int, height: int, gefuellt: bool = True) -> dict:
    """Eine schwarze Flaeche als Rasterblock.

    Zeilenweise, ein Bit je Punkt, jede Zeile auf ganze Bytes aufgefuellt,
    hoechstwertiges Bit links - dasselbe Format, das die Firmware erwartet.
    """
    row_bytes = (width + 7) // 8
    fuellung = 0xFF if gefuellt else 0x00
    daten = bytearray()
    for _ in range(height):
        zeile = bytearray([fuellung] * row_bytes)
        # Ueberhaengende Bits der letzten Spalte loeschen, sonst druckt der
        # Balken bis zu sieben Punkte breiter als angegeben.
        rest = width % 8
        if rest and gefuellt:
            zeile[-1] = (0xFF << (8 - rest)) & 0xFF
        daten += zeile
    return {
        "t": "raster",
        "w": width,
        "h": height,
        "d": base64.b64encode(bytes(daten)).decode("ascii"),
    }


def breite_dots(line_mm: float) -> int:
    return min(MARK_WIDTH_DOTS, MAX_WIDTH_DOTS, int(line_mm * DOTS_PER_MM))


def build(line_mm: float = 48.0, chars: int = 32) -> dict:
    """Den Teststreifen als Druckauftrag bauen."""
    width = breite_dots(line_mm)
    marke = _raster(width, 8)

    def zeile(text: str, hoehe: int = LINE_HEIGHT, bold: bool = False) -> dict:
        return {"t": "text", "v": text, "align": 0, "bold": bold, "h": hoehe}

    blocks: list[dict] = [
        zeile("KALIBRIERUNG", bold=True),
        zeile(""),
        # --- 1: gilt ESC 3 in Punkten? ----------------------------------
        zeile("1) Zeilenabstand", bold=True),
        dict(marke),
    ]
    blocks += [zeile("|") for _ in range(LINE_COUNT)]
    blocks += [
        dict(marke),
        zeile(f"Balken-Innenkanten: {LINE_SOLL_MM:.1f} mm".replace(".", ",")),
        zeile(""),
        # --- 2: stimmt die Rastergeometrie? ------------------------------
        zeile("2) Rasterquadrat", bold=True),
        _raster(BOX_DOTS, BOX_DOTS),
        zeile(f"Soll: {BOX_SOLL_MM:.1f} x {BOX_SOLL_MM:.1f} mm".replace(".", ",")),
        zeile(""),
        # --- 3: kann der Drucker rueckwaerts? ----------------------------
        zeile("3) Rueckzug", bold=True),
        dict(marke),
        {"t": "back", "dots": BACKFEED_DOTS},
        dict(marke),
        zeile("Zwei Balken oder einer?"),
        zeile(""),
        # --- 4: hat er einen Lueckensensor? ------------------------------
        zeile("4) Lueckensensor", bold=True),
        zeile("Jetzt GS FF:"),
        {"t": "form", "dots": 0},
        zeile("<<< oben am Etikett?", bold=True),
        zeile(""),
        zeile("Ende."),
        {"t": "feed", "dots": 80},
    ]
    return {"chars": chars, "rotate": False, "blocks": blocks}


# Was der Benutzer ablesen soll, und was die Antwort bedeutet. Steht hier und
# nicht in der Oberflaeche, damit Messung und Deutung nicht auseinanderlaufen.
ANLEITUNG: list[dict] = [
    {
        "nr": 1,
        "titel": "Zeilenabstand",
        "messen": f"Abstand zwischen den Innenkanten der beiden Balken. "
                  f"Soll: {LINE_SOLL_MM:.1f} mm".replace(".", ","),
        "bedeutet": "Stimmt es, rechnet der Drucker ESC 3 in Punkten (203 dpi) "
                    "und das ganze Etikettenlayout geht auf. Ist es groesser, "
                    "ignoriert er ESC 3 und benutzt seinen eigenen Abstand - "
                    "dann wandern die Etiketten weiter und der Zuschnitt muss "
                    "auf den gemessenen Wert umgerechnet werden.",
    },
    {
        "nr": 2,
        "titel": "Rasterquadrat",
        "messen": f"Breite und Hoehe des schwarzen Quadrats. "
                  f"Soll: {BOX_SOLL_MM:.1f} x {BOX_SOLL_MM:.1f} mm".replace(".", ","),
        "bedeutet": "Ist es quadratisch, stimmt die Punktdichte in beiden "
                    "Richtungen und der Weg ueber ein fertig gerechnetes Bild "
                    "ist gangbar - damit liesse sich der QR-Code neben den "
                    "Text setzen statt darueber. Ist es verzerrt, steht hier "
                    "das Verhaeltnis, mit dem gerechnet werden muss.",
    },
    {
        "nr": 3,
        "titel": "Rueckzug",
        "messen": "Stehen dort zwei Balken untereinander oder nur einer?",
        "bedeutet": "Nur einer (oder ein dickerer) heisst: der Drucker kann "
                    "rueckwaerts, ESC j wirkt. Dann laesst sich der Totbereich "
                    "am Etikettenanfang zurueckholen. Zwei getrennte Balken "
                    "heissen: er kann es nicht, 'Rueckzug' bleibt auf 0.",
    },
    {
        "nr": 4,
        "titel": "Lueckensensor",
        "messen": "Faengt die Zeile mit den Pfeilen oben auf einem neuen "
                  "Etikett an?",
        "bedeutet": "Ja heisst: der Drucker findet die Trennluecke selbst. "
                    "Dann 'Etikettenende' auf 'Drucker sucht die Luecke' "
                    "stellen - danach kann sich kein Versatz mehr ueber die "
                    "Rolle aufsummieren. Bleibt die Zeile stehen, wo sie war, "
                    "oder wirft er eine ganze Seite aus, kennt er GS FF nicht.",
    },
    {
        "nr": 5,
        "titel": "Totbereich",
        "messen": "Abstand von der Oberkante des Etiketts bis zum ersten "
                  "gedruckten Punkt.",
        "bedeutet": "Das ist der Streifen, den der Druckkopf nicht erreicht. "
                    "Unter 'Totbereich (mm)' eintragen; das Layout wird dann "
                    "entsprechend gekuerzt.",
    },
]
