#pragma once

#include <Arduino.h>

// FT6336-Touchcontroller, gepollt ueber I2C.
//
// Das Polling laeuft im Hauptloop, nicht in einem eigenen Task. Im
// Vorgaengerprojekt lag es auf Core 0 und musste sich beim Watchdog anmelden -
// mit dem schlanken Loop hier gibt es keinen Grund mehr fuer einen zweiten
// Task, und damit auch keine Race Conditions beim Zeichnen.
//
// Erkannt werden drei Gesten, wie im Vorgaengerprojekt (dort in
// Display::tick()): ein kurzer, kaum bewegter Kontakt ist ein Tap; ein
// schneller, ueberwiegend horizontaler Kontakt ist ein Swipe (dient
// bildschirmweit als Zurueck-Geste); ein anhaltender vertikaler Kontakt auf
// einem scrollbaren Bildschirm wird als Scroll-Drag gewertet und liefert bei
// jedem Poll das Pixel-Delta seit dem letzten Aufruf, bis der Finger abhebt.
enum class GestureType { None, Tap, SwipeLeft, SwipeRight, ScrollDrag, ScrollEnd };

struct Gesture {
    GestureType type   = GestureType::None;
    int16_t     x       = 0;   // Tap: Punkt der Beruehrung
    int16_t     y       = 0;
    int16_t     deltaY  = 0;   // ScrollDrag: Bewegung seit dem letzten Poll
};

class Touch {
public:
    bool begin();

    // Muss jeden Loop-Durchlauf aufgerufen werden. `scrollable` sagt, ob der
    // aktuelle Bildschirm ueberhaupt scrollen kann (Screen::scrollable()) -
    // nur dann wird eine anhaltende Vertikalbewegung als Scroll-Drag statt als
    // (dann unwirksamer) Swipe gewertet.
    Gesture poll(bool scrollable);

    bool available() const { return _ok; }

private:
    bool read(int16_t &x, int16_t &y);

    bool     _ok       = false;
    bool     _down     = false;
    bool     _dragging = false;
    int16_t  _startX = 0, _startY = 0;
    int16_t  _lastX  = 0, _lastY  = 0;
    uint8_t  _releaseDebounce = 0;
    uint32_t _lastPoll = 0;
};

extern Touch touch;
