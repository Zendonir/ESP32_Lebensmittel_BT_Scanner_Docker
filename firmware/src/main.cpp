// ============================================================================
// Lebensmittel-Terminal - schlanke Firmware
//
// Aufgabe des Geraets in einem Satz: Barcodes lesen, Bildschirme anzeigen,
// Etiketten drucken. Alles andere macht der Docker-Container.
//
// Der gesamte Ablauf laeuft in einem einzigen Loop auf Core 1. Es gibt keine
// eigenen Tasks fuer Touch, Sync oder Netzwerk mehr - und damit auch keine der
// Wettlaeufe, die im Vorgaengerprojekt zu haengenden Zustandsflags gefuehrt
// haben. Die einzigen Nebenlaeufer sind die NimBLE- und WiFi-Stacks selbst.
// ============================================================================

#include <Arduino.h>
#include <HTTPUpdate.h>
#include <WiFiClient.h>
#include <esp_system.h>    // esp_reset_reason()
#include <esp_task_wdt.h>
#include <vector>

#include "config.h"
#include "core/Board.h"
#include "core/Buttons.h"
#include "core/Net.h"
#include "core/OfflineQueue.h"
#include "core/SdStore.h"
#include "core/Settings.h"
#include "printer/Printer.h"
#include "scanner/BLEScanner.h"
#include "ui/Buzzer.h"
#include "ui/Screen.h"
#include "ui/Touch.h"
#include "usb/UsbHost.h"

#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "dev"
#endif

static uint32_t lastTelemetry = 0;

// ---------------------------------------------------------------------------
// Offline-Betrieb
//
// Faellt WLAN oder Server weg, bleibt das Terminal benutzbar: Scans landen im
// OfflineQueue (NVS, uebersteht Neustarts) und der Bildschirm zeigt, was
// gesammelt wurde. Steht die Verbindung wieder, geht alles als eine
// "offline"-Nachricht hinaus; der Server lagert Etiketten aus und legt
// Produkt-Scans unter "Nachtragen" ab. Was ein Code bedeutet, entscheidet
// weiterhin der Server - hier wird nur gesammelt.
// ---------------------------------------------------------------------------
static constexpr int LOCAL_SCREEN_ID = -1;   // Bildschirm aus der Firmware
static String   labelPrefix   = "LEB";       // kommt mit config vom Server
static String   lastLocation;                // Ort aus dem letzten Server-Bildschirm
static uint32_t lostAt        = 0;           // seit wann ohne Server (0 = verbunden)
static uint32_t connectedAt   = 0;           // seit wann wieder verbunden
static bool     offlineShown  = false;
static uint32_t batchSentAt   = 0;           // 0 = keine Quittung ausstehend
static size_t   batchSize     = 0;

// Druckergebnisse, die ohne Verbindung anfielen.
struct PrintResult { int job; bool ok; String error; };
static std::vector<PrintResult> pendingResults;

static void showOfflineScreen() {
    JsonDocument doc;
    doc["id"] = LOCAL_SCREEN_ID;
    doc["kind"] = "list";
    doc["title"] = "Offline";
    String sub = net.wifiConnected() ? "Server nicht erreichbar" : "Kein WLAN";
    if (net.portalActive()) sub += String(" - Einrichtung: WLAN ") + AP_SSID;
    doc["subtitle"] = sub;

    JsonObject status = doc["status"].to<JsonObject>();
    status["location"] = lastLocation;
    status["scanner"] = bleScanner.isConnected();
    status["battery"] = bleScanner.battery();

    JsonArray items = doc["items"].to<JsonArray>();
    JsonObject info = items.add<JsonObject>();
    info["header"] = true;
    info["label"] = offlineQueue.size()
        ? String(offlineQueue.size()) + " gespeichert - wird verbucht, sobald der Server da ist"
        : String("Weiter scannen: Etikett = auslagern, Produkt = spaeter nachtragen");

    // Neueste zuerst - das ist der Scan, den man gerade gemacht hat.
    const auto &list = offlineQueue.items();
    for (int i = (int)list.size() - 1; i >= 0; i--) {
        const bool etikett = labelPrefix.length() && list[i].code.startsWith(labelPrefix);
        JsonObject o = items.add<JsonObject>();
        o["id"] = "__local_none";
        o["label"] = list[i].code;
        o["sub"] = etikett ? "wird ausgelagert" : "Produkt - wird nachgetragen";
        o["color"] = etikett ? "#2eb048" : "#fb8c00";
    }
    screen.apply(doc);
    offlineShown = true;
}

