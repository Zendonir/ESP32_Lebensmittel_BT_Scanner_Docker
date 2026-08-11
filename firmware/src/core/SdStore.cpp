#include "SdStore.h"

#include <ArduinoJson.h>
#include <SD_MMC.h>

#include "Settings.h"
#include "config.h"

SdStore sdStore;

static const char *SETTINGS_PATH = "/terminal_settings.json";

void SdStore::ensureMounted() {
    if (_mountTried) return;
    _mountTried = true;

    SD_MMC.end();  // nach einem Soft-Reset kann der Treiber halb initialisiert sein
    SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0);
    _mounted = SD_MMC.begin("/sdcard", true);  // 1-Bit-Modus, kein CS noetig

    if (_mounted) {
        log_i("SD-Karte bereit (%llu MB)", SD_MMC.cardSize() / (1024ULL * 1024ULL));
    } else {
        log_i("Keine SD-Karte gefunden - Zugangsdaten bleiben nur im NVS");
    }
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

    settings.wifiSsid   = ssid;
    settings.wifiPass   = String(doc["wifi_pass"]   | "");
    settings.serverHost = String(doc["server_host"] | settings.serverHost);
    settings.serverPort = doc["server_port"] | settings.serverPort;
    settings.token = String(doc["token"]       | "");
    settings.deviceName = String(doc["device_name"] | settings.deviceName);
    settings.useTls     = doc["use_tls"] | false;
    return true;
}
