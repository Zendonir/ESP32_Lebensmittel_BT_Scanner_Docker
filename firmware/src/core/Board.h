#pragma once

#include <Arduino.h>
#include <Wire.h>

// Hardware-Eigenheiten des Waveshare ESP32-S3-Touch-LCD-3.5, die vor allem
// anderen erledigt sein muessen.
//
// Auf diesem Board haengen weder der Reset des Displays noch der des
// Touchcontrollers an einem GPIO des ESP32 - beide gehen ueber einen
// TCA9554-Portexpander auf dem I2C-Bus. Wer das ueberspringt, bekommt einen
// ST7796, der nie zurueckgesetzt wurde (Anzeige voller Muell) und einen
// FT6336, der auf dem Bus gar nicht erst antwortet.
//
// Reihenfolge ist zwingend: I2C -> Expander -> TFT.
class Board {
public:
    // I2C starten und Display/Touch aus dem Reset holen.
    // Gibt false zurueck, wenn der Expander nicht antwortet.
    bool begin();

    bool expanderFound() const { return _expanderOk; }
    TwoWire &i2c() { return Wire; }

    // Lautsprecherverstaerker (EXIO7). Audio schaltet ihn zum Ton ein und kurz
    // danach wieder aus - dauerhaft an rauscht der Lautsprecher leise.
    void setAmplifier(bool on);

private:
    uint8_t write(uint8_t reg, uint8_t value);
    uint8_t read(uint8_t reg);

    bool    _expanderOk = false;
    uint8_t _outputs = 0xFF;
};

extern Board board;
