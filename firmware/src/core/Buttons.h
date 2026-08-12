#pragma once

#include <Arduino.h>

#include "config.h"

// Drei optionale Taster: Zurueck, Hochscrollen, Runterscrollen.
//
// Sie ergaenzen die Bedienung, sie ersetzen sie nicht - dieselben Wege gibt es
// weiterhin per Wischen und Ziehen. Ist nichts angeloetet, liegen die
// Eingaenge durch den internen Pull-up auf HIGH und es passiert schlicht
// nichts.
//
// Die Scrolltaster wiederholen beim Halten, der Zurueck-Taster bewusst nicht:
// zweimal versehentlich zurueck ist aergerlicher als einmal zu wenig.
enum class ButtonEvent { None, Back, ScrollUp, ScrollDown };

class Buttons {
public:
    void begin();

    // Liefert hoechstens ein Ereignis je Aufruf. Muss im Hauptloop laufen.
    ButtonEvent poll();

private:
    struct Key {
        int8_t   pin;
        bool     repeats;
        bool     raw;          // zuletzt gelesener Pegel
        bool     down;         // entprellter Zustand
        uint32_t changedAt;    // wann sich der Pegel zuletzt bewegt hat
        uint32_t nextRepeat;
    };

    bool update(Key &key, uint32_t now);   // true = ausloesen

    Key _back = {BTN_BACK_PIN, false, false, false, 0, 0};
    Key _up   = {BTN_UP_PIN,   true,  false, false, 0, 0};
    Key _down = {BTN_DOWN_PIN, true,  false, false, 0, 0};
};

extern Buttons buttons;