// ---------------------------------------------------------------------------
static void logResetReason() {
    const esp_reset_reason_t reason = esp_reset_reason();
    static const char *names[] = {
        "unbekannt", "Kaltstart", "extern", "Software", "Panic", "Interrupt-WDT",
        "Task-WDT", "anderer WDT", "Deep-Sleep", "Brownout", "SDIO",
    };
    const char *name = (reason < sizeof(names) / sizeof(names[0])) ? names[reason] : "?";
    if (reason == ESP_RST_PANIC || reason == ESP_RST_TASK_WDT ||
        reason == ESP_RST_INT_WDT || reason == ESP_RST_BROWNOUT) {
        log_e("Vorheriger Neustart: %s", name);   // Ursache steht im Server-Log
    } else {
        log_i("Vorheriger Neustart: %s", name);
    }
}

// ---------------------------------------------------------------------------
// Firmware-Update
//
// Das Abbild liegt beim Server; der Pfad kommt in der ota-Nachricht, Host und
// Port sind dieselben wie fuer den WebSocket. Der Ablauf blockiert bewusst -
// waehrend eines Updates soll ohnehin nichts anderes passieren -, deshalb wird
// der Watchdog im Fortschrittsrueckruf ausdruecklich gefuettert. Ohne das
// wuerde ein langsamer Download mitten im Schreiben einen Neustart ausloesen
// und eine halb geschriebene Partition hinterlassen.
// ---------------------------------------------------------------------------
static void runOta(const String &path, const String &version) {
    if (path.isEmpty()) return;

    screen.showBoot("Firmware-Update", version.isEmpty() ? "wird geladen…" : version);
    bleScanner.disconnect();   // Funk und Rechenzeit dem Download ueberlassen

    WiFiClient client;
    httpUpdate.rebootOnUpdate(true);
    httpUpdate.onProgress([](int done, int total) {
        esp_task_wdt_reset();
        static int lastPercent = -1;
        const int percent = total > 0 ? (done * 100 / total) : 0;
        if (percent == lastPercent) return;      // nur bei echter Aenderung zeichnen
        lastPercent = percent;
        screen.showBoot("Firmware-Update", String(percent) + " %");
    });

    const String url = String("http://") + settings.serverHost + ":" +
                       String(settings.serverPort) + path + "?token=" + settings.token;

    const t_httpUpdate_return result = httpUpdate.update(client, url);
    // Bei Erfolg startet das Geraet in rebootOnUpdate() neu und kehrt hier nie
    // zurueck - alles Weitere ist also ein Fehlschlag.
    const String reason = httpUpdate.getLastErrorString();
    log_e("Firmware-Update fehlgeschlagen (%d): %s", (int)result, reason.c_str());
    screen.showBoot("Update fehlgeschlagen", reason);
    delay(2500);

    // Weiterlaufen statt neu starten: die alte Firmware ist unversehrt, das
    // Geraet bleibt bedienbar.
    net.sendEvent("ota_failed");
    screen.showBoot("Verbinde…", settings.serverHost + ":" + String(settings.serverPort));
}

// ---------------------------------------------------------------------------
// Nachrichten vom Server
// ---------------------------------------------------------------------------
// Ergebnis eines Druckauftrags melden. Ohne Verbindung merken: sonst haelt
// der Server den Auftrag nach einer Weile fuer verloren und druckt ihn ein
// zweites Mal.
static void reportPrint(int jobId, bool ok, const String &error) {
    JsonDocument doc;
    doc["t"] = "print_result";
    doc["job"] = jobId;
    doc["ok"] = ok;
    doc["error"] = error;
    if (!net.send(doc) && pendingResults.size() < 16) {
        pendingResults.push_back({jobId, ok, error});
    }
}

