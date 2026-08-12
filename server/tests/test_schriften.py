"""Die Geraeteschriften muessen auf einer Grundlinie stehen.

Kein Servertest im engeren Sinn - die Schriftdateien liegen in der Firmware.
Sie stehen hier trotzdem, weil es die einzige Stelle ist, an der bei jedem
Lauf etwas geprueft wird, und weil der Fehler sonst erst auf dem Geraet
auffaellt: FreeType staucht beim Hinting Zeichen mit Aufsatz, damit der
Aufsatz unter die Oberlaenge passt. "ä" und "ö" sassen dadurch eine Pixelzeile
ueber der Grundlinie, "a" und "o" darauf - in einem deutschen Text tanzt dann
jedes zweite Wort.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

FONTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "firmware" / "include" / "fonts"
SCRIPTS_DIR = FONTS_DIR.parents[1] / "scripts"

# Buchstaben, deren Unterkante auf der Grundlinie sitzt - ohne Unterlaengen
# (g, j, p, q, y) und ohne Zeichen, die bewusst darunter reichen.
AUF_DER_GRUNDLINIE = "abcdehiklmnorstuvwxzäöüßABCDEHIKLMNORSTUVWXZÄÖÜ0123456789"


def _lade_schrift(name: str):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from preview_screens import GfxFont
    finally:
        sys.path.pop(0)
    return GfxFont(FONTS_DIR / f"{name}.h")


@pytest.mark.parametrize(
    "name", ["UiSans12", "UiSans16", "UiSans21", "UiSansBold38"]
)
def test_grundlinie_ist_einheitlich(name):
    schrift = _lade_schrift(name)

    unterkanten: dict[int, list[str]] = {}
    for zeichen in AUF_DER_GRUNDLINIE:
        _, _, hoehe, _, _, dy = schrift.glyph(zeichen)
        if hoehe == 0:
            continue
        unterkanten.setdefault(dy + hoehe, []).append(zeichen)

    assert len(unterkanten) == 1, (
        f"{name}: die Buchstaben sitzen auf {len(unterkanten)} verschiedenen "
        f"Grundlinien - "
        + ", ".join(f"{k}: {''.join(v)}" for k, v in sorted(unterkanten.items()))
    )


@pytest.mark.parametrize("name", ["UiSans12", "UiSans16", "UiSans21", "UiSansBold38"])
def test_umlaute_stehen_wie_ihre_grundbuchstaben(name):
    """Der Umlaut darf nur oben etwas hinzufuegen, nicht unten verschieben."""
    schrift = _lade_schrift(name)
    for umlaut, grund in (("ä", "a"), ("ö", "o"), ("ü", "u"),
                          ("Ä", "A"), ("Ö", "O"), ("Ü", "U")):
        _, _, h_um, _, _, dy_um = schrift.glyph(umlaut)
        _, _, h_gr, _, _, dy_gr = schrift.glyph(grund)
        assert dy_um + h_um == dy_gr + h_gr, (
            f"{name}: {umlaut!r} sitzt {abs((dy_um + h_um) - (dy_gr + h_gr))} Pixel "
            f"neben {grund!r}"
        )
