#include "Touch.h"

#include <Wire.h>

#include "config.h"

Touch touch;

// Schwellwerte aus dem Vorgaengerprojekt (Display::tick(), display.cpp)
// uebernommen - dort ueber viele Geraete hinweg erprobt.
static constexpr int16_t  SWIPE_MIN_DIST  = 60;   // px Mindestweg fuer einen Swipe
static constexpr int16_t  SWIPE_MAX_OFFAX = 40;   // px max. Abweichung quer zur Richtung
static constexpr int16_t  TAP_MAX_DIST    = 15;   // px max. Bewegung fuer einen Tap
static constexpr int16_t  SCROLL_DEAD_PX  = 8;     // px Totzone, bevor der Drag beginnt

// Der FT6336 meldet zwischendurch ein einzelnes "nicht beruehrt", obwohl der
// Finger noch aufliegt. Ohne Entprellung wird daraus ein vorzeitiges Abheben:
// aus einem Tippen werden zwei Taps (der zweite trifft dann schon den neu
// aufgebauten Bildschirm), und ein Ziehen bricht mittendrin ab. Das
// Vorgaengerprojekt hatte dafuer denselben Zaehler.
static constexpr uint8_t RELEASE_DEBOUNCE_TICKS = 2;

bool Touch::begin() {
    // Wire wurde bereits in Board::begin() gestartet - dort muss der Bus
    // stehen, bevor der Expander den Display-Reset loesen kann.
    Wire.beginTransmission(TOUCH_ADDR);
    _ok = Wire.endTransmission() == 0;
    if (!_ok) {
#if defined(BOARD_WAVESHARE_35B)
        log_e("AXS15231B-Touch nicht gefunden (0x%02X)", TOUCH_ADDR);
#else
        log_e("FT6336 nicht gefunden (0x%02X)", TOUCH_ADDR);
#endif
    }
    return _ok;
}

bool Touch::read(int16_t &x, int16_t &y) {
#if defined(BOARD_WAVESHARE_35B)
    // Protokoll aus dem Waveshare-Treiber esp_lcd_touch_axs15231b. Der
    // Controller erwartet vor jedem 14-Byte-Bericht dieses 11-Byte-Kommando.
    static const uint8_t command[11] = {
        0xB5, 0xAB, 0xA5, 0x5A, 0x00, 0x00, 0x00, 0x0E, 0x00, 0x00, 0x00,
    };
    uint8_t data[14] = {};
    Wire.beginTransmission(TOUCH_ADDR);
    Wire.write(command, sizeof(command));
    if (Wire.endTransmission() != 0) return false;
    if (Wire.requestFrom((uint8_t)TOUCH_ADDR, (uint8_t)sizeof(data)) != sizeof(data)) return false;
    Wire.readBytes(data, sizeof(data));
    if (data[0] == 0xFF || data[1] == 0 || data[1] > 2 || data[3] < 2 || data[5] < 2) {
        return false;
    }
    const int16_t px = ((data[2] & 0x0F) << 8) | data[3];
    const int16_t py = ((data[4] & 0x0F) << 8) | data[5];
    x = constrain(py, 0, UI_WIDTH - 1);
    y = constrain(PANEL_WIDTH - 1 - px, 0, UI_HEIGHT - 1);
    return true;
#else
    Wire.beginTransmission(TOUCH_ADDR);
    Wire.write(0x02);                       // Registeranfang: Anzahl Beruehrungen
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((uint8_t)TOUCH_ADDR, (uint8_t)5) != 5) return false;

    const uint8_t points = Wire.read() & 0x0F;
    const uint8_t xh = Wire.read(), xl = Wire.read();
    const uint8_t yh = Wire.read(), yl = Wire.read();
    if (points == 0 || points > 2) return false;

    const int16_t px = ((xh & 0x0F) << 8) | xl;   // Rohwerte im Hochformat
    const int16_t py = ((yh & 0x0F) << 8) | yl;

    // Panel ist hochkant verbaut, gezeichnet wird quer (Rotation 1):
    // x_ui = y_raw, y_ui = Panelbreite - x_raw.
    x = constrain(py, 0, UI_WIDTH - 1);
    y = constrain(PANEL_WIDTH - 1 - px, 0, UI_HEIGHT - 1);
    return true;
#endif
}

Gesture Touch::poll(bool scrollable) {
    Gesture g;
    if (!_ok) return g;

    if (millis() - _lastPoll < TOUCH_POLL_MS) return g;
    _lastPoll = millis();

    int16_t px = 0, py = 0;
    const bool contact = read(px, py);

    if (contact) {
        _releaseDebounce = 0;

        if (!_down) {
            // Neuer Fingerkontakt - erst beim Loslassen entscheidet sich ueber
            // die zurueckgelegte Strecke, ob es ein Tap oder ein Swipe war.
            _down     = true;
            _dragging = false;
            _startX = _lastX = px;
            _startY = _lastY = py;
            return g;
        }

        const int16_t dx = px - _startX;
        const int16_t dy = py - _startY;

        if (!_dragging && scrollable && abs(dy) > SCROLL_DEAD_PX && abs(dy) > abs(dx)) {
            _dragging = true;
        }
        if (_dragging) {
            g.type    = GestureType::ScrollDrag;
            g.deltaY  = py - _lastY;
        }
        _lastX = px;
        _lastY = py;
        return g;
    }

    if (_down) {
        // Erst nach mehreren aufeinanderfolgenden Leermessungen gilt der Finger
        // als abgehoben - siehe RELEASE_DEBOUNCE_TICKS.
        if (++_releaseDebounce < RELEASE_DEBOUNCE_TICKS) return g;

        _down = false;
        _releaseDebounce = 0;
        if (_dragging) {
            _dragging = false;
            g.type = GestureType::ScrollEnd;
            return g;
        }

        const int16_t dx    = _lastX - _startX;
        const int16_t dy    = _lastY - _startY;
        const int16_t absDx = abs(dx);
        const int16_t absDy = abs(dy);

        // Ein Tippen wird allein ueber die Bewegung erkannt, nicht zusaetzlich
        // ueber die Dauer. Mit der frueheren 250-ms-Schranke fiel ein bewusst
        // laenger gehaltener Druck durch beide Raster und loeste gar nichts
        // aus - genau das Verhalten, das sich wie ein toter Touch anfuehlt.
        // Zum Unterscheiden von Tippen und Wischen genuegt die Strecke.
        if (absDx <= TAP_MAX_DIST && absDy <= TAP_MAX_DIST) {
            g.type = GestureType::Tap;
            g.x    = _startX;
            g.y    = _startY;
        } else if (absDx >= SWIPE_MIN_DIST && absDy <= SWIPE_MAX_OFFAX) {
            g.type = dx < 0 ? GestureType::SwipeLeft : GestureType::SwipeRight;
        }
        return g;
    }

    return g;
}