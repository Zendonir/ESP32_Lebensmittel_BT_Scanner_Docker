#include "Buttons.h"

Buttons buttons;

void Buttons::begin() {
    for (const Key *key : {&_back, &_up, &_down}) {
        if (key->pin >= 0) pinMode(key->pin, INPUT_PULLUP);
    }
}

// Entprellen und Flanke erkennen: erst wenn ein Pegel BTN_DEBOUNCE_MS lang
// unveraendert anliegt, gilt er. Ein Taster prellt beim Druecken sonst
// mehrfach und loest die Aktion gleich zwei- oder dreimal aus.
bool Buttons::update(Key &key, uint32_t now) {
    if (key.pin < 0) return false;

    const bool pressed = digitalRead(key.pin) == LOW;

    if (pressed != key.raw) {          // Pegel bewegt sich - Uhr neu stellen
        key.raw       = pressed;
        key.changedAt = now;
        return false;
    }
    if (now - key.changedAt < BTN_DEBOUNCE_MS) return false;   // noch unruhig

    if (pressed != key.down) {         // stabile neue Lage
        key.down = pressed;
        if (!pressed) return false;    // Loslassen loest nichts aus
        key.nextRepeat = now + 500;    // Wiederholung erst nach kurzem Halten
        return true;
    }

    if (pressed && key.repeats && now >= key.nextRepeat) {
        key.nextRepeat = now + BTN_REPEAT_MS;
        return true;
    }
    return false;
}

ButtonEvent Buttons::poll() {
    const uint32_t now = millis();
    if (update(_back, now)) return ButtonEvent::Back;
    if (update(_up, now))   return ButtonEvent::ScrollUp;
    if (update(_down, now)) return ButtonEvent::ScrollDown;
    return ButtonEvent::None;
}
