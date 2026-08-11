#pragma once

#include <Arduino.h>

#include "config.h"

// Tonausgabe ueber den ES8311-Codec.
//
// Das Board hat keinen Piezo - was nach Summer klingt, kommt aus dem
// Audiocodec und geht ueber I2S. Der Aufbau ist aus dem Vorgaengerprojekt
// uebernommen (Registerfolge, Taktverhaeltnisse, Spannungsschienen des
// AXP2101), die Wiedergabe aber nicht: dort lief ein eigener Task mit
// blockierendem i2s_write, hier laeuft alles im Hauptloop.
//
// loop() schiebt pro Durchlauf so viele Abtastwerte nach, wie der
// DMA-Puffer gerade aufnimmt, und kehrt sofort zurueck. Damit ein langer
// Bildaufbau (~60 ms) den Ton nicht abreissen laesst, ist der Puffer
// grosszuegig: 8 x 256 Bilder bei 48 kHz sind rund 43 ms Vorlauf.
class Audio {
public:
    // Gibt false zurueck, wenn kein Codec gefunden wurde. Das ist kein
    // Fehlerfall - das Geraet arbeitet dann stumm weiter.
    bool begin();
    bool available() const { return _ok; }

    void playTone(uint16_t hz, uint16_t ms);
    void loop();

    void setVolume(uint8_t percent);

private:
    void generate();

    static constexpr uint32_t SAMPLE_RATE = 48000;
    static constexpr size_t   CHUNK = 256;      // Bilder je Erzeugungsschritt
    static constexpr uint32_t RAMP_FRAMES = 240;  // ~5 ms weich einblenden

    bool     _ok = false;
    uint8_t  _volume = 70;

    // Erzeugungszustand des laufenden Tons
    uint32_t _framesLeft = 0;
    float    _phase = 0.0f;
    float    _step  = 0.0f;
    float    _amp   = 0.0f;
    uint32_t _ramp  = 0;

    // Was erzeugt, aber noch nicht in den DMA-Puffer gepasst hat.
    int16_t  _staging[CHUNK * 2];
    size_t   _stagingBytes = 0;
    size_t   _stagingSent  = 0;

    uint32_t _silentSince = 0;   // fuer das verzoegerte Abschalten der Endstufe
    bool     _ampOn = false;
};

extern Audio audio;
