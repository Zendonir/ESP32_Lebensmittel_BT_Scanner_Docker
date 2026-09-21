#include "Net.h"

#include <DNSServer.h>
#include <WebServer.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#include "SdStore.h"
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
        startPortal(false);   // von Hand bzw. mangels Zugangsdaten - bleibt offen
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

void Net::startPortal(bool automatic) {
    if (_portalActive) return;
    _portalActive = true;
    _portalAuto = automatic;
    _closePortal = false;
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_PASSWORD);
    dns.start(53, "*", WiFi.softAPIP());
    WiFi.scanNetworks(true);

    // Die Routen nur beim ersten Mal anmelden. Das Portal kann sich im Betrieb
    // oeffnen, schliessen und wieder oeffnen; jedes Mal dieselben Handler
    // anzuhaengen liesse die Liste im WebServer immer weiter wachsen.
    if (_portalRoutes) {
        portal.begin();
        log_w("Einrichtungsportal wieder aktiv: SSID %s, IP %s", AP_SSID,
              WiFi.softAPIP().toString().c_str());
        return;
    }
    _portalRoutes = true;

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
        // Eingaben pruefen, bevor sie ins NVS wandern. Ein Tippfehler im Port
        // (66000) lief vorher stillschweigend durch toInt() und den Ueberlauf
        // nach uint16_t - heraus kam eine Zahl, die niemand eingegeben hat,
        // und das Geraet fand den Server nie wieder. Am Bildschirm stand dann
        // nur "Kein Server", und das Portal zeigte den falschen Wert als
        // waere er richtig.
        const String ssid = portal.arg("ssid");
        const String host = portal.arg("host");
        const long   port = portal.arg("port").toInt();

        if (ssid.isEmpty() || host.isEmpty() || port < 1 || port > 65535) {
            portal.send(400, "text/html; charset=utf-8",
                        "<meta charset=utf-8><body style='font:16px system-ui;padding:30px'>"
                        "WLAN-Name, Server und ein Port zwischen 1 und 65535 werden "
                        "gebraucht. <a href=/>Zurueck</a></body>");
            return;
        }

        settings.wifiSsid   = ssid;
        settings.wifiPass   = portal.arg("pass");
        settings.serverHost = host;
        settings.serverPort = (uint16_t)port;
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

