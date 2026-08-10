#pragma once

#include <Arduino.h>

// Alles, was das Terminal dauerhaft wissen muss - mehr nicht.
// Im Vorgaengerprojekt lagen hier zusaetzlich Inventar, Vorlagen, Kategorien,
// Lagerorte, Produktcache und UI-Einstellungen. Die liegen jetzt im Server.
struct Settings {
    String wifiSsid;
    String wifiPass;
    String serverHost;
    uint16_t serverPort = 8080;
    String  token;
    String  deviceName;
    bool    useTls = false;

    void begin();
    void save();
    void clear();

    // Eindeutige, stabile Geraetekennung aus der MAC-Adresse.
    static String deviceId();
    bool configured() const { return wifiSsid.length() > 0 && serverHost.length() > 0; }
};

extern Settings settings;
