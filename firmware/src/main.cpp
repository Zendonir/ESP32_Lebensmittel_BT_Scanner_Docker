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
#include <esp_task_wdt.h>

#include "config.h"
#include "core/Net.h"
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
        screen.showBoot("Neustart", "vom Server ausgeloest");
        delay(400);
        ESP.restart();
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

    screen.begin();
    screen.showBoot("Lebensmittel-Terminal", FIRMWARE_VERSION);

    touch.begin();
    buzzer.begin();
    printer.begin();

    net.onMessage(onServerMessage);
    net.begin();

    if (net.portalActive()) {
        screen.showBoot("Einrichtung", String("WLAN ") + AP_SSID + " - 192.168.4.1");
    } else {
        screen.showBoot("Verbinde…", settings.serverHost + ":" + String(settings.serverPort));
        bleScanner.begin();
    }

    // Der Watchdog ist die letzte Instanz: haengt der Loop, gibt es einen
    // Neustart mit Backtrace statt eines Geraets, das nur noch dasteht.
    esp_task_wdt_config_t wdt = {
        .timeout_ms = WDT_TIMEOUT_S * 1000,
        .idle_core_mask = 0,
        .trigger_panic = true,
    };
    esp_task_wdt_reconfigure(&wdt);
    esp_task_wdt_add(nullptr);

    log_i("Start abgeschlossen, Heap frei: %u", ESP.getFreeHeap());
}

// ---------------------------------------------------------------------------
void loop() {
    // Als Erstes fuettern - danach darf jeder Zweig fruehzeitig zurueckkehren,
    // ohne den Watchdog zu reissen.
    esp_task_wdt_reset();

    net.loop();

    if (net.portalActive()) {
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

    // --- Beruehrung -------------------------------------------------------
    int16_t x = 0, y = 0;
    if (touch.pressed(x, y)) {
        const Action action = screen.handleTouch(x, y);
        if (action.type == ActionType::Tap) {
            JsonDocument doc;
            doc["t"] = "tap";
            doc["screen"] = screen.screenId();
            doc["item"] = action.id;
            net.send(doc);
        } else if (action.type == ActionType::Input) {
            // Erst den Wert, dann den Tipp: der Server soll beides in dieser
            // Reihenfolge sehen, sonst speichert er den vorherigen Wert.
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

    if (millis() - lastTelemetry > TELEMETRY_INTERVAL_MS) {
        lastTelemetry = millis();
        sendTelemetry();
    }

    delay(5);   // gibt IDLE-Task und WLAN-Stack Luft
}
