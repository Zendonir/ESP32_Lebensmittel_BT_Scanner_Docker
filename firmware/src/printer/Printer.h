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

    // Am aktuellen Auftrag weiterdrucken - Block fuer Block, und nur so viel,
    // wie gerade in den Sendepuffer passt. Ein Hochformat-Etikett ist ein Bild
    // von rund 11 KB; bei 9600 Baud sind das zehn Sekunden, und am Stueck
    // geschrieben stuende der Loop so lange (Touch, Scanner, Server).
    // Gibt die Auftragsnummer zurueck, sobald der Auftrag ganz gesendet ist,
    // sonst 0.
    int  process(bool &ok, String &error);

    size_t queued() const { return _count; }
    void   setPaperChars(uint8_t chars) { _chars = chars; }

private:
    void   writeBlock(JsonObject block);
    size_t blockCost(JsonObject block) const;
    void lineSpacing(uint8_t dots);
    void textLine(const String &text, uint8_t align, bool bold, bool large, uint16_t height);
    void row(const String &key, const String &value, bool underline, uint16_t height);
    void separator(uint16_t height);
    void qr(const String &data, uint16_t reserved);
    void raster(const char *b64, size_t b64len, uint16_t w, uint16_t h,
                const char *mode, bool packed);
    void code128(const String &data, uint8_t height);
    void feedDots(uint16_t dots);
    void formFeed();
    void backfeedDots(uint8_t dots);
    void reset();
    String toCp1252(const String &utf8) const;

    static constexpr size_t MAX_QUEUE = 8;

    // Ruhezone des QR-Codes in Modulen. Muss mit services/labels.QR_QUIET
    // uebereinstimmen - der Server reserviert danach die Hoehe.
    static constexpr int QR_QUIET = 2;

    // Obergrenze fuer einen einzelnen Rasterblock, in Bytes auf der Leitung.
    // Muss unter der Schwelle in process() bleiben, sonst wartet write() doch
    // wieder auf die 9600-Baud-Leitung und der Loop steht. Ein ganzes Etikett
    // als Bild (50x30 mm sind rund 12 KB) passt damit noch nicht - dafuer
    // muesste process() das Bild ueber mehrere Durchlaeufe verteilen.
    static constexpr size_t MAX_RASTER_TRAFFIC = 4096;

    // Sendepuffer (setTxBufferSize in begin()) und wie viel davon ein Block
    // hoechstens abwarten darf. Ein Block, der mehr verlangt, wird trotzdem
    // geschrieben, sobald der Puffer so leer ist - sonst wartete er ewig.
    static constexpr size_t TX_BUFFER   = 8192;
    static constexpr size_t MAX_WAIT_FOR = 6144;
    // Mehr als der Hardware-FIFO (128 Byte) - siehe process().
    static constexpr size_t MIN_FREE    = 256;

    // Die Auftraege liegen im PSRAM. Ein Hochformat-Etikett bringt einige
    // Kilobyte Bilddaten mit, vier davon sind gleichzeitig unterwegs - im
    // internen Speicher, den sich WLAN, BLE und die Anzeige teilen, waere das
    // ein Viertel des Freien. Nur Daten, keine Task-Stacks (siehe CLAUDE.md).
    struct PsramAllocator : ArduinoJson::Allocator {
        void *allocate(size_t size) override;
        void  deallocate(void *ptr) override;
        void *reallocate(void *ptr, size_t size) override;
    };
    static PsramAllocator psram;

    struct Job {
        int jobId = 0;
        JsonDocument doc{&psram};
    };

    Job     _queue[MAX_QUEUE];
    size_t  _head = 0, _count = 0;
    uint8_t _chars = 32;
    bool    _rotate = false;   // Hochkant: Text um 90 Grad gedreht
    bool    _ready = false;

    // Der Auftrag an der Spitze der Warteschlange ist angefangen, bis Block
    // `_nextBlock` ist er auf der Leitung.
    bool    _active = false;
    size_t  _nextBlock = 0;

    // Zuletzt gesetzter Zeilenabstand (`ESC 3 n`). -1 = unbekannt, der
    // naechste Block setzt ihn in jedem Fall.
    int16_t _spacing = -1;
};

extern Printer printer;
