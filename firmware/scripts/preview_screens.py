"""Nachbau der Geraetedarstellung, um die Bildschirme ansehen zu koennen.

Zeichnet mit denselben Schriften (den echten GFX-Bitmaps aus include/fonts)
und demselben Raster wie src/ui/Screen.cpp. Kein Ersatz fuer das Geraet, aber
gut genug, um ueberlaufende Beschriftungen, uebereinanderliegende Zeilen und
gequetschte Kacheln zu sehen - ohne dafuer zwanzig Mal zu flashen. Genau so
sind der Untertitel quer durch die Kennzahlenkarten, die ineinander laufenden
Listenzeilen und die aus der Karte laufende Statuszeile aufgefallen.

**Achtung:** das hier ist eine Zweitschrift von Screen.cpp und wandert nicht
von selbst mit. Wer dort das Raster aendert, muss es hier nachziehen - sonst
begutachtet man eine Darstellung, die es nicht gibt. Die Ausgabe meldet
zusaetzlich jede Beschriftung, die breiter ist als ihr Platz.

Aufruf: die Funktion `render(screen_dict)` mit einer Bildschirmnachricht des
Servers fuettern (device/protocol.screen(...)) und `.img` speichern.
"""
from __future__ import annotations

import re
import pathlib
from PIL import Image, ImageDraw

FONTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "include" / "fonts"

W, H = 480, 320
STATUS_H, TITLE_H = 26, 40
def body_top(screen):
    return STATUS_H + (50 if screen.get("subtitle") else 34)


def rgb565(v):
    r = ((v >> 11) & 0x1F) << 3
    g = ((v >> 5) & 0x3F) << 2
    b = (v & 0x1F) << 3
    return (r | (r >> 5), g | (g >> 6), b | (b >> 5))


C_BG = rgb565(0x0862)
C_SURFACE = rgb565(0x10A3)
C_SURFACE2 = rgb565(0x1905)
C_BORDER = rgb565(0x2967)
C_TEXT = rgb565(0xEF9E)
C_MUTED = rgb565(0x7C32)
C_PRIMARY = rgb565(0x4CFF)
C_OK = rgb565(0x2D89)
C_WARN = rgb565(0xCC83)
C_DANGER = rgb565(0xF228)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


class GfxFont:
    def __init__(self, path: pathlib.Path):
        text = path.read_text()
        self.bitmaps = bytes(
            int(x, 16)
            for x in re.findall(r"0x([0-9A-Fa-f]{2})", text.split("Bitmaps[]")[1].split("};")[0])
        )
        rows = re.findall(
            r"\{\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\s*\}",
            text.split("Glyphs[]")[1].split("};")[0],
        )
        self.glyphs = [tuple(int(v) for v in r) for r in rows]
        tail = text.split("Glyphs,")[1]
        first, last, yadv = re.findall(r"(0x[0-9A-Fa-f]+|\d+)", tail)[:3]
        self.first, self.last = int(first, 16), int(last, 16)
        self.yadvance = int(yadv)
        self.ascent = max((-g[5]) + 0 for g in self.glyphs) if self.glyphs else 0
        self.ascent = max(g[2] + g[5] + (-g[5]) - g[2] for g in self.glyphs) if False else max(
            -g[5] for g in self.glyphs
        )

    def glyph(self, ch):
        code = ord(ch)
        if code < self.first or code > self.last:
            code = ord("?")
        return self.glyphs[code - self.first]

    def width(self, text):
        return sum(self.glyph(c)[3] for c in text)

    def draw(self, img, x, y_top, text, color):
        """y_top ist die Oberkante der Zeile, wie bei TFT_eSPI mit TL_DATUM."""
        px = img.load()
        cx = x
        baseline = y_top + self.ascent
        for ch in text:
            off, gw, gh, xadv, dx, dy = self.glyph(ch)
            bit = 0
            for row in range(gh):
                for col in range(gw):
                    byte = self.bitmaps[off + (bit >> 3)]
                    if byte & (0x80 >> (bit & 7)):
                        ix, iy = cx + dx + col, baseline + dy + row
                        if 0 <= ix < W and 0 <= iy < H:
                            px[ix, iy] = color
                    bit += 1
            cx += xadv
        return cx


FONTS = {
    1: GfxFont(FONTS_DIR / "UiSans12.h"),
    2: GfxFont(FONTS_DIR / "UiSans16.h"),
    4: GfxFont(FONTS_DIR / "UiSans21.h"),
    6: GfxFont(FONTS_DIR / "UiSansBold38.h"),
}


def font_for(n):
    if n >= 6:
        return FONTS[6]
    if n >= 4:
        return FONTS[4]
    if n >= 2:
        return FONTS[2]
    return FONTS[1]


