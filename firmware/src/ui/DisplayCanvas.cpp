#include "DisplayCanvas.h"

#include "config.h"

bool DisplayCanvas::begin() {
#if defined(BOARD_WAVESHARE_35B)
    _bus = new Arduino_ESP32QSPI(LCD_QSPI_CS, LCD_QSPI_CLK, LCD_QSPI_D0,
                                 LCD_QSPI_D1, LCD_QSPI_D2, LCD_QSPI_D3);
    _panel = new Arduino_AXS15231B(_bus, GFX_NOT_DEFINED, 0, false,
                                   PANEL_WIDTH, PANEL_HEIGHT);
    _canvas = new Arduino_Canvas(PANEL_WIDTH, PANEL_HEIGHT, _panel, 0, 0, 1);
    if (!_bus || !_panel || !_canvas || !_canvas->begin()) {
        log_e("AXS15231B/Arduino_GFX konnte nicht initialisiert werden");
        return false;
    }
    _canvas->fillScreen(0);
    _canvas->flush();
    _created = true;
#else
    _tft.init();
    _tft.setRotation(1);
    _sprite.setColorDepth(16);
    _created = _sprite.createSprite(UI_WIDTH, UI_HEIGHT) != nullptr;
    // Startschrift setzen, damit vor dem ersten setTextFont() nicht die
    // eingebaute Bitmapschrift durchschlaegt.
    if (_created) _sprite.setFreeFont(fontFor(_font));
#endif
    return _created;
}

// Die Bildschirme sprechen weiterhin in den gewohnten Nummern (1/2/4/6/7) der
// eingebauten TFT_eSPI-Schriften. Hier wird daraus eine echte Vektorschrift -
// die eingebauten sind grob gerastert und sehen auf diesem Panel entsprechend
// kantig aus. Die Groessen sind so gewaehlt, dass sie die bisherigen
// Zeilenhoehen moeglichst genau treffen, damit kein Layout verrutscht.
const GFXfont *DisplayCanvas::fontFor(uint8_t font) {
    if (font >= 6) return &UiSansBold38;   // grosse Datums-/Zahlenanzeige
    if (font >= 4) return &UiSans21;   // Titel, Kachelbeschriftung
    if (font >= 2) return &UiSans16;       // Fliesstext
    return &UiSans12;                      // Kleingedrucktes
}

void DisplayCanvas::setTextFont(uint8_t font) {
    _font = font;
#if !defined(BOARD_WAVESHARE_35B)
    _sprite.setFreeFont(fontFor(font));
#endif
}

void DisplayCanvas::setTextColor(uint16_t fg, uint16_t bg) {
#if defined(BOARD_WAVESHARE_35B)
    _fg = fg;
    _bg = bg;
    if (_canvas) _canvas->setTextColor(fg, bg);
#else
    _sprite.setTextColor(fg, bg);
#endif
}

void DisplayCanvas::fillSprite(uint16_t color) {
#if defined(BOARD_WAVESHARE_35B)
    if (_canvas) _canvas->fillScreen(color);
#else
    _sprite.fillSprite(color);
#endif
}

void DisplayCanvas::fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color) {
#if defined(BOARD_WAVESHARE_35B)
    if (_canvas) _canvas->fillRect(x, y, w, h, color);
#else
    _sprite.fillRect(x, y, w, h, color);
#endif
}

void DisplayCanvas::fillRoundRect(int16_t x, int16_t y, int16_t w, int16_t h,
                                  int16_t radius, uint16_t color) {
#if defined(BOARD_WAVESHARE_35B)
    if (_canvas) _canvas->fillRoundRect(x, y, w, h, radius, color);
#else
    _sprite.fillRoundRect(x, y, w, h, radius, color);
#endif
}

void DisplayCanvas::drawRoundRect(int16_t x, int16_t y, int16_t w, int16_t h,
                                  int16_t radius, uint16_t color) {
#if defined(BOARD_WAVESHARE_35B)
    if (_canvas) _canvas->drawRoundRect(x, y, w, h, radius, color);
#else
    _sprite.drawRoundRect(x, y, w, h, radius, color);
#endif
}

void DisplayCanvas::fillCircle(int16_t x, int16_t y, int16_t radius, uint16_t color) {
#if defined(BOARD_WAVESHARE_35B)
    if (_canvas) _canvas->fillCircle(x, y, radius, color);
#else
    _sprite.fillCircle(x, y, radius, color);
#endif
}

#if defined(BOARD_WAVESHARE_35B)
// Arduino_GFX dekodiert UTF-8 nur fuer u8g2-Schriften, nicht fuer die hier
// verwendeten GFX-Schriften - ohne diese Umsetzung wuerde aus einem "ue" nicht
// ein Zeichen, sondern zwei falsche. TFT_eSPI bringt das selbst mit, deshalb
// steht es nur in diesem Zweig.
static String toLatin1(const String &utf8) {
    String out;
    out.reserve(utf8.length());
    for (size_t i = 0; i < utf8.length(); i++) {
        const uint8_t c = utf8[i];
        if (c < 0x80) {
            out += (char)c;
        } else if ((c & 0xE0) == 0xC0 && i + 1 < utf8.length()) {
            const uint16_t cp = ((c & 0x1F) << 6) | (utf8[++i] & 0x3F);
            out += (cp <= 0xFF) ? (char)cp : '?';
        } else if ((c & 0xF0) == 0xE0) {
            i += 2;            // 3-Byte-Zeichen kennt Latin-1 nicht
            out += '?';
        } else {
            out += '?';
        }
    }
    return out;
}
#endif

void DisplayCanvas::drawString(const String &text, int16_t x, int16_t y) {
#if defined(BOARD_WAVESHARE_35B)
    if (!_canvas) return;
    const String out = toLatin1(text);
    _canvas->setFont(fontFor(_font));
    _canvas->setTextSize(1);
    _canvas->setTextColor(_fg, _bg);

    // Bei einer GFX-Schrift ist die Cursorposition die Grundlinie, nicht die
    // linke obere Ecke; bx/by geben den Versatz dorthin an. Ohne diese
    // Umrechnung saesse jede Zeile um die Schrifthoehe zu hoch.
    int16_t bx = 0, by = 0;
    uint16_t bw = 0, bh = 0;
    _canvas->getTextBounds(out, 0, 0, &bx, &by, &bw, &bh);
    if (_datum == MC_DATUM) {
        _canvas->setCursor(x - bx - bw / 2, y - by - bh / 2);
    } else {
        _canvas->setCursor(x - bx, y - by);
    }
    _canvas->print(out);
#else
    _sprite.drawString(text, x, y);
#endif
}

uint16_t DisplayCanvas::textWidth(const String &text) {
#if defined(BOARD_WAVESHARE_35B)
    if (!_canvas) return 0;
    _canvas->setFont(fontFor(_font));
    _canvas->setTextSize(1);
    int16_t bx = 0, by = 0;
    uint16_t bw = 0, bh = 0;
    _canvas->getTextBounds(toLatin1(text), 0, 0, &bx, &by, &bw, &bh);
    return bw;
#else
    return _sprite.textWidth(text);
#endif
}

void DisplayCanvas::pushSprite(int16_t x, int16_t y) {
#if defined(BOARD_WAVESHARE_35B)
    (void)x;
    (void)y;
    if (_canvas) _canvas->flush();
#else
    _sprite.pushSprite(x, y);
#endif
}