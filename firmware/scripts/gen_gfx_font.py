#!/usr/bin/env python3
"""Erzeugt aus einer TTF-Datei eine Schrift im Adafruit-GFX-Format.

Uebernommen aus dem Vorgaengerprojekt und um den vollen Latin-1-Bereich
erweitert. Die eingebauten Bitmap-Schriften von TFT_eSPI sind grob gerastert;
diese hier stammen aus einer echten Vektorschrift und sehen auf dem 480x320er
Panel deutlich sauberer aus.

Erzeugt wird der Bereich 0x20-0xFF, also ASCII plus Latin-1. Damit stimmen
Umlaute und Sonderzeichen - auch in Produktnamen, die von OpenFoodFacts
kommen und die niemand vorher durchsieht.

Das Ergebnis ist mit **beiden** Displaytreibern verwendbar: TFT_eSPI und
Arduino_GFX definieren GFXfont/GFXglyph byteidentisch.

Aufruf:
    gen_gfx_font.py <ttf> <groesse_px> <name> <ausgabe.h>

Die erzeugten Header sind eingecheckt; das Skript muss nur laufen, wenn eine
Groesse dazukommt oder sich die Schriftart aendert.
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw, ImageFont

FIRST, LAST = 0x20, 0xFF

# Nicht darstellbare bzw. nicht belegte Positionen bekommen ein leeres Zeichen,
# damit die Glyphentabelle luecklos bleibt (der Renderer indiziert direkt).
BLANK = set(range(0x7F, 0xA0))


def gen_font(ttf_path: str, size_px: int, font_name: str, output_path: str) -> None:
    font = ImageFont.truetype(ttf_path, size_px)
    ascent, descent = font.getmetrics()

    bitmap_bytes = bytearray()
    glyphs: list[tuple[int, int, int, int, int, int]] = []

    for code in range(FIRST, LAST + 1):
        char = chr(code)
        offset = len(bitmap_bytes)

        if code in BLANK:
            glyphs.append((offset, 0, 0, 0, 0, 0))
            continue

        advance = int(font.getlength(char) + 0.5)
        box = font.getbbox(char)
        if box is None or box[2] <= box[0] or box[3] <= box[1]:
            # Leerzeichen und Aehnliches: keine Pixel, aber Vorschub.
            glyphs.append((offset, 0, 0, advance, 0, 0))
            continue

        x0, y0, x1, y1 = box
        width, height = x1 - x0, y1 - y0

        image = Image.new("L", (width, height), 0)
        ImageDraw.Draw(image).text((-x0, -y0), char, font=font, fill=255)

        # 1 Bit je Pixel, zeilenweise, links beginnend - so erwartet es GFX.
        bits, current = [], 0
        for index, value in enumerate(image.getdata()):
            current = (current << 1) | (1 if value >= 128 else 0)
            if index % 8 == 7:
                bits.append(current)
                current = 0
        if len(image.getdata()) % 8:
            bits.append(current << (8 - len(image.getdata()) % 8))
        bitmap_bytes.extend(bits)

        # yOffset ist der Abstand von der Grundlinie zur Oberkante, negativ
        # nach oben - genau so, wie GFX beim Zeichnen rechnet.
        glyphs.append((offset, width, height, advance, x0, y0 - ascent))

    lines = [
        f"// Erzeugt von scripts/gen_gfx_font.py - nicht von Hand aendern.",
        f"// Schrift: {ttf_path.split('/')[-1]}, {size_px} px, Bereich 0x20-0xFF.",
        "// Nach dem Displaytreiber einbinden, der GFXfont/GFXglyph definiert.",
        "#pragma once",
        "#include <pgmspace.h>",
        "",
        f"static const uint8_t {font_name}Bitmaps[] PROGMEM = {{",
    ]
    for start in range(0, len(bitmap_bytes), 16):
        chunk = ", ".join(f"0x{b:02X}" for b in bitmap_bytes[start : start + 16])
        lines.append(f"  {chunk},")
    lines += ["};", "", f"static const GFXglyph {font_name}Glyphs[] PROGMEM = {{"]
    for code, (off, w, h, adv, xo, yo) in zip(range(FIRST, LAST + 1), glyphs):
        lines.append(
            f"  {{ {off:6d}, {w:3d}, {h:3d}, {adv:3d}, {xo:4d}, {yo:4d} }},"
            f"  // 0x{code:02X}"
        )
    lines += [
        "};",
        "",
        f"static const GFXfont {font_name} PROGMEM = {{",
        f"  (uint8_t *){font_name}Bitmaps,",
        f"  (GFXglyph *){font_name}Glyphs,",
        f"  0x{FIRST:02X}, 0x{LAST:02X}, {ascent + descent}",
        "};",
        "",
    ]

    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    print(f"{output_path}: {len(bitmap_bytes)} Byte Bitmaps, {len(glyphs)} Zeichen")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)
    gen_font(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
