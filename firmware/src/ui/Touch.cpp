#include "Touch.h"

#include <Wire.h>

#include "config.h"

Touch touch;

bool Touch::begin() {
    Wire.begin(TOUCH_SDA, TOUCH_SCL, I2C_FREQ);
    Wire.beginTransmission(TOUCH_ADDR);
    _ok = Wire.endTransmission() == 0;
    if (!_ok) log_e("FT6336 nicht gefunden (0x%02X)", TOUCH_ADDR);
    return _ok;
}

bool Touch::read(int16_t &x, int16_t &y) {
    Wire.beginTransmission(TOUCH_ADDR);
    Wire.write(0x02);                       // Registeranfang: Anzahl Beruehrungen
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom(TOUCH_ADDR, (uint8_t)5) != 5) return false;

    const uint8_t points = Wire.read() & 0x0F;
    const uint8_t xh = Wire.read(), xl = Wire.read();
    const uint8_t yh = Wire.read(), yl = Wire.read();
    if (points == 0 || points > 2) return false;

    const int16_t px = ((xh & 0x0F) << 8) | xl;   // Rohwerte im Hochformat
    const int16_t py = ((yh & 0x0F) << 8) | yl;

    // Panel ist hochkant verbaut, gezeichnet wird quer (Rotation 1):
    // x_ui = y_raw, y_ui = Panelbreite - x_raw.
    x = constrain(py, 0, UI_WIDTH - 1);
    y = constrain(PANEL_WIDTH - px, 0, UI_HEIGHT - 1);
    return true;
}

bool Touch::pressed(int16_t &x, int16_t &y) {
    if (!_ok) return false;

    const uint32_t now = millis();
    if (now - _lastPoll < TOUCH_POLL_MS) return false;
    _lastPoll = now;

    int16_t px = 0, py = 0;
    const bool contact = read(px, py);

    if (!contact) {
        _down = false;
        return false;
    }
    if (_down) return false;             // Finger liegt noch auf: kein neues Ereignis
    if (now - _lastEvent < 220) return false;  // Entprellen gegen Doppelausloesung

    _down = true;
    _lastEvent = now;
    x = px;
    y = py;
    return true;
}
