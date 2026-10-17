#include "OfflineQueue.h"

#include <Preferences.h>

OfflineQueue offlineQueue;

static const char *NS  = "offline";
static const char *KEY = "q";

void OfflineQueue::begin() {
    Preferences prefs;
    // Erst schreibend oeffnen, damit es den Namensraum gibt - sonst meldet
    // das Lesen beim allerersten Start "NOT_FOUND" (harmlos, aber laut).
    if (prefs.begin(NS, false)) prefs.end();
    if (!prefs.begin(NS, true)) return;
    const String raw = prefs.getString(KEY, "");
    prefs.end();
    if (raw.isEmpty()) return;

    JsonDocument doc;
    if (deserializeJson(doc, raw)) return;
    for (JsonArray e : doc.as<JsonArray>()) {
        if (_items.size() >= MAX_ENTRIES) break;
        OfflineScan s;
        s.code = String(e[0] | "");
        s.location = String(e[1] | "");
        s.atMs = 0;   // aus der Zeit vor dem Neustart - Alter unbekannt
        if (!s.code.isEmpty()) _items.push_back(s);
    }
    if (!_items.empty()) log_i("%u Offline-Scans aus dem Speicher geladen", (unsigned)_items.size());
}

bool OfflineQueue::add(const String &code, const String &location) {
    if (full()) return false;
    OfflineScan s;
    s.code = code.substring(0, 40);
    s.location = location.substring(0, 30);
    s.atMs = millis() | 1;    // nie 0 - 0 heisst "vor dem Neustart"
    _items.push_back(s);
    save();
    return true;
}

size_t OfflineQueue::buildBatch(JsonDocument &doc, size_t max) const {
    doc["t"] = "offline";
    JsonArray arr = doc["items"].to<JsonArray>();
    size_t n = 0;
    const uint32_t now = millis();
    for (const OfflineScan &s : _items) {
        if (n >= max) break;
        JsonObject o = arr.add<JsonObject>();
        o["code"] = s.code;
        o["loc"] = s.location;
        o["age_s"] = s.atMs ? (int32_t)((now - s.atMs) / 1000) : -1;
        n++;
    }
    return n;
}

void OfflineQueue::drop(size_t n) {
    if (n >= _items.size()) _items.clear();
    else _items.erase(_items.begin(), _items.begin() + n);
    save();
}

void OfflineQueue::save() {
    // Kurz und kompakt: [[code, ort], ...] - siehe MAX_ENTRIES.
    JsonDocument doc;
    JsonArray arr = doc.to<JsonArray>();
    for (const OfflineScan &s : _items) {
        JsonArray e = arr.add<JsonArray>();
        e.add(s.code);
        e.add(s.location);
    }
    String out;
    serializeJson(doc, out);
    Preferences prefs;
    if (!prefs.begin(NS, false)) return;
    if (_items.empty()) prefs.remove(KEY);
    else prefs.putString(KEY, out);
    prefs.end();
}
