#include "Buzzer.h"

#include "config.h"

Buzzer buzzer;

void Buzzer::begin() {
    ledcAttach(BUZZER_PIN, 2000, 10);
    ledcWriteTone(BUZZER_PIN, 0);
}

void Buzzer::play(const String &pattern) {
    if (!_enabled) return;

    _count = 0;
    auto add = [&](uint16_t hz, uint16_t ms) {
        if (_count < MAX_TONES) _tones[_count++] = {hz, ms};
    };

    if (pattern == "error") {
        add(400, 160); add(0, 60); add(300, 240);
    } else if (pattern == "warn") {
        add(700, 120); add(0, 70); add(700, 120);
    } else if (pattern == "scan") {
        add(1800, 45);
    } else if (pattern == "print") {
        add(1200, 70); add(0, 40); add(1600, 90);
    } else {  // ok
        add(1400, 70); add(0, 40); add(2000, 90);
    }

    _index = 0;
    _active = true;
    _nextAt = millis();
}

void Buzzer::loop() {
    if (!_active) return;
    if ((int32_t)(millis() - _nextAt) < 0) return;

    if (_index >= _count) {
        ledcWriteTone(BUZZER_PIN, 0);
        _active = false;
        return;
    }

    const Tone &tone = _tones[_index];
    ledcWriteTone(BUZZER_PIN, tone.hz);
    _nextAt = millis() + tone.ms;
    _index++;
}
