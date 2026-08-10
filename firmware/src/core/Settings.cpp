#include "Settings.h"

#include <Preferences.h>
#include <WiFi.h>
#include <esp_mac.h>   // esp_read_mac() - nicht ueber Arduino.h eingebunden

#include "config.h"

Settings settings;

static Preferences prefs;
static const char *NS = "terminal";

void Settings::begin() {
    // Einmal schreibend oeffnen legt den Namespace an. Sonst meldet
    // Preferences beim ersten Start "nvs_open failed: NOT_FOUND" - harmlos,
    // aber es sieht im Log nach einem Fehler aus.
    if (prefs.begin(NS, false)) prefs.end();

    prefs.begin(NS, true);
    wifiSsid   = prefs.getString("ssid", "");
    wifiPass   = prefs.getString("pass", "");
    serverHost = prefs.getString("host", DEFAULT_SERVER_HOST);
    serverPort = prefs.getUShort("port", DEFAULT_SERVER_PORT);
    token      = prefs.getString("token", DEFAULT_DEVICE_TOKEN);
    deviceName = prefs.getString("name", "");
    useTls     = prefs.getBool("tls", false);
    prefs.end();

    if (deviceName.isEmpty()) deviceName = "Terminal-" + deviceId().substring(6);
}

void Settings::save() {
    prefs.begin(NS, false);
    prefs.putString("ssid", wifiSsid);
    prefs.putString("pass", wifiPass);
    prefs.putString("host", serverHost);
    prefs.putUShort("port", serverPort);
    prefs.putString("token", token);
    prefs.putString("name", deviceName);
    prefs.putBool("tls", useTls);
    prefs.end();
}

void Settings::clear() {
    prefs.begin(NS, false);
    prefs.clear();
    prefs.end();
}

String Settings::deviceId() {
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char buf[13];
    snprintf(buf, sizeof(buf), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return String(buf);
}
