#include "SdStore.h"

#include <ArduinoJson.h>
#include <SD_MMC.h>

#include "Settings.h"
#include "config.h"

SdStore sdStore;

static const char *SETTINGS_PATH = "/terminal_settings.json";

// Ein misslungener Mount kostet spuerbar Zeit (der Treiber wartet auf eine
// Karte, die nicht da ist). Deshalb nicht bei jedem Aufruf, sondern hoechstens
// alle 30 Sekunden - haeufiger braucht es niemand, der gerade eine Karte
// hineinschiebt.
static constexpr uint32_t REMOUNT_INTERVAL_MS = 30000;

void SdStore::ensureMounted() {
    if (_mounted) return;
    const uint32_t now = millis();
    if (_everTried && (int32_t)(now - _nextTryMs) < 0) return;
    _nextTryMs = now + REMOUNT_INTERVAL_MS;
    const bool first = !_everTried;
    _everTried = true;

#if !SD_ENABLED
    // Boardvariante, deren Kartensteckplatz nicht belegt ist - siehe config.h.
    if (first) log_i("Kartenleser auf dieser Boardvariante nicht angeschlossen");
    return;
#else

    SD_MMC.end();  // nach einem Soft-Reset kann der Treiber halb initialisiert sein
    SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0);
    _mounted = SD_MMC.begin("/sdcard", true);  // 1-Bit-Modus, kein CS noetig

    if (_mounted) {
        log_i("SD-Karte bereit (%llu MB)", SD_MMC.cardSize() / (1024ULL * 1024ULL));
    } else if (first) {
        // Nur einmal melden: ein Geraet ohne Karte laeuft voellig normal, und
        // alle 30 Sekunden dieselbe Zeile im Log hilft niemandem.
        log_i("Keine SD-Karte gefunden - Zugangsdaten bleiben nur im NVS");
    }
#endif
}

void SdStore::saveSettings() {
    ensureMounted();
    if (!_mounted) return;

    JsonDocument doc;
    doc["wifi_ssid"]   = settings.wifiSsid;
    doc["wifi_pass"]   = settings.wifiPass;
    doc["server_host"] = settings.serverHost;
    doc["server_port"] = settings.serverPort;
    doc["token"]       = settings.token;
    doc["device_name"] = settings.deviceName;
    doc["use_tls"]     = settings.useTls;

    File f = SD_MMC.open(SETTINGS_PATH, FILE_WRITE);
    if (!f) {
        log_w("Einstellungen konnten nicht auf die SD-Karte geschrieben werden");
        return;
    }
    serializeJson(doc, f);
    f.close();
}

bool SdStore::loadSettings() {
    ensureMounted();
    if (!_mounted || !SD_MMC.exists(SETTINGS_PATH)) return false;

    File f = SD_MMC.open(SETTINGS_PATH, FILE_READ);
    if (!f) return false;

    JsonDocument doc;
    const DeserializationError err = deserializeJson(doc, f);
    f.close();
    if (err) {
        log_w("Einstellungen auf der SD-Karte sind beschaedigt: %s", err.c_str());
        return false;
    }

    const String ssid = doc["wifi_ssid"] | "";
    if (ssid.isEmpty()) return false;  // keine brauchbare Datei

    // Fehlende Schluessel lassen den bisherigen Wert stehen; nur was in der
    // Datei steht, wird uebernommen.
    //
    // Passwort, Token und TLS-Schalter machten das vorher nicht: eine von
    // Hand geschriebene Datei, die nur WLAN-Name und Server enthielt, loeschte
    // damit das Geraete-Token - und weil Settings::save() die Datei
    // anschliessend zurueckschreibt, war es auch auf der Karte weg. Aus einem
    // Reparaturversuch wurde so ein Geraet, das sich gar nicht mehr anmelden
    // konnte.
    auto text = [&](const char *key, const String &fallback) -> String {
        JsonVariantConst value = doc[key];
        return value.isNull() ? fallback : String(value.as<const char *>());
    };

    settings.wifiSsid   = ssid;
    settings.wifiPass   = text("wifi_pass", settings.wifiPass);
    settings.serverHost = text("server_host", settings.serverHost);
    settings.serverPort = doc["server_port"] | settings.serverPort;
    settings.token      = text("token", settings.token);
    settings.deviceName = text("device_name", settings.deviceName);
    settings.useTls     = doc["use_tls"] | settings.useTls;
    return true;
}
