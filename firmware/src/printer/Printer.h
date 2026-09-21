#pragma once

#include <ArduinoJson.h>
#include <Arduino.h>

#include "config.h"

// ESC/POS-Drucker als reines Ausgabegeraet.
//
// Die Firmware kennt kein Etikettenlayout mehr. Der Server schickt eine Liste
// von Bloecken (text, row, sep, qr, code128, feed); dieser Treiber setzt sie in
// ESC/POS-Bytes um. Layoutaenderungen brauchen deshalb kein OTA.
//
// Gedruckt wird ausschliesslich aus dem Hauptloop: ein Etikett belegt die UART
// mehrere hundert Millisekunden. Im Vorgaengerprojekt stand waehrenddessen der
// gesamte Webserver still.
class Printer {
public:
    void begin(uint32_t baud = PRINTER_BAUD);

    // Auftrag einreihen. false = Warteschlange voll (dann meldet der Aufrufer
    // dem Server einen Fehlschlag, statt ihn stillschweigend zu verlieren).
    bool enqueue(int jobId, JsonDocument &job);

    // Hoechstens einen Auftrag pro Aufruf drucken.
    // Gibt die abgeschlossene Auftragsnummer zurueck, sonst 0.
    int  process(bool &ok, String &error);

    size_t queued() const { return _count; }
    void   setPaperChars(uint8_t chars) { _chars = chars; }

private:
    void writeBlocks(JsonArray blocks);
    void lineSpacing(uint8_t dots);
    void textLine(const String &text, uint8_t align, bool bold, bool large, uint16_t height);
    void row(const String &key, const String &value, bool underline, uint16_t height);
    void separator(uint16_t height);
    void qr(const String &data, uint16_t reserved);
    void code128(const String &data, uint8_t height);
    void feedDots(uint16_t dots);
    void backfeedDots(uint8_t dots);
    void reset();
    String toCp1252(const String &utf8) const;

    static constexpr size_t MAX_QUEUE = 8;

    // Ruhezone des QR-Codes in Modulen. Muss mit services/labels.QR_QUIET
    // uebereinstimmen - der Server reserviert danach die Hoehe.
    static constexpr int QR_QUIET = 2;

    struct Job {
        int jobId = 0;
        JsonDocument doc;
    };

    Job     _queue[MAX_QUEUE];
    size_t  _head = 0, _count = 0;
    uint8_t _chars = 32;
    bool    _rotate = false;   // Hochkant: Text um 90 Grad gedreht
    bool    _ready = false;

    // Zuletzt gesetzter Zeilenabstand (`ESC 3 n`). -1 = unbekannt, der
    // naechste Block setzt ihn in jedem Fall.
    int16_t _spacing = -1;
};

extern Printer printer;
