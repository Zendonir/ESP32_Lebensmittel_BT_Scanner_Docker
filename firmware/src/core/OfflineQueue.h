#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <vector>

// Scans, die ohne Serververbindung anfallen.
//
// Sie liegen im NVS und ueberstehen damit auch einen Neustart oder einen
// Stromausfall. Was ein Code bedeutet, entscheidet weiterhin der Server: beim
// naechsten Verbinden geht alles als "offline"-Nachricht hinaus, und erst
// dessen Quittung loescht den Speicher. Kommt sie nicht an, wird dasselbe
// spaeter noch einmal geschickt - der Server zaehlt doppelte Etiketten nicht
// zweimal.
struct OfflineScan {
    String   code;
    String   location;     // Lagerort, der beim Scan aktiv war
    uint32_t atMs = 0;     // millis() beim Scan; 0 = vor dem letzten Neustart
};

class OfflineQueue {
public:
    // 40 Eintraege mit Code (<= 40 Zeichen) und Ort (<= 30) bleiben sicher
    // unter der Grenze eines NVS-Texts (4000 Bytes).
    static constexpr size_t MAX_ENTRIES = 40;

    void begin();                       // aus dem NVS laden
    bool add(const String &code, const String &location);   // false = voll
    size_t size() const { return _items.size(); }
    bool full() const { return _items.size() >= MAX_ENTRIES; }
    const std::vector<OfflineScan> &items() const { return _items; }

    // Die ersten bis zu `max` Eintraege als "offline"-Nachricht.
    size_t buildBatch(JsonDocument &doc, size_t max) const;
    // Nach der Quittung: die ersten n Eintraege sind verbucht.
    void drop(size_t n);

private:
    void save();
    std::vector<OfflineScan> _items;
};

extern OfflineQueue offlineQueue;