class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (W, H), C_BG)
        self.d = ImageDraw.Draw(self.img)
        self.font = 2
        self.overflow = []
        self.body_y = 66

    def set_font(self, n):
        self.font = n

    def text_width(self, s):
        return font_for(self.font).width(s)

    def rect(self, x, y, w, h, color):
        self.d.rectangle([x, y, x + w - 1, y + h - 1], fill=color)

    def rrect(self, x, y, w, h, r, color, outline=None):
        self.d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=r,
                                 fill=color, outline=outline)

    def circle(self, cx, cy, r, color):
        self.d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    def string(self, s, x, y, color, datum="TL", limit=None, tag=""):
        f = font_for(self.font)
        w = f.width(s)
        if limit is not None and w > limit:
            self.overflow.append(f"{tag}: {s!r} braucht {w}px, Platz {limit}px")
        if datum == "MC":
            x -= w // 2
            y -= f.ascent // 2 + 1
        elif datum == "MR":
            x -= w
        f.draw(self.img, x, y, s, color)
        return w


def parse_color(hex_str, fallback):
    if not hex_str or not hex_str.startswith("#") or len(hex_str) < 7:
        return fallback
    v = int(hex_str[1:7], 16)
    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


def text_on(bg):
    lum = (299 * bg[0] + 587 * bg[1] + 114 * bg[2]) // 1000
    return C_BG if lum > 110 else C_TEXT


def fit_text(c, text, max_w, font):
    for step in (6, 4, 2, 1):
        if step > font:
            continue
        c.set_font(step)
        if c.text_width(text) <= max_w:
            return text
    out = text
    while len(out) > 1 and c.text_width(out + "~") > max_w:
        out = out[:-1]
    return out + "~"


def grid_font(c, items, max_w):
    for step in (4, 2, 1):
        c.set_font(step)
        if all(c.text_width(i.get("label", "")) <= max_w for i in items):
            return step
    return 1