static void onServerMessage(JsonDocument &doc) {
    const String type = String(doc["t"] | "");

    if (type == "screen") {
        screen.apply(doc);
        offlineShown = false;
        const String ort = screen.statusLocation();
        if (!ort.isEmpty()) lastLocation = ort;

    } else if (type == "offline_ack") {
        // Erst jetzt ist der gesammelte Stand verbucht.
        const size_t n = doc["n"] | 0;
        offlineQueue.drop(min(n, batchSize));
        batchSentAt = 0;
        batchSize = 0;

    } else if (type == "toast") {
        screen.toast(String(doc["text"] | ""), String(doc["level"] | "info"));

    } else if (type == "beep") {
        buzzer.play(String(doc["pattern"] | "ok"));

    } else if (type == "print") {
        const int jobId = doc["job"] | 0;
        if (!printer.enqueue(jobId, doc)) {
            // Warteschlange voll - dem Server sofort Bescheid geben, damit er
            // den Auftrag erneut einreiht statt ihn als gedruckt zu fuehren.
            JsonDocument reply;
            reply["t"] = "print_result";
            reply["job"] = jobId;
            reply["ok"] = false;
            reply["error"] = "Warteschlange voll";
            net.send(reply);
        }

    } else if (type == "usbdruck") {
        // Brother-Raster fuer den Drucker am USB, in Stuecken. Gebaut hat es
        // der Server; hier wird es nur gesammelt und weitergereicht.
        const int jobId = doc["job"] | 0;
        String error;
        if (!usbHost.addChunk(jobId, doc["teil"] | 0, doc["letzter"] | false,
                              doc["daten"] | "", error)) {
            reportPrint(jobId, false, error);
        }

    } else if (type == "config") {
        screen.setBrightness(doc["brightness"] | 80);
        buzzer.setEnabled(doc["beep"] | true);
        screen.setIdleDim(doc["idle_seconds"] | 0);
        // Nur uebernehmen, wenn der Server es schickt - ein aelterer kennt
        // die Einstellung nicht, dann bleibt es beim Startwert.
        JsonVariant minuten = doc["scanner_minutes"];
        if (minuten.is<float>() || minuten.is<int>()) {
            const float m = max(0.0f, minuten.as<float>());
            bleScanner.setIdleTimeout((uint32_t)(m * 60000.0f));
        }
        if (doc["label_prefix"].is<const char *>()) labelPrefix = String(doc["label_prefix"] | "");
        // Drucker und Scanner am USB, je Terminal im Web eingestellt. Nur die
        // config der eigenen Verbindung traegt die Felder.
        if (doc["drucker"].is<const char *>()) {
            const bool usb = String(doc["drucker"] | "") == "usb" || (doc["usb_scanner"] | false);
            settings.saveUsbHost(usb);
            if (usb && !usbHost.active()) usbHost.begin();
            // Abschalten wirkt erst nach einem Neustart - der Host laeuft bis
            // dahin weiter, die Telemetrie zeigt es.
        }

    } else if (type == "ping") {
        net.sendEvent("pong");

    } else if (type == "reboot") {
        screen.showBoot("Neustart", "vom Server ausgelöst");
        delay(400);
        ESP.restart();

    } else if (type == "ota") {
        runOta(String(doc["path"] | ""), String(doc["version"] | ""));
    }
}

// ---------------------------------------------------------------------------
static void sendTelemetry() {
    JsonDocument doc;
    doc["t"]        = "telemetry";
    doc["heap"]     = ESP.getFreeHeap();
    doc["min_heap"] = ESP.getMinFreeHeap();
    doc["psram"]    = ESP.getFreePsram();
    doc["rssi"]     = net.rssi();
    doc["uptime"]   = millis() / 1000;

    JsonObject scanner = doc["scanner"].to<JsonObject>();
    scanner["connected"] = bleScanner.isConnected();
    scanner["battery"]   = bleScanner.battery();
    scanner["name"]      = bleScanner.deviceName();
    // Fuer das System-Panel: was der Scanner gerade tut, warum er zuletzt
    // getrennt wurde (vom Scanner selbst? Funkloch?) und wie lange die
    // letzte Kopplung gedauert hat.
    scanner["state"]     = bleScanner.stateName();
    scanner["reason"]    = bleScanner.lastReason();
    scanner["setup_ms"]  = bleScanner.lastSetupMs();
    scanner["idle_min"]  = bleScanner.idleTimeoutMs() / 60000;

    JsonObject usb = doc["usb"].to<JsonObject>();
    usb["aktiv"]  = usbHost.active();
    usb["geraet"] = usbHost.deviceKind();
    usb["name"]   = usbHost.deviceName();
    usb["wunsch"] = settings.usbHost;   // weicht ab: Neustart noetig

    net.send(doc);
}

static void storeOffline(const String &code) {
    if (!offlineQueue.add(code, lastLocation)) {
        buzzer.play("error");
        screen.toast("Offline-Speicher voll - bitte Verbindung herstellen", "error");
        return;
    }
    buzzer.play("ok");
    if (offlineShown) showOfflineScreen();
    screen.toast("Gespeichert - wird spaeter verbucht", "warn");
}

