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
#
# Es kommt dreimal, einmal je Rasterbefehl (A, B, C). Welcher davon auf einem
# namenlosen Drucker masshaltig druckt, steht in keinem Datenblatt; gedruckt
# nebeneinander sieht man es sofort. Der passende gehoert dann unter
# "Bildmodus" - von ihm haengt das Hochformat ab, das als Bild gedruckt wird.
# 72 statt 80 Punkte: `ESC * 33` druckt in Baendern von 24 Zeilen, und ein
# angebrochenes Band waere schon wieder eine Verzerrung, die der Drucker gar
# nicht hat. Nicht groesser, weil der Streifen auch auf einer Firmware drucken
# soll, die noch keine Bildstreifen kennt - dort muss er in einem Stueck in
# den Sendepuffer und die Nachricht unter 8 KB bleiben.
BOX_DOTS = 72
BOX_SOLL_MM = BOX_DOTS / DOTS_PER_MM
BOX_MODI = (("A", "GS v 0", "gsv0"), ("B", "ESC * 33", "esc33"), ("C", "ESC * 0", "esc0"))

# Rueckzug fuer den Test. 40 Punkte = 5 mm, weit genug, um mit blossem Auge
# zu sehen, ob er stattgefunden hat.
BACKFEED_DOTS = 40
# Vorschub vor dem Rueckzug, und wo der zweite Balken dann landet.
BACK_VOR_DOTS = 64
BACK_SOLL_MM = (BACK_VOR_DOTS + 8 - BACKFEED_DOTS) / DOTS_PER_MM


def _raster(width: int, height: int, gefuellt: bool = True, modus: str = "") -> dict:
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
    block = {
        "t": "raster",
        "w": width,
        "h": height,
        "d": base64.b64encode(bytes(daten)).decode("ascii"),
    }
    # Ohne Angabe druckt die Firmware wie bisher mit `ESC * 0` - auch eine
    # aeltere, die `mode` nicht kennt.
    if modus:
        block["mode"] = modus
    return block


def breite_dots(line_mm: float) -> int:
    return min(MARK_WIDTH_DOTS, MAX_WIDTH_DOTS, int(line_mm * DOTS_PER_MM))


def _zeile(text: str, bold: bool = False) -> dict:
    return {"t": "text", "v": text, "align": 0, "bold": bold, "h": LINE_HEIGHT}


def _mm(wert: float) -> str:
    return f"{wert:.1f}".replace(".", ",")


