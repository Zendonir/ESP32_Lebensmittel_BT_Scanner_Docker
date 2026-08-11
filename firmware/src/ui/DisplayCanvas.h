#pragma once

#include <Arduino.h>

#if defined(BOARD_WAVESHARE_35B)
#include <Arduino_GFX_Library.h>

#ifndef TL_DATUM
#define TL_DATUM 0
#define MC_DATUM 1
#endif
#ifndef TFT_BLACK
#define TFT_BLACK 0x0000
#define TFT_WHITE 0xFFFF
#endif
#else
#include <TFT_eSPI.h>
#endif

// Kleine Kompatibilitaetsschicht fuer die beiden elektrisch unterschiedlichen
// 3,5-Zoll-Boards. Screen kennt dadurch weder TFT_eSPI noch Arduino_GFX.
class DisplayCanvas {
public:
    bool begin();
    bool created() const { return _created; }
    void setTextDatum(uint8_t datum) { _datum = datum; }
    void setTextFont(uint8_t font);
    void setTextColor(uint16_t fg, uint16_t bg);
    void fillSprite(uint16_t color);
    void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color);
    void fillRoundRect(int16_t x, int16_t y, int16_t w, int16_t h, int16_t radius, uint16_t color);
    void drawRoundRect(int16_t x, int16_t y, int16_t w, int16_t h, int16_t radius, uint16_t color);
    void fillCircle(int16_t x, int16_t y, int16_t radius, uint16_t color);
    void drawString(const String &text, int16_t x, int16_t y);
    void pushSprite(int16_t x, int16_t y);

private:
    bool _created = false;
    uint8_t _datum = TL_DATUM;
    uint8_t _font = 2;

#if defined(BOARD_WAVESHARE_35B)
    Arduino_DataBus *_bus = nullptr;
    Arduino_GFX *_panel = nullptr;
    Arduino_Canvas *_canvas = nullptr;
    uint16_t _fg = 0xFFFF;
    uint16_t _bg = 0;
#else
    TFT_eSPI _tft;
    TFT_eSprite _sprite{&_tft};
#endif
};