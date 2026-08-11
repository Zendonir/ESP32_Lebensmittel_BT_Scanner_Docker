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
#endif
    return _created;
}

void DisplayCanvas::setTextFont(uint8_t font) {
    _font = font;
#if !defined(BOARD_WAVESHARE_35B)
    _sprite.setTextFont(font);
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

void DisplayCanvas::drawString(const String &text, int16_t x, int16_t y) {
#if defined(BOARD_WAVESHARE_35B)
    if (!_canvas) return;
    const uint8_t scale = _font >= 7 ? 4 : _font >= 6 ? 3 : _font >= 4 ? 2 : 1;
    _canvas->setTextSize(scale);
    _canvas->setTextColor(_fg, _bg);
    int16_t bx = 0, by = 0;
    uint16_t bw = 0, bh = 0;
    _canvas->getTextBounds(text, 0, 0, &bx, &by, &bw, &bh);
    if (_datum == MC_DATUM) {
        x -= static_cast<int16_t>(bw / 2);
        y -= static_cast<int16_t>(bh / 2);
    }
    _canvas->setCursor(x, y);
    _canvas->print(text);
#else
    _sprite.drawString(text, x, y);
#endif
}

uint16_t DisplayCanvas::textWidth(const String &text) {
#if defined(BOARD_WAVESHARE_35B)
    if (!_canvas) return 0;
    const uint8_t scale = _font >= 7 ? 4 : _font >= 6 ? 3 : _font >= 4 ? 2 : 1;
    _canvas->setTextSize(scale);
    int16_t bx = 0, by = 0;
    uint16_t bw = 0, bh = 0;
    _canvas->getTextBounds(text, 0, 0, &bx, &by, &bw, &bh);
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