def zeilen_test(stack_mm: float, dead_mm: float = 0.0) -> int:
    """Wie viele Testzeilen auf ein Etikett passen - hoechstens LINE_COUNT.

    Ueberschrift, zwei Balken und die Beschriftung muessen mit auf dasselbe
    Etikett: eine Strecke, die ueber die Luecke zum naechsten laeuft, kann
    man nicht messen.
    """
    nutzbar = _nutzbar(stack_mm, dead_mm)
    rest = nutzbar - 2 * LINE_HEIGHT - 2 * 8
    return max(2, min(LINE_COUNT, rest // LINE_HEIGHT))


def _nutzbar(stack_mm: float, dead_mm: float) -> int:
    # 16 Punkte Luft wie bei den Etiketten (labels.SAFETY_DOTS): lieber etwas
    # frei lassen, als die letzte Zeile auf die Kante zu setzen.
    return int(stack_mm * DOTS_PER_MM) - int(dead_mm * DOTS_PER_MM) - 16


def build(stack_mm: float = 30.0, line_mm: float = 48.0, chars: int = 32,
          dead_mm: float = 0.0) -> dict:
    """Den Teststreifen als Druckauftrag bauen - ein Test je Etikett.

    Frueher lief der Streifen am Stueck ueber die Rolle. Auf gestanzten
    Etiketten fiel dabei die Messstrecke ueber die Luecke, und ganze Punkte
    landeten auf dem Traegerpapier dazwischen. Jetzt ist jedes Stueck
    hoechstens so hoch wie die bedruckbare Flaeche und wird auf genau eine
    Teilung aufgefuellt - wie ein Etikett.
    """
    pitch = int(stack_mm * DOTS_PER_MM)
    width = breite_dots(line_mm)
    marke = _raster(width, 8)
    n = zeilen_test(stack_mm, dead_mm)
    soll_mm = n * LINE_HEIGHT / DOTS_PER_MM

    etiketten: list[list[dict]] = [
        # --- 1: gilt ESC 3 in Punkten? ----------------------------------
        [_zeile("1) Zeilenabstand", bold=True), dict(marke),
         *[_zeile("|") for _ in range(n)],
         dict(marke), _zeile(f"Innenkanten: {_mm(soll_mm)} mm")],
    ]
    # --- 2: stimmt die Rastergeometrie? ------------------------------
    # Jedes Quadrat auf ein eigenes Etikett, mit seinem Buchstaben darueber.
    for buchstabe, name, modus in BOX_MODI:
        etiketten.append([
            _zeile(f"2{buchstabe.lower()}) Bildmodus {buchstabe}: {name}", bold=True),
            _zeile(f"Soll {_mm(BOX_SOLL_MM)} x {_mm(BOX_SOLL_MM)} mm"),
            _raster(BOX_DOTS, BOX_DOTS, modus=modus),
        ])
    # --- 3: kann der Drucker rueckwaerts? ----------------------------
    # Erster Balken, 8 mm vor, 5 mm zurueck, zweiter (halb so breiter)
    # Balken. Rechnet der Drucker den Rueckzug, liegen die Oberkanten 4 mm
    # auseinander, sonst 9 mm. Frueher kam der zweite Balken direkt nach dem
    # Rueckzug: ohne Rueckzug lagen die Balken dann Kante an Kante und sahen
    # aus wie *ein* dicker - genau das Bild, das "geht" bedeuten sollte.
    etiketten.append([
        _zeile("3) Rückzug", bold=True), dict(marke),
        {"t": "feed", "dots": BACK_VOR_DOTS},
        {"t": "back", "dots": BACKFEED_DOTS}, _raster(width // 2, 8),
        {"t": "feed", "dots": BACKFEED_DOTS},
        _zeile(f"Oberkanten: {_mm(BACK_SOLL_MM)} mm = geht"),
        _zeile(f"{_mm((BACK_VOR_DOTS + 8) / DOTS_PER_MM)} mm = geht nicht"),
    ])

    blocks: list[dict] = []
    for inhalt in etiketten:
        hoehe = sum(_hoehe(b) for b in inhalt)
        blocks += inhalt
        # "teilung" ist nur fuer die Tests: hier endet ein Etikett.
        blocks.append({"t": "feed", "dots": max(0, pitch - hoehe), "teilung": True})

    # --- 4: hat er einen Lueckensensor? ------------------------------
    # Steht am Ende, weil GS FF auf einem Drucker ohne Sensor eine ganze
    # Seite auswerfen kann - dann ist wenigstens der Rest schon gedruckt.
    blocks += [
        _zeile("4) Lückensensor", bold=True),
        _zeile("Jetzt GS FF:"),
        {"t": "feed", "dots": 0, "form": True},
        _zeile("<<< oben am Etikett?", bold=True),
        _zeile("Ende."),
        {"t": "feed", "dots": max(0, pitch - 2 * LINE_HEIGHT)},
    ]
    return {"chars": chars, "rotate": False, "blocks": blocks, "soll_mm": soll_mm}


def _hoehe(block: dict) -> int:
    # Dieselbe Rechnung wie labels.block_dots, ohne den Import im Kreis.
    if block["t"] == "back":
        return -int(block["dots"])
    if block["t"] == "raster":
        return -(-int(block["h"]) // 8) * 8
    if block["t"] == "feed":
        return int(block.get("dots", 0))
    return int(block.get("h", LINE_HEIGHT))


# Was der Benutzer ablesen soll, und was die Antwort bedeutet. Steht hier und
# nicht in der Oberflaeche, damit Messung und Deutung nicht auseinanderlaufen.
def anleitung(soll_mm: float = LINE_SOLL_MM) -> list[dict]:
    """Die Anleitung mit dem Sollwert, der auf dem Streifen steht."""
    schritte = [dict(s) for s in ANLEITUNG]
    schritte[0]["messen"] = ("Abstand zwischen den Innenkanten der beiden Balken. "
                             f"Soll: {_mm(soll_mm)} mm")
    return schritte


ANLEITUNG: list[dict] = [
    {
        "nr": 1,
        "titel": "Zeilenabstand",
        "messen": f"Abstand zwischen den Innenkanten der beiden Balken. "
                  f"Soll: {LINE_SOLL_MM:.1f} mm".replace(".", ","),
        "bedeutet": "Stimmt es, rechnet der Drucker ESC 3 in Punkten (203 dpi) "
                    "und das ganze Etikettenlayout geht auf. Ist es größer, "
                    "ignoriert er ESC 3 und benutzt seinen eigenen Abstand – "
                    "dann wandern die Etiketten weiter und der Zuschnitt muss "
                    "auf den gemessenen Wert umgerechnet werden.",
    },
    {
        "nr": 2,
        "titel": "Rasterquadrat",
        "messen": f"Breite und Höhe der schwarzen Quadrate A, B und C (je ein Etikett). "
                  f"Soll: {BOX_SOLL_MM:.1f} x {BOX_SOLL_MM:.1f} mm".replace(".", ","),
        "bedeutet": "Das Quadrat, das genau so groß und wirklich quadratisch "
                    "ist, zeigt den Bildbefehl, den dein Drucker richtig "
                    "versteht. Diesen Buchstaben unter 'Bildmodus' wählen – "
                    "davon hängt das Hochformat ab, das als Bild gedruckt "
                    "wird. Fehlt ein Quadrat ganz, kennt der Drucker diesen "
                    "Befehl nicht. Sind alle verzerrt, kann er kein "
                    "Hochformat.",
    },
    {
        "nr": 3,
        "titel": "Rückzug",
        "messen": "Abstand zwischen den Oberkanten des langen und des kurzen "
                  f"Balkens. {_mm(BACK_SOLL_MM)} mm: der Drucker fährt "
                  f"zurück. {_mm((BACK_VOR_DOTS + 8) / DOTS_PER_MM)} mm: er kann es nicht.",
        "bedeutet": "Kann er zurück, lässt sich der Totbereich am "
                    "Etikettenanfang zurückholen: unter 'Rückzug (Punkte)' "
                    "den Totbereich in Punkten eintragen (mm × 8). Liegt der "
                    "Wert dazwischen, fährt er nur einen Teil zurück – "
                    "Rückzug dann auf 0 lassen. Kann er es nicht, bleibt "
                    "'Rückzug' auf 0.",
    },
    {
        "nr": 4,
        "titel": "Lückensensor",
        "messen": "Fängt die Zeile mit den Pfeilen oben auf einem neuen "
                  "Etikett an?",
        "bedeutet": "Ja heißt: der Drucker findet die Trennlücke selbst. "
                    "Dann 'Etikettenende' auf 'Drucker sucht die Lücke' "
                    "stellen – danach kann sich kein Versatz mehr über die "
                    "Rolle aufsummieren. Bleibt die Zeile stehen, wo sie war, "
                    "oder wirft er eine ganze Seite aus, kennt er GS FF nicht.",
    },
    {
        "nr": 5,
        "titel": "Totbereich",
        "messen": "Abstand von der Oberkante eines Etiketts bis zu seiner "
                  "ersten Zeile.",
        "bedeutet": "Das ist der Streifen, den der Druckkopf nicht erreicht. "
                    "Unter 'Totbereich (mm)' eintragen; das Layout wird dann "
                    "entsprechend gekürzt.",
    },
]
