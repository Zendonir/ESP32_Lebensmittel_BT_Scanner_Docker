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

#include "config.h"
#include "core/Board.h"
#include "core/Buttons.h"
#include "core/Net.h"
#include "core/SdStore.h"
#include "core/Settings.h"
#include "printer/Printer.h"
#include "scanner/BLEScanner.h"
#include "ui/Buzzer.h"
#include "ui/Screen.h"
#include "ui/Touch.h"

#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "dev"
#endif

static uint32_t lastTelemetry = 0;

// Ein Scan, der bei getrenntem Server anfaellt, geht nicht verloren: er wartet
// hier, bis die Verbindung steht. Genau ein Platz - mehr braucht niemand, der
// vor dem Geraet steht und auf eine Rueckmeldung wartet.
static String pendingScan;

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
static void onServerMessage(JsonDocument &doc) {
    const String type = String(doc["t"] | "");

    if (type == "screen") {
        screen.apply(doc);

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

    } else if (type == "config") {
        screen.setBrightness(doc["brightness"] | 80);
        buzzer.setEnabled(doc["beep"] | true);

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

    net.send(doc);
}

static void sendScan(const String &code) {
    JsonDocument doc;
    doc["t"] = "scan";
    doc["code"] = code;
    doc["source"] = "ble";
    if (!net.send(doc)) pendingScan = code;
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

    net.onMessage(onServerMessage);
    net.begin();
    esp_task_wdt_reset();

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

    if (net.portalActive()) {
        // Das Portal kann auch mitten im Betrieb aufgehen (anhaltender
        // WLAN-/Server-Ausfall, siehe Net::trackConnectionHealth) - dann fehlt
        // der Bildschirmaufruf aus setup() und das Display wuerde einfach
        // weiter den letzten Zustand zeigen.
        static bool wasPortalActive = false;
        if (!wasPortalActive) {
            screen.showBoot("Einrichtung", String("WLAN ") + AP_SSID + " - 192.168.4.1");
        }
        wasPortalActive = true;
        delay(5);
        return;
    }

    bleScanner.loop();
    buzzer.loop();
    screen.loop();

    // --- Barcode ---------------------------------------------------------
    String code;
    if (bleScanner.readCode(code)) {
        buzzer.play("scan");
        sendScan(code);
    }
    if (!pendingScan.isEmpty() && net.serverConnected()) {
        const String queued = pendingScan;
        pendingScan = "";
        sendScan(queued);
    }

    // --- Hardwaretaster ---------------------------------------------------
    // Gleichwertig zur Bedienung am Bildschirm: Zurueck macht dasselbe wie das
    // Wischen, die beiden anderen dasselbe wie das Ziehen in einer Liste.
    switch (buttons.poll()) {
        case ButtonEvent::Back: {
            JsonDocument doc;
            doc["t"] = "tap";
            doc["screen"] = screen.screenId();
            doc["item"] = "back";
            net.send(doc);
            break;
        }
        case ButtonEvent::ScrollUp:   screen.scrollByRows(-1); break;
        case ButtonEvent::ScrollDown: screen.scrollByRows(1);  break;
        default: break;
    }

    // --- Beruehrung -------------------------------------------------------
    const Gesture gesture = touch.poll(screen.scrollable());
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
            } else if (action.type == ActionType::Tap) {
                JsonDocument doc;
                doc["t"] = "tap";
                doc["screen"] = screen.screenId();
                doc["item"] = action.id;
                net.send(doc);
            } else if (action.type == ActionType::Input) {
                // Erst den Wert, dann den Tipp: der Server soll beides in
                // dieser Reihenfolge sehen, sonst speichert er den vorherigen
                // Wert.
                JsonDocument value;
                value["t"] = "input";
                value["screen"] = screen.screenId();
                value["value"] = action.value;
                net.send(value);

                JsonDocument tap;
                tap["t"] = "tap";
                tap["screen"] = screen.screenId();
                tap["item"] = action.id;
                net.send(tap);
            }
            break;
        }

        case GestureType::SwipeLeft:
        case GestureType::SwipeRight: {
            // Bildschirmweite Zurueck-Geste - "back" existiert als Aktion
            // bereits auf jedem Bildschirm (device/workflow.py::on_tap), die
            // Geste loest also nur aus, was auch der Zurueck-Knopf ausloest.
            JsonDocument doc;
            doc["t"] = "tap";
            doc["screen"] = screen.screenId();
            doc["item"] = "back";
            net.send(doc);
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
        if (jobId > 0) {
            JsonDocument doc;
            doc["t"] = "print_result";
            doc["job"] = jobId;
            doc["ok"] = ok;
            doc["error"] = error;
            net.send(doc);
        }
    }

    // --- Zustandsanzeige und Telemetrie ------------------------------------
    if (!net.serverConnected()) {
        screen.setBanner(net.wifiConnected() ? "Kein Server" : "Kein WLAN");
    } else {
        screen.setBanner("");
    }

    // Einen Zustandswechsel des Scanners sofort melden statt auf das naechste
    // Telemetriepaket zu warten - sonst stuende bis zu 30 s lang "Getrennt"
    // auf dem Bildschirm, obwohl der Scanner laengst dranhaengt.
    static bool lastScannerState = false;
    if (bleScanner.isConnected() != lastScannerState) {
        lastScannerState = bleScanner.isConnected();
        lastTelemetry = millis();
        sendTelemetry();
    }

    if (millis() - lastTelemetry > TELEMETRY_INTERVAL_MS) {
        lastTelemetry = millis();
        sendTelemetry();
    }

    delay(5);   // gibt IDLE-Task und WLAN-Stack Luft
}
