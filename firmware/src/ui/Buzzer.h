#pragma once

#include <Arduino.h>

#include "config.h"

// Quittungstoene ueber einen passiven Piezo an BUZZER_PIN.
//
// Nicht blockierend: `play()` legt eine kurze Tonfolge ab, `loop()` schaltet
// sie weiter. Ein `delay()` im Hauptloop wuerde die Bedienung traege machen
// und im schlimmsten Fall den Watchdog ausloesen.
class Buzzer {
public:
    void begin();
    void loop();

    // pattern: ok | error | warn | scan | print
    void play(const String &pattern);
    void setEnabled(bool enabled) { _enabled = enabled && (BUZZER_PIN >= 0); }

private:
    struct Tone { uint16_t hz; uint16_t ms; };

    static constexpr uint8_t MAX_TONES = 4;
    Tone     _tones[MAX_TONES];
    uint8_t  _count = 0;
    uint8_t  _index = 0;
    uint32_t _nextAt = 0;
    bool     _enabled = true;
    bool     _active = false;
};

extern Buzzer buzzer;
