#pragma once

#include <Arduino.h>
#include <deque>

// USB-Host am OTG-Anschluss (GPIO19/20, auf der 3.5B auch Leiste Pin 6/8).
//
// Genau ein Geraet: ein Etikettendrucker (USB-Druckerklasse, gedacht fuer den
// Brother TD-2020A) oder ein Barcodescanner, der sich als Tastatur meldet.
// Einen Hub kennt der Host nicht.
//
// Wie alles andere laeuft der Host im Hauptloop: loop() fragt die
// Ereignisse des USB-Stacks mit Zeitgrenze 0 ab, Uebertragungen sind
// asynchron und melden sich in eben diesem Aufruf zurueck. Keine eigenen
// Tasks (siehe CLAUDE.md) - der Druckerstand im Printserver-Projekt brauchte
// drei davon, weil dort jeder Aufruf blockierend wartete.
//
// Das Raster baut der Server (services/brother.py, dieselbe Funktion wie fuer
// den WLAN-Printserver). Hier werden nur Bytes weitergereicht.
//
// Ist der Host an, gehoert der USB-Anschluss ihm: die serielle Konsole ueber
// USB ist dann stumm. Abschalten geht erst mit einem Neustart (das
// Deinstallieren des Stacks bei offenem Geraet waere eine eigene Baustelle).
class UsbHost {
public:
    bool begin();
    void loop();
    bool active() const { return _installed; }

    // Ein Stueck eines Druckauftrags (Nachricht "usbdruck"). false = der
    // Auftrag ist verloren, error sagt warum - der Aufrufer meldet das.
    bool addChunk(int job, int part, bool last, const char *b64, String &error);
    // Weitersenden; gibt die Auftragsnummer zurueck, sobald ein Auftrag fertig
    // (oder gescheitert) ist, sonst 0.
    int  process(bool &ok, String &error);
    size_t queued() const { return _jobs.size(); }

    // Ein ganzer Barcode vom Tastaturscanner.
    bool readCode(String &code);

    // Fuer die Telemetrie: "drucker", "scanner" oder "".
    const char *deviceKind() const;
    const String &deviceName() const { return _name; }

private:
    struct Job {
        int      id = 0;
        int      nextPart = 0;
        bool     complete = false;
        bool     started = false;   // schon (teilweise) beim Drucker
        bool     broken = false;
        uint8_t *data = nullptr;
        size_t   len = 0;
        size_t   sent = 0;
        uint32_t since = 0;
        uint32_t lastData = 0;
    };

    void openDevice(uint8_t addr);
    void closeDevice();
    void startKeyboard();
    void onKeyboardReport(const uint8_t *report, int len);
    void finishCode();
    void dropJob(size_t index);

    static constexpr size_t   OUT_CHUNK      = 4096;
    static constexpr size_t   MAX_JOBS       = 8;
    static constexpr size_t   MAX_JOB_BYTES  = 256 * 1024;
    static constexpr uint32_t WAIT_PRINTER_MS = 20000;   // Drucker noch nicht eingesteckt/an
    static constexpr uint32_t OUT_TIMEOUT_MS  = 30000;   // Drucker nimmt nichts mehr an
    static constexpr uint32_t PART_TIMEOUT_MS = 30000;   // naechstes Stueck bleibt aus
    static constexpr uint32_t CODE_GAP_MS     = 200;     // Scanner ohne Enter am Ende

    bool     _installed = false;
    std::deque<Job> _jobs;
    std::deque<String> _codes;
    String   _typed;
    uint32_t _lastKeyAt = 0;
    uint8_t  _lastKeys[6] = {0};
    String   _name;
};

extern UsbHost usbHost;
