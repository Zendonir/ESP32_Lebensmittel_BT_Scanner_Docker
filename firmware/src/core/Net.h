#pragma once

#include <ArduinoJson.h>
#include <Arduino.h>
#include <functional>

// WLAN-Verbindung, Einrichtungsportal und die WebSocket-Verbindung zum Server.
//
// Der WebSocket ist die einzige Datenverbindung des Geraets. Faellt er aus,
// verbindet die Bibliothek selbsttaetig neu; das Geraet zeigt so lange einen
// Hinweis, arbeitet aber sonst unveraendert weiter.
class Net {
public:
    using MessageHandler = std::function<void(JsonDocument &)>;

    void begin();
    void loop();

    void onMessage(MessageHandler handler) { _handler = handler; }

    bool wifiConnected() const;
    bool serverConnected() const { return _wsConnected; }
    bool portalActive() const { return _portalActive; }

    // Einrichtungsportal von Hand oeffnen (System-Panel, "WLAN einrichten"),
    // ohne auf BOOT-beim-Start oder einen anhaltenden Ausfall zu warten.
    void forcePortal() { if (!_portalActive) startPortal(); }
    String ip() const;
    int  rssi() const;

    // Nachricht an den Server. Gibt false zurueck, wenn keine Verbindung
    // besteht - der Aufrufer entscheidet dann, ob er es spaeter erneut
    // versucht (Scans werden gepuffert, Telemetrie wird verworfen).
    bool send(const JsonDocument &doc);
    bool sendEvent(const char *type);

private:
    void startPortal();
    void handlePortal();
    void connectWifi();
    void startSocket();
    void handleSocketEvent(uint8_t type, uint8_t *payload, size_t length);
    void sendHello();
    void trackConnectionHealth(bool fullyConnected);

    MessageHandler _handler;
    bool _wsConnected   = false;
    bool _wsStarted     = false;
    bool _portalActive  = false;
    uint32_t _lastWifiTry = 0;
    uint32_t _helloAt     = 0;

    // Gestufte Wiederherstellung bei anhaltendem Ausfall - siehe config.h.
    uint32_t _disconnectedSince = 0;   // 0 = gerade verbunden
    bool     _sdRetryDone       = false;
};

extern Net net;