static void sendScan(const String &code, const char *source) {
    // Solange noch Gesammeltes aussteht, hinten anstellen - sonst kaeme ein
    // neuer Scan vor aelteren beim Server an.
    if (!net.serverConnected() || offlineQueue.size() > 0) {
        storeOffline(code);
        return;
    }
    JsonDocument doc;
    doc["t"] = "scan";
    doc["code"] = code;
    doc["source"] = source;
    if (net.send(doc)) screen.setBusy(true);
    else storeOffline(code);
}

// Tipp an den Server. Ohne Verbindung sagt das Geraet das sofort, statt den
// Tipp stillschweigend zu verschlucken.
static void sendTap(const String &item) {
    if (!net.serverConnected()) {
        screen.toast("Keine Verbindung - Scannen geht weiter", "warn");
        return;
    }
    JsonDocument doc;
    doc["t"] = "tap";
    doc["screen"] = screen.screenId();
    doc["item"] = item;
    if (net.send(doc)) screen.setBusy(true);
}

// Offline Gesammeltes nachreichen, sobald die Verbindung eine Weile steht
// (der Server hat dann hello und Startbildschirm hinter sich).
static void syncOffline() {
    if (offlineQueue.size() == 0 || !net.serverConnected()) return;
    if (millis() - connectedAt < 1500) return;
    if (batchSentAt && millis() - batchSentAt < 15000) return;   // Quittung abwarten
    JsonDocument doc;
    const size_t n = offlineQueue.buildBatch(doc, 20);
    if (n && net.send(doc)) {
        batchSentAt = millis() | 1;
        batchSize = n;
        log_i("%u Offline-Scans an den Server", (unsigned)n);
    }
}

// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(150);
    logResetReason();

    // Der Watchdog muss stehen, BEVOR irgendetwas laeuft, das haengen kann
    // (SD-Karte, Display-Reset, WLAN) - vorher stand er erst ganz am Ende von
    // setup(), also ungeschuetzt genau in den Schritten, die am ehesten
    // haengen bleiben. Ohne Watchdog wird aus einem haengenden SD_MMC.begin()
    // (z.B. bei einer angeschlagenen Karte) ein echtes Einfrieren statt eines
    // Neustarts mit Backtrace.
    esp_task_wdt_config_t wdt = {
        .timeout_ms = WDT_TIMEOUT_S * 1000,
        .idle_core_mask = 0,
        .trigger_panic = true,
    };
    esp_task_wdt_reconfigure(&wdt);
    esp_task_wdt_add(nullptr);

    // Zwingend als Naechstes: auf diesem Board haengen die Reset-Leitungen von
    // Display und Touchcontroller am TCA9554-Portexpander, nicht an einem GPIO.
    // Ohne diesen Schritt zeigt der ST7796 Rauschen und der FT6336 meldet sich
    // gar nicht erst auf dem I2C-Bus.
    board.begin();
    esp_task_wdt_reset();

    // Einmaliger Erkennungsversuch fuers System-Panel - unabhaengig davon, ob
    // die Karte gleich fuer Settings::begin() gebraucht wird.
    sdStore.probe();
    esp_task_wdt_reset();

    screen.begin();
    screen.showBoot("Lebensmittel-Terminal", FIRMWARE_VERSION);
    esp_task_wdt_reset();

    touch.begin();
    buttons.begin();
    buzzer.begin();
    printer.begin();
    esp_task_wdt_reset();

    offlineQueue.begin();
    net.onMessage(onServerMessage);
    net.begin();
    esp_task_wdt_reset();

    // Erst jetzt: ab hier ist die serielle Konsole ueber USB stumm.
    if (settings.usbHost) usbHost.begin();

    if (net.portalActive()) {
        screen.showBoot("Einrichtung", String("WLAN ") + AP_SSID + " - 192.168.4.1");
    } else {
        screen.showBoot("Verbinde…", settings.serverHost + ":" + String(settings.serverPort));
        bleScanner.begin();
    }

    log_i("Start abgeschlossen, Heap frei: %u", ESP.getFreeHeap());
}

