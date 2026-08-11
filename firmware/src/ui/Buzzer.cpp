#include "Buzzer.h"

#include "Audio.h"
#include "config.h"

Buzzer buzzer;

// Die Toene gehen ueber den ES8311-Codec (siehe Audio) - das Board hat keinen
// Piezo. BUZZER_PIN bleibt als Ausweichweg bestehen, falls doch einmal einer
// angeloetet wird; standardmaessig ist er -1 und damit aus.
void Buzzer::begin() {
    const bool haveCodec = audio.begin();

    if (!haveCodec && BUZZER_PIN >= 0) {
        ledcAttach(BUZZER_PIN, 2000, 10);
        ledcWriteTone(BUZZER_PIN, 0);
        return;
    }
    if (!haveCodec) _enabled = false;
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
    // Der Codec will in jedem Durchlauf bedient werden, auch wenn gerade keine
    // Tonfolge laeuft - er blendet danach noch die Endstufe ab.
    audio.loop();

    if (!_active) return;
    if ((int32_t)(millis() - _nextAt) < 0) return;

    if (_index >= _count) {
        if (BUZZER_PIN >= 0 && !audio.available()) ledcWriteTone(BUZZER_PIN, 0);
        _active = false;
        return;
    }

    const Tone &tone = _tones[_index];
    if (audio.available()) {
        // Eine Pause ist ein Ton mit 0 Hz - da wird schlicht nichts erzeugt,
        // die Laufzeit steuert weiterhin _nextAt.
        audio.playTone(tone.hz, tone.ms);
    } else if (BUZZER_PIN >= 0) {
        ledcWriteTone(BUZZER_PIN, tone.hz);
    }
    _nextAt = millis() + tone.ms;
    _index++;
}
