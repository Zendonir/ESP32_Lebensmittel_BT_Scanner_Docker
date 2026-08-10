#pragma once

#include <Arduino.h>

// FT6336-Touchcontroller, gepollt ueber I2C.
//
// Das Polling laeuft im Hauptloop, nicht in einem eigenen Task. Im
// Vorgaengerprojekt lag es auf Core 0 und musste sich beim Watchdog anmelden -
// mit dem schlanken Loop hier gibt es keinen Grund mehr fuer einen zweiten
// Task, und damit auch keine Race Conditions beim Zeichnen.
class Touch {
public:
    bool begin();

    // Liefert genau einmal true, wenn ein Finger neu aufgesetzt hat
    // (Flankenerkennung inklusive Entprellung).
    bool pressed(int16_t &x, int16_t &y);

    bool available() const { return _ok; }

private:
    bool read(int16_t &x, int16_t &y);

    bool     _ok = false;
    bool     _down = false;
    uint32_t _lastEvent = 0;
    uint32_t _lastPoll = 0;
};

extern Touch touch;