def tile(c, x, y, w, h, label, sub, color, font=4):
    c.rrect(x, y, w, h, 8, color, C_BORDER)
    fg = text_on(color)
    max_w = w - 12
    s = fit_text(c, label, max_w, font)
    c.string(s, x + w // 2, y + h // 2 - (0 if not sub else 10), fg, "MC", max_w, "Kachel")
    if sub:
        s2 = fit_text(c, sub, max_w, 2)
        c.string(s2, x + w // 2, y + h // 2 + 14, fg, "MC", max_w, "Kachel-Unterzeile")


def button(c, x, y, w, h, label, bg):
    c.rrect(x, y, w, h, 6, bg, C_BORDER)
    s = fit_text(c, label, w - 8, 2)
    c.string(s, x + w // 2, y + h // 2, text_on(bg), "MC", w - 8, "Knopf")


def draw_status(c, screen):
    c.rect(0, 0, W, STATUS_H, C_SURFACE)
    c.set_font(2)
    st = screen.get("status", {})
    counts = f"{st.get('total',0)} im Bestand"
    c.string(counts, 10, 5, C_MUTED)
    if st.get("expiring", 0) > 0:
        c.string(f"{st['expiring']} laufen ab", 10 + c.text_width(counts) + 12, 5, C_WARN)
    loc = st.get("location") or "kein Ort"
    pill_h, pill_w = STATUS_H - 6, 150
    pill_x, pill_y = W - 84 - pill_w, 3
    c.rrect(pill_x, pill_y, pill_w, pill_h, pill_h // 2, C_PRIMARY)
    c.string(fit_text(c, loc, pill_w - 16, 2), pill_x + pill_w // 2,
             pill_y + pill_h // 2 + 1, WHITE, "MC", pill_w - 12, "Ortspille")
    c.set_font(2)
    bat = st.get("battery", -1)
    if bat >= 0:
        c.string(f"{bat}%", W - 74, 5, C_DANGER if bat < 10 else C_MUTED)
    c.circle(W - 22, STATUS_H // 2, 5, C_OK if st.get("scanner") else C_DANGER)


def draw_title(c, screen):
    y = STATUS_H
    c.set_font(4)
    c.string(fit_text(c, screen.get("title", ""), W - 20, 4), 10, y + 3, C_TEXT,
             limit=W - 20, tag="Titel")
    sub = screen.get("subtitle", "")
    if sub:
        c.set_font(2)
        c.string(fit_text(c, sub, W - 20, 2), 10, y + 29, C_MUTED, limit=W - 20,
                 tag="Untertitel")


def draw_tiles(c, screen):
    items = screen.get("items", [])
    if not items:
        return
    cols = 2 if len(items) <= 4 else 3
    rows = (len(items) + cols - 1) // cols
    gap = 8
    w = (W - gap * (cols + 1)) // cols
    h = min(((H - c.body_y) - gap * (rows + 1)) // rows, 92)
    font = grid_font(c, items, w - 12)
    for i, it in enumerate(items):
        r, col = divmod(i, cols)
        tile(c, gap + col * (w + gap), c.body_y + gap + r * (h + gap), w, h,
             it.get("label", ""), it.get("sub", ""),
             parse_color(it.get("color"), C_PRIMARY), font)


def draw_list(c, screen):
    items = screen.get("items", [])
    total = len(items)
    row_h = 44
    list_y = c.body_y
    controls = (screen.get("meta") or {}).get("controls") or []
    if controls:
        n = len(controls)
        w = (W - 8 * (n + 1)) // n
        for i, ctl in enumerate(controls):
            button(c, 8 + i * (w + 8), list_y, w, 34, ctl.get("label", ""), C_SURFACE2)
        list_y += 40
    list_h = H - list_y
    page = list_h // row_h
    for i in range(min(page, total)):
        it = items[i]
        y = list_y + i * row_h
        if it.get("header"):
            c.set_font(2)
            c.string(it.get("label", ""), 10, y + row_h // 2 - 8, C_MUTED)
            continue
        color = parse_color(it.get("color"), C_PRIMARY)
        bar = 34 if total > page else 0
        c.rrect(8, y + 2, W - 16 - bar, row_h - 6, 6, C_SURFACE)
        c.rrect(8, y + 2, 5, row_h - 6, 3, color)
        text_w = W - 16 - bar - 22
        sub = it.get("sub")
        c.string(fit_text(c, it.get("label", ""), text_w, 4), 22, y + (3 if sub else 9),
                 C_TEXT, limit=text_w, tag="Listenzeile")
        if sub:
            c.string(fit_text(c, sub, text_w, 1), 22, y + 26, C_MUTED,
                     limit=text_w, tag="Listen-Unterzeile")
    if total > page:
        button(c, W - 32, list_y, 26, list_h // 2 - 3, "^", C_SURFACE)
        button(c, W - 32, list_y + list_h // 2 + 3, 26, list_h // 2 - 3, "v", C_SURFACE)


def draw_home(c, screen):
    y = c.body_y
    meta = screen.get("meta") or {}
    stats = meta.get("stats") or []
    if stats:
        gap, h = 4, 64
        w = (W - gap * (len(stats) + 1)) // len(stats)
        for i, s in enumerate(stats):
            color = parse_color(s.get("color"), C_PRIMARY)
            x = gap + i * (w + gap)
            c.rrect(x, y, w, h, 10, C_SURFACE, color)
            c.rect(x + 1, y + 1, 4, h - 2, color)
            c.set_font(4)
            c.string(str(s.get("value", 0)), x + 10, y + 8, color, limit=w - 20, tag="Kennzahl")
            c.set_font(2)
            c.string(s.get("label", ""), x + 10, y + 38, C_MUTED, limit=w - 16,
                     tag="Kennzahl-Beschriftung")
        y += h + 8
    wifi = meta.get("wifi", True)
    ble = meta.get("ble", False)
    c.set_font(2)

    def pill(px, label, bg):
        tw = c.text_width(label) + 16
        c.rrect(px, y, tw, 22, 11, bg)
        c.string(label, px + tw // 2, y + 11, C_TEXT, "MC")
        return px + tw + 6

    after = pill(4, "WLAN OK" if wifi else "WLAN FEHLT", C_OK if wifi else C_DANGER)
    pill(after, "BLE OK" if ble else "BLE ---", C_OK if ble else C_SURFACE2)
    button(c, W - 120, y - 2, 116, 26, "Neue Rolle", C_SURFACE2)
    y += 30
    items = screen.get("items", [])
    if not items:
        return
    cols, gap = 2, 4
    rows = (len(items) + cols - 1) // cols
    tw = (W - gap * (cols + 1)) // cols
    th = max(40, (c.body_y + (H - c.body_y) - y - gap * (rows + 1)) // rows)
    font = grid_font(c, items, tw - 12)
    for i, it in enumerate(items):
        r, col = divmod(i, cols)
        tile(c, gap + col * (tw + gap), y + gap + r * (th + gap), tw, th,
             it.get("label", ""), it.get("sub", ""),
             parse_color(it.get("color"), C_PRIMARY), font)


def draw_cards(c, screen):
    cards = (screen.get("meta") or {}).get("cards") or []
    if not cards:
        return
    cols, gap = 2, 8
    rows = (len(cards) + cols - 1) // cols
    w = (W - gap * (cols + 1)) // cols
    h = ((H - c.body_y) - gap * (rows + 1)) // rows
    for i, card in enumerate(cards):
        r, col = divmod(i, cols)
        x = gap + col * (w + gap)
        y = c.body_y + gap + r * (h + gap)
        tc = parse_color(card.get("title_color"), C_PRIMARY)
        c.d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=8, outline=tc)
        c.set_font(2)
        c.string(fit_text(c, card.get("title", ""), w - 20, 2), x + 10, y + 6, tc,
                 limit=w - 20, tag="Kartentitel")
        ly = y + 26
        if card.get("status"):
            c.set_font(4)
            c.string(card["status"], x + 10, ly, parse_color(card.get("status_color"), C_TEXT),
                     limit=w - 20, tag="Kartenstatus")
            ly += 26
        btn = card.get("button")
        bottom = (y + h - 6) if not btn else (y + h - 40)
        c.set_font(2)
        for line in card.get("lines", []):
            if ly + 13 > bottom:
                c.overflow.append(f"Karte {card.get('title')!r}: Zeile {line!r} faellt weg")
                break
            c.string(fit_text(c, line, w - 20, 2), x + 10, ly, C_MUTED,
                     limit=w - 20, tag="Kartenzeile")
            c.set_font(2)
            ly += 14
        if btn:
            button(c, x + 8, y + h - 38, w - 16, 32, btn.get("label", ""),
                   parse_color(btn.get("color"), C_SURFACE))


def draw_datepad(c, screen, digits=""):
    DIV_X = 240
    c.rect(DIV_X, c.body_y, 1, H - c.body_y, C_BORDER)
    ly = c.body_y + 8
    for i, line in enumerate(screen.get("lines", [])[:2]):
        c.set_font(2)
        c.string(fit_text(c, line, DIV_X - 20, 2), 10, ly,
                 C_TEXT if i == 0 else C_MUTED, limit=DIV_X - 20, tag="MHD-Zeile")
        ly += 20
    c.rect(10, c.body_y + 52, DIV_X - 20, 1, C_BORDER)
    c.set_font(2)
    c.string("MHD Eingabe:", 10, c.body_y + 58, C_MUTED)
    bx, by, bw, bh = 10, c.body_y + 76, DIV_X - 20, 76
    c.rrect(bx, by, bw, bh, 10, C_SURFACE, C_PRIMARY)
    ph = "TTMMJJ"
    shown = ""
    for i in range(6):
        shown += digits[i] if i < len(digits) else ph[i]
        if i in (1, 3):
            shown += "."
    c.string(fit_text(c, shown, bw - 12, 6), bx + bw // 2, by + bh // 2, C_PRIMARY,
             "MC", bw - 12, "Datum")
    items = screen.get("items", [])
    if items:
        bh2, gap = 40, 6
        y2 = H - 6 - len(items) * bh2 - (len(items) - 1) * gap
        for it in items:
            button(c, 10, y2, DIV_X - 20, bh2, it.get("label", ""),
                   parse_color(it.get("color"), C_SURFACE2))
            y2 += bh2 + gap
    npx = DIV_X + 1
    btn_w = (W - npx) // 3
    btn_h = (H - c.body_y) // 4
    for i in range(9):
        x = npx + (i % 3) * btn_w
        y = c.body_y + (i // 3) * btn_h
        c.rect(x, y, btn_w, btn_h, C_SURFACE2)
        c.rect(x, y, btn_w, 1, C_BORDER)
        c.rect(x, y, 1, btn_h, C_BORDER)
        c.set_font(6)
        c.string(str(i + 1), x + btn_w // 2, y + btn_h // 2, C_TEXT, "MC")
    ly2 = c.body_y + 3 * btn_h
    for x, w, label, bg in ((npx, btn_w * 2, "0", C_SURFACE2),
                            (npx + btn_w * 2, btn_w, "<-", C_DANGER)):
        c.rect(x, ly2, w, btn_h, bg)
        c.rect(x, ly2, w, 1, C_BORDER)
        c.set_font(6)
        c.string(label, x + w // 2, ly2 + btn_h // 2, C_TEXT, "MC")


def draw_message(c, screen):
    lines = screen.get("lines", [])
    y = c.body_y + 20
    for i, line in enumerate(lines):
        c.set_font(4 if i == 0 else 2)
        c.string(fit_text(c, line, W - 40, 4 if i == 0 else 2), W // 2, y, C_TEXT, "MC",
                 W - 40, "Meldung")
        y += 30 if i == 0 else 22


def render(screen) -> Canvas:
    c = Canvas()
    c.body_y = body_top(screen)
    draw_status(c, screen)
    draw_title(c, screen)
    kind = screen.get("kind", "message")
    {"tiles": draw_tiles, "list": draw_list, "home": draw_home, "cards": draw_cards,
     "datepad": draw_datepad, "message": draw_message}.get(kind, draw_message)(c, screen)
    return c