// ---------------------------------------------------------------------------
void loop() {
    // Als Erstes fuettern - danach darf jeder Zweig fruehzeitig zurueckkehren,
    // ohne den Watchdog zu reissen.
    esp_task_wdt_reset();

    net.loop();

    // Das Portal kann auch mitten im Betrieb aufgehen (anhaltender
    // WLAN-/Server-Ausfall, siehe Net::trackConnectionHealth) - dann fehlt der
    // Bildschirmaufruf aus setup() und das Display wuerde einfach weiter den
    // letzten Zustand zeigen. Und es kann sich von selbst wieder schliessen,
    // wenn die Verbindung zurueckkommt; dann muss der Bildschirm ebenso
    // umgeschaltet werden, sonst stuende dort weiter eine Einrichtungsadresse,
    // die es gar nicht mehr gibt.
    //
    // Nur das von Hand geoeffnete Portal uebernimmt den Bildschirm. Das nach
    // einem langen Ausfall selbsttaetig geoeffnete lief frueher genauso -
    // und legte damit Scanner und Touch still: nach drei Minuten ohne WLAN
    // war das Terminal nicht mehr zu bedienen. Jetzt laeuft es nebenher, der
    // Offline-Schirm nennt es.
    static bool wasPortalActive = false;
    if (net.portalActive() && !net.portalAuto()) {
        if (!wasPortalActive) {
            screen.showBoot("Einrichtung", String("WLAN ") + AP_SSID + " - 192.168.4.1");
            wasPortalActive = true;
        }
        delay(5);
        return;
    }
    if (wasPortalActive) {
        wasPortalActive = false;
        screen.showBoot("Verbinde…", settings.serverHost + ":" + String(settings.serverPort));
        // Waehrend des Portals lief kein BLE-Loop; ein Scanner, der in der
        // Zwischenzeit abgeschaltet wurde, soll sofort wieder erwartet werden.
        bleScanner.retryNow();
    }

    // --- Verbindung / Offline ---------------------------------------------
    if (net.serverConnected()) {
        if (lostAt) connectedAt = millis();
        lostAt = 0;
        syncOffline();
    } else {
        if (!lostAt) lostAt = millis() | 1;
        batchSentAt = 0;                 // Quittung kommt nicht mehr
        if (!offlineShown && millis() - lostAt > OFFLINE_AFTER_MS) showOfflineScreen();
    }
    // Antwortet der Server nicht mehr, ist das Warten zu beenden - sonst
    // steht der Hinweis ewig da.
    if (screen.busyTooLong()) {
        screen.setBusy(false);
        screen.toast(net.serverConnected() ? "Server antwortet nicht" : "Keine Verbindung", "warn");
    }

    bleScanner.loop();
    usbHost.loop();
    buzzer.loop();
    screen.loop();

    // --- Barcode ---------------------------------------------------------
    String code;
    if (bleScanner.readCode(code)) {
        screen.wake();
        net.noteUserActivity();
        buzzer.play("scan");
        sendScan(code, "ble");
    }
    if (usbHost.readCode(code)) {
        screen.wake();
        net.noteUserActivity();
        buzzer.play("scan");
        sendScan(code, "usb");
    }

    // --- Hardwaretaster ---------------------------------------------------
    // Gleichwertig zur Bedienung am Bildschirm: Zurueck macht dasselbe wie das
    // Wischen, die beiden anderen dasselbe wie das Ziehen in einer Liste.
    ButtonEvent taste = buttons.poll();
    if (taste != ButtonEvent::None) net.noteUserActivity();
    if (taste != ButtonEvent::None && screen.wake()) taste = ButtonEvent::None;  // weckt nur
    switch (taste) {
        case ButtonEvent::Back:
            if (screen.screenId() != LOCAL_SCREEN_ID) sendTap("back");
            break;
        case ButtonEvent::ScrollUp:   screen.scrollByRows(-1); break;
        case ButtonEvent::ScrollDown: screen.scrollByRows(1);  break;
        default: break;
    }

    // --- Beruehrung -------------------------------------------------------
    Gesture gesture = touch.poll(screen.scrollable());
    if (gesture.type != GestureType::None) net.noteUserActivity();
    // Ein dunkler Bildschirm wird von der ersten Beruehrung nur geweckt -
    // sonst loeste man beim Aufwecken blind eine Kachel aus, die man gar
    // nicht gesehen hat.
    if (gesture.type != GestureType::None && screen.wake()) gesture.type = GestureType::None;
    switch (gesture.type) {
        case GestureType::Tap: {
            const Action action = screen.handleTouch(gesture.x, gesture.y);
            if (action.type == ActionType::Tap && action.id == "__local_wifi_setup") {
                // WLAN ist reine Geraetesache (Radio, Zugangsdaten lokal im
                // NVS) - dafuer keine Serverfahrt, das Portal geht direkt auf.
                net.forcePortal();
            } else if (action.type == ActionType::Tap && action.id == "__local_ble_toggle") {
                if (bleScanner.isConnected()) bleScanner.disconnect();
                else bleScanner.retryNow();
            } else if (screen.screenId() == LOCAL_SCREEN_ID) {
                // Offline-Schirm: nur Anzeige. Der Ort laesst sich ohne
                // Server nicht wechseln (die Liste der Orte kennt er).
                if (action.id == "locations") {
                    screen.toast("Ort wechseln geht wieder mit Verbindung", "warn");
                }
            } else if (action.type == ActionType::Tap) {
                sendTap(action.id);
            } else if (action.type == ActionType::Input) {
                if (!net.serverConnected()) {
                    screen.toast("Keine Verbindung - Eingabe nicht gespeichert", "warn");
                    break;
                }
                // Erst den Wert, dann den Tipp: der Server soll beides in
                // dieser Reihenfolge sehen, sonst speichert er den vorherigen
                // Wert.
                JsonDocument value;
                value["t"] = "input";
                value["screen"] = screen.screenId();
                value["value"] = action.value;
                net.send(value);

                sendTap(action.id);
            }
            break;
        }

        case GestureType::SwipeLeft:
        case GestureType::SwipeRight: {
            // Bildschirmweite Zurueck-Geste - "back" existiert als Aktion
            // bereits auf jedem Bildschirm (device/workflow.py::on_tap), die
            // Geste loest also nur aus, was auch der Zurueck-Knopf ausloest.
            if (screen.screenId() != LOCAL_SCREEN_ID) sendTap("back");
            break;
        }

        case GestureType::ScrollDrag:
            screen.scrollBy(gesture.deltaY);
            break;

        default:
            break;
    }

    // --- Drucken ----------------------------------------------------------
    // Genau ein Etikett pro Durchlauf. Der Loop bleibt dadurch reaktionsfaehig,
    // auch wenn zwanzig Etiketten in der Warteschlange stehen.
    if (printer.queued() > 0) {
        bool ok = false;
        String error;
        const int jobId = printer.process(ok, error);
        if (jobId > 0) reportPrint(jobId, ok, error);
    }
    if (usbHost.queued() > 0) {
        bool ok = false;
        String error;
        const int jobId = usbHost.process(ok, error);
        if (jobId > 0) reportPrint(jobId, ok, error);
    }
    if (!pendingResults.empty() && net.serverConnected() && millis() - connectedAt > 1500) {
        for (const PrintResult &r : pendingResults) {
            JsonDocument doc;
            doc["t"] = "print_result";
            doc["job"] = r.job;
            doc["ok"] = r.ok;
            doc["error"] = r.error;
            net.send(doc);
        }
        pendingResults.clear();
    }

    // --- Zustandsanzeige und Telemetrie ------------------------------------
    if (!net.serverConnected()) {
        // Auf dem Offline-Schirm steht es schon im Titel.
        screen.setBanner(offlineShown ? "" : (net.wifiConnected() ? "Kein Server" : "Kein WLAN"));
    } else {
        screen.setBanner("");
    }

    // Einen Zustandswechsel des Scanners sofort melden statt auf das naechste
    // Telemetriepaket zu warten - sonst stuende bis zu 30 s lang "Getrennt"
    // auf dem Bildschirm, obwohl der Scanner laengst dranhaengt.
    static bool lastScannerState = false;
    if (bleScanner.isConnected() != lastScannerState) {
        lastScannerState = bleScanner.isConnected();
        // Mit verbundenem Scanner bleibt der Bildschirm an: wer den Scanner
        // in der Hand hat, will sehen, was er scannt. Nach dem Trennen
        // dunkelt er nach der eingestellten Zeit wieder ab.
        screen.setKeepAwake(lastScannerState);
        lastTelemetry = millis();
        sendTelemetry();
        if (offlineShown) showOfflineScreen();   // Scanner-Punkt aktualisieren
    }

    if (millis() - lastTelemetry > TELEMETRY_INTERVAL_MS) {
        lastTelemetry = millis();
        sendTelemetry();
    }

    delay(5);   // gibt IDLE-Task und WLAN-Stack Luft
}