// Selbsttaetig geoeffnetes Portal wieder schliessen. Der Aufrufer hat vorher
// festgestellt, dass die gespeicherten Zugangsdaten doch wieder tragen - es
// gibt dann nichts mehr einzurichten, und ein offener Access Point mit
// bekanntem Passwort soll nicht laenger stehen als noetig.
void Net::stopPortal() {
    if (!_portalActive) return;
    log_i("Verbindung ist zurueck - Einrichtungsportal wird geschlossen");
    portal.stop();
    dns.stop();
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_STA);
    _portalActive = false;
    _portalAuto = false;
    _closePortal = false;
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
            _disconnectedSince = 0;   // Ausfall vorbei - naechster faengt wieder bei 0 an
            _nextSdRetryMs = 0;
            // Der Ausfall, der das Portal aufgemacht hat, ist vorbei. Nicht
            // hier schliessen: wir stecken gerade in ws.loop(), und dem den
            // Netzwerkmodus unter den Fuessen wegzuziehen waere unnoetig
            // heikel. Der naechste Loop-Durchlauf raeumt auf.
            if (_portalAuto) _closePortal = true;
            log_i("Server verbunden");
            sendHello();
            break;

        case WStype_DISCONNECTED:
            if (_wsConnected) log_w("Server getrennt");
            _wsConnected = false;
            break;

        case WStype_TEXT: {
            // Der Server schickt kleine Bildschirmbeschreibungen; 8 KB decken
            // auch eine lange Liste ab.
            //
            // Die Grenze muss hier stehen und nicht im JsonDocument: seit
            // ArduinoJson 7 waechst das Dokument mit den Daten, statt bei
            // einer festen Groesse "NoMemory" zu melden. Der Kommentar
            // versprach also eine Obergrenze, die es gar nicht mehr gab - ein
            // ueberlanger Frame haette den Heap leergeraeumt und das Geraet in
            // den Neustart geschickt. Verwerfen und weitermachen ist besser:
            // der naechste Bildschirm kommt spaetestens beim naechsten Tippen.
            if (length > MAX_MESSAGE_BYTES) {
                log_w("Nachricht verworfen: %u Bytes (Grenze %u)",
                      (unsigned)length, (unsigned)MAX_MESSAGE_BYTES);
                return;
            }
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

    // Statische Geraetedaten fuers System-Panel - aendern sich nicht waehrend
    // der Verbindung, deshalb hier statt in der periodischen Telemetrie.
    doc["ssid"]     = settings.wifiSsid;
    doc["flash_mb"] = ESP.getFlashChipSize() / (1024 * 1024);
    doc["res"]      = String(UI_WIDTH) + "x" + String(UI_HEIGHT);
    doc["sd"]       = sdStore.mounted();

    // Boardvariante: entscheidet serverseitig, welches OTA-Abbild das Geraet
    // bekommt. Die beiden Varianten haben unterschiedliche Displaytreiber -
    // das falsche Abbild ergaebe ein schwarzes Geraet ohne Bedienung.
#if defined(BOARD_WAVESHARE_35B)
    doc["board"] = "35b";
#else
    doc["board"] = "35";
#endif

    send(doc);
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
    if (_closePortal) stopPortal();

    if (_portalActive) {
        handlePortal();
        // Ein von Hand geoeffnetes Portal ist der Endzustand: dort wird
        // eingerichtet, sonst nichts.
        if (!_portalAuto) return;
        // Ein selbsttaetig geoeffnetes dagegen laeuft nebenher weiter - der
        // Rest dieser Funktion versucht unveraendert, die gespeicherten
        // Zugangsdaten wieder zum Laufen zu bringen. Ohne das war das Portal
        // eine Sackgasse: ein Router, der laenger als
        // PORTAL_FALLBACK_AFTER_MS zum Hochfahren braucht (oder ein
        // Container, der gerade aktualisiert wird), liess das Terminal
        // dauerhaft in der Einrichtung stehen, obwohl Minuten spaeter alles
        // wieder da war. Nur ein Griff zum Netzstecker holte es zurueck.
    }

    if (WiFi.status() != WL_CONNECTED) {
        _wsConnected = false;
        trackConnectionHealth(false);

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

    trackConnectionHealth(_wsConnected);

    if (!_wsStarted) startSocket();
    ws.loop();
}

// Bewertet einen anhaltenden Ausfall (WLAN oder Server) und eskaliert in zwei
// Stufen - siehe die Zeitleiste in config.h. Beide Stufen laufen genau
// einmal pro Ausfall, nicht bei jedem Loop-Durchlauf.
void Net::trackConnectionHealth(bool fullyConnected) {
    if (fullyConnected) {
        _disconnectedSince = 0;
        _nextSdRetryMs = 0;
        return;
    }

    const uint32_t now = millis();
    if (_disconnectedSince == 0) {
        _disconnectedSince = now;
        _nextSdRetryMs = now + SD_RETRY_AFTER_MS;
        return;
    }
    const uint32_t downFor = now - _disconnectedSince;

    if ((int32_t)(now - _nextSdRetryMs) >= 0) {
        // Nicht nur einmal je Ausfall nachsehen, sondern immer wieder.
        // Genau darum geht es bei diesem Weg: wer vor einem Terminal steht,
        // das den Server nicht mehr findet, schreibt die richtigen Daten auf
        // eine Karte und steckt sie hinein - und das ist naturgemaess
        // *nachdem* der Ausfall begonnen hat. Mit einem einzigen Versuch nach
        // 90 Sekunden kam die Karte praktisch immer zu spaet.
        _nextSdRetryMs = now + SD_RETRY_AFTER_MS;
        log_w("Keine Verbindung seit %u s - Blick auf die SD-Karte", downFor / 1000);

        // Vorher/nachher vergleichen und nur bei echter Aenderung schreiben.
        // Sonst landete bei jedem Durchgang derselbe Inhalt im NVS - der Flash
        // haelt das nicht unbegrenzt aus, und neu verbinden muesste man dabei
        // auch nicht.
        const String vorherSsid = settings.wifiSsid;
        const String vorherPass = settings.wifiPass;
        const String vorherHost = settings.serverHost;
        const uint16_t vorherPort = settings.serverPort;
        const String vorherToken = settings.token;

        if (sdStore.loadSettings() &&
            (settings.wifiSsid != vorherSsid || settings.wifiPass != vorherPass ||
             settings.serverHost != vorherHost || settings.serverPort != vorherPort ||
             settings.token != vorherToken)) {
            settings.save();
            log_i("Zugangsdaten von der SD-Karte uebernommen - Neustart");

            // Neu starten statt im Betrieb umzuschalten.
            //
            // An den Zugangsdaten haengen WLAN-Verbindung, WebSocket-Ziel und
            // Token gleichzeitig. Die von Hand einzeln nachzuziehen hiesse,
            // eine halb aufgebaute Verbindung mitten im Betrieb umzubiegen -
            // fuer einen Weg, der hoechstens einmal im Leben eines Geraets
            // begangen wird, ist das die aufwendigere und unsicherere
            // Loesung. Im NVS steht jetzt alles; nach dem Neustart gilt es
            // von der ersten Zeile an.
            delay(200);
            ESP.restart();
        } else {
            log_i("Keine (neue) Datei auf der SD-Karte - Zugangsdaten unveraendert");
        }
    }

    if (downFor >= PORTAL_FALLBACK_AFTER_MS && !_portalActive) {
        log_w("Weiterhin keine Verbindung nach %u s - Einrichtungsportal wird geoeffnet",
              downFor / 1000);
        startPortal(true);
    }
}

bool Net::wifiConnected() const { return WiFi.status() == WL_CONNECTED; }
String Net::ip() const {
    return _portalActive ? WiFi.softAPIP().toString() : WiFi.localIP().toString();
}
int Net::rssi() const { return WiFi.RSSI(); }
