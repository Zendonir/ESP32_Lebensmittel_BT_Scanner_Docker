#include "Net.h"

#include <DNSServer.h>
#include <WebServer.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#include "Settings.h"
#include "config.h"

Net net;

static WebSocketsClient ws;
static WebServer portal(80);
static DNSServer dns;
static Net *self = nullptr;

// ---------------------------------------------------------------------------
// Einrichtungsportal
// Nur aktiv, solange kein WLAN hinterlegt ist oder BOOT beim Start gedrueckt
// wird. Die Seite steht im Flash statt in einem Dateisystem - damit gibt es
// kein zweites Abbild, das beim OTA vergessen werden kann.
// ---------------------------------------------------------------------------
static const char PORTAL_HTML[] PROGMEM = R"HTML(<!doctype html><html lang=de><head>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Terminal einrichten</title><style>
body{font:15px/1.5 system-ui;margin:0;background:#12171b;color:#e6edf3;padding:20px}
.c{max-width:420px;margin:0 auto;background:#1a2126;border:1px solid #2e3941;border-radius:10px;padding:20px}
h1{font-size:18px;margin:0 0 4px}p.m{color:#93a3ae;margin:0 0 18px;font-size:13px}
label{display:block;margin:12px 0 4px;font-size:12px;color:#93a3ae}
input,select{width:100%;padding:9px;border-radius:8px;border:1px solid #2e3941;background:#232c33;color:#e6edf3;font:inherit;box-sizing:border-box}
button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:#1e88e5;color:#fff;font:inherit;font-weight:600}
</style></head><body><div class=c>
<h1>Lebensmittel-Terminal</h1><p class=m>Geraet %ID%</p>
<form method=post action=/save>
<label>WLAN-Name</label><input name=ssid value="%SSID%" required list=nets>
<datalist id=nets>%NETS%</datalist>
<label>WLAN-Passwort</label><input name=pass type=password value="%PASS%">
<label>Server (Host oder IP)</label><input name=host value="%HOST%" required>
<label>Port</label><input name=port type=number value="%PORT%" required>
<label>Geraete-Token (DEVICE_TOKEN aus der .env)</label><input name=token value="%TOKEN%" required>
<label>Anzeigename</label><input name=name value="%NAME%">
<label>Verschluesselung</label><select name=tls><option value=0>HTTP / WS</option>
<option value=1 %TLSSEL%>HTTPS / WSS</option></select>
<button>Speichern und neu starten</button></form></div></body></html>)HTML";

void Net::begin() {
    self = this;
    settings.begin();

    pinMode(BOOT_BTN, INPUT_PULLUP);
    const bool forcePortal = digitalRead(BOOT_BTN) == LOW;

    if (!settings.configured() || forcePortal) {
        startPortal();
        return;
    }
    connectWifi();
}

void Net::connectWifi() {
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);            // Latenz vor Stromverbrauch: das Geraet haengt am Netzteil
    WiFi.setAutoReconnect(true);
    WiFi.setHostname(("terminal-" + Settings::deviceId().substring(6)).c_str());
    WiFi.begin(settings.wifiSsid.c_str(), settings.wifiPass.c_str());
    _lastWifiTry = millis();
    log_i("WLAN-Verbindung zu %s", settings.wifiSsid.c_str());
}

void Net::startPortal() {
    _portalActive = true;
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_PASSWORD);
    dns.start(53, "*", WiFi.softAPIP());
    WiFi.scanNetworks(true);

    portal.on("/", HTTP_GET, [] {
        String page = FPSTR(PORTAL_HTML);
        String nets;
        const int found = WiFi.scanComplete();
        for (int i = 0; i < found && i < 20; i++) {
            nets += "<option value=\"" + WiFi.SSID(i) + "\">";
        }
        page.replace("%ID%", Settings::deviceId());
        page.replace("%SSID%", settings.wifiSsid);
        page.replace("%PASS%", settings.wifiPass);
        page.replace("%HOST%", settings.serverHost);
        page.replace("%PORT%", String(settings.serverPort));
        page.replace("%TOKEN%", settings.token);
        page.replace("%NAME%", settings.deviceName);
        page.replace("%NETS%", nets);
        page.replace("%TLSSEL%", settings.useTls ? "selected" : "");
        portal.send(200, "text/html; charset=utf-8", page);
    });

    portal.on("/save", HTTP_POST, [] {
        settings.wifiSsid   = portal.arg("ssid");
        settings.wifiPass   = portal.arg("pass");
        settings.serverHost = portal.arg("host");
        settings.serverPort = portal.arg("port").toInt() ?: 8080;
        settings.token      = portal.arg("token");
        settings.deviceName = portal.arg("name");
        settings.useTls     = portal.arg("tls") == "1";
        settings.save();
        portal.send(200, "text/html; charset=utf-8",
                    "<meta charset=utf-8><body style='font:16px system-ui;padding:30px'>"
                    "Gespeichert. Das Terminal startet neu.</body>");
        delay(600);
        ESP.restart();
    });

    portal.onNotFound([] {  // Captive-Portal: alles auf die Startseite
        portal.sendHeader("Location", "/", true);
        portal.send(302, "text/plain", "");
    });

    portal.begin();
    log_w("Einrichtungsportal aktiv: SSID %s, IP %s", AP_SSID,
          WiFi.softAPIP().toString().c_str());
}

void Net::handlePortal() {
    dns.processNextRequest();
    portal.handleClient();
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
void Net::startSocket() {
    const String url = String(WS_PATH) + "?id=" + Settings::deviceId() +
                       "&token=" + settings.token;

    if (settings.useTls) {
        ws.beginSSL(settings.serverHost.c_str(), settings.serverPort, url.c_str());
    } else {
        ws.begin(settings.serverHost.c_str(), settings.serverPort, url.c_str());
    }

    ws.onEvent([](WStype_t type, uint8_t *payload, size_t length) {
        if (self) self->handleSocketEvent(type, payload, length);
    });
    // Der eingebaute Reconnect erspart eigene Zustandsverwaltung - genau die
    // war im Vorgaengerprojekt die Quelle haengender Verbindungsflags.
    ws.setReconnectInterval(WS_RECONNECT_MS);
    ws.enableHeartbeat(15000, 5000, 2);
    _wsStarted = true;
    log_i("WebSocket zu %s:%u%s", settings.serverHost.c_str(), settings.serverPort, url.c_str());
}

void Net::handleSocketEvent(uint8_t type, uint8_t *payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            _wsConnected = true;
            log_i("Server verbunden");
            sendHello();
            break;

        case WStype_DISCONNECTED:
            if (_wsConnected) log_w("Server getrennt");
            _wsConnected = false;
            break;

        case WStype_TEXT: {
            // Der Server schickt kleine Bildschirmbeschreibungen; 8 KB decken
            // auch eine lange Liste ab. Bei Ueberlauf wird die Nachricht
            // verworfen statt den Heap zu sprengen.
            JsonDocument doc;
            const DeserializationError err = deserializeJson(doc, payload, length);
            if (err) {
                log_w("JSON-Fehler: %s", err.c_str());
                return;
            }
            if (_handler) _handler(doc);
            break;
        }

        case WStype_ERROR:
            log_w("WebSocket-Fehler");
            break;

        default:
            break;
    }
}

void Net::sendHello() {
    JsonDocument doc;
    doc["t"]           = "hello";
    doc["device_id"]   = Settings::deviceId();
    doc["name"]        = settings.deviceName;
    doc["firmware"]    = FIRMWARE_VERSION;
    doc["ip"]          = ip();
    doc["has_printer"] = true;
    send(doc);
    _helloAt = millis();
}

bool Net::send(const JsonDocument &doc) {
    if (!_wsConnected) return false;
    String out;
    serializeJson(doc, out);
    return ws.sendTXT(out);
}

bool Net::sendEvent(const char *type) {
    JsonDocument doc;
    doc["t"] = type;
    return send(doc);
}

void Net::loop() {
    if (_portalActive) {
        handlePortal();
        return;
    }

    if (WiFi.status() != WL_CONNECTED) {
        _wsConnected = false;
        // WiFi.setAutoReconnect() greift nicht in jedem Fehlerfall (z.B. wenn
        // der Router waehrend des DHCP-Vorgangs verschwindet). Deshalb alle
        // 20 s ein expliziter Neuversuch.
        if (millis() - _lastWifiTry > 20000) {
            log_w("WLAN weg - neuer Verbindungsversuch");
            WiFi.disconnect();
            WiFi.begin(settings.wifiSsid.c_str(), settings.wifiPass.c_str());
            _lastWifiTry = millis();
        }
        return;
    }

    if (!_wsStarted) startSocket();
    ws.loop();
}

bool Net::wifiConnected() const { return WiFi.status() == WL_CONNECTED; }
String Net::ip() const {
    return _portalActive ? WiFi.softAPIP().toString() : WiFi.localIP().toString();
}
int Net::rssi() const { return WiFi.RSSI(); }
