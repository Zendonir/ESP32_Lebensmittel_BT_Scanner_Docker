#pragma once

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include "config.h"

// BLE-HID-Barcodescanner.
//
// Uebernommen aus dem Vorgaengerprojekt, mit seinen beiden Haerten:
//   * Der Zustand wird auf JEDEM Ausstiegspfad zurueckgesetzt, nicht nur in
//     onDisconnect. Sonst bleibt das Geraet fuer immer im Zustand "verbinde…".
//   * Ein Verbindungsversuch bricht nach 30 s ab.
//
// Schnell gekoppelt wird mit drei Dingen: der bekannte Scanner wird nicht
// gesucht, sondern ohne Zeitgrenze erwartet (AutoConnect); die Verschluesselung
// startet im selben Augenblick wie die Verbindung; und solange Dienste gesucht
// und abonniert werden, laeuft die Verbindung mit kurzem Intervall ohne
// Latenz. Erst danach wird auf sparsame Parameter umgestellt.
//
// Anders als dort laeuft die Kopplung hier nicht in einem eigenen Task,
// sondern als Zustandsmaschine im Hauptloop - siehe State weiter unten.
class BLEScanner {
public:
    void begin();
    void loop();

    // Fertigen Barcode abholen (durch Enter abgeschlossen). true = es gab einen.
    bool readCode(String &code);

    bool isConnected() const { return _state == State::Connected; }
    bool isConnecting() const {
        return _state == State::Scanning || _state == State::Connecting ||
               _state == State::Discovering || _state == State::Securing;
    }

    // Kurzbeschreibung fuer die Telemetrie: verbunden, wartet, sucht,
    // verbindet, pausiert, aus.
    const char *stateName() const;

    // Wie lange der Scanner nach dem letzten Barcode verbunden bleibt.
    // 0 = das Terminal trennt nie (der Scanner kann trotzdem selbst schlafen
    // gehen). Kommt vom Server (device_ui.scanner_minutes).
    void setIdleTimeout(uint32_t ms) { _idleTimeoutMs = ms; }
    uint32_t idleTimeoutMs() const { return _idleTimeoutMs; }

    // Grund der letzten Trennung als HCI-Code (0x13 = vom Scanner beendet,
    // 0x08 = Funkverbindung abgerissen, 0x16 = vom Terminal beendet, ...),
    // 0 = noch keine. Und wie lange die letzte Kopplung vom Verbindungsaufbau
    // bis "bereit" gedauert hat - beides fuer das System-Panel.
    int lastReason() const { return _lastReason; }
    uint32_t lastSetupMs() const { return _setupMs; }
    int  battery() const { return _battery; }
    String deviceName() const { return _deviceName; }

    void disconnect();
    void forget();

    // Sofortigen Neuversuch erzwingen (System-Panel, "Verbinden"), statt auf
    // die gestaffelte Rueckversuchszeit nach einem Fehlschlag zu warten.
    void retryNow() {
        _nextTryMs    = 0;
        _pauseUntilMs = 0;
        _failures     = 0;
    }

    // Aus NimBLE-Rueckrufen aufgerufen - nicht direkt verwenden.
    // Die Rueckrufe laufen auf dem NimBLE-Host-Task, nicht im Hauptloop:
    // sie setzen deshalb nur den Zustand und arbeiten selbst nichts ab.
    void _onDisconnect(int reason);
    void _onConnect();
    void _onSecured() { _secured = true; }
    void _onConnectFail();
    void _onReport(const uint8_t *data, size_t length);
    void _onBattery(uint8_t level);

private:
    // Kopplung als Zustandsmaschine, damit der Hauptloop nie blockiert.
    //
    //   Idle ──bekannt?──> AutoConnect ─┐
    //     └──unbekannt──>  Scanning ────┴─> Connecting -> Discovering -> Connected
    //
    // AutoConnect ist der Normalfall: ein gerichteter Verbindungswunsch ohne
    // Zeitgrenze. Den haelt der Controller selbst offen und stellt die
    // Verbindung in dem Augenblick her, in dem der Scanner sich meldet - ohne
    // dass die Firmware etwas tun muesste. Schneller geht es nicht, und es
    // belegt deutlich weniger Funkzeit als staendiges Suchen, was neben WLAN
    // im selben 2,4-GHz-Band spuerbar ist.
    //
    // Gesucht wird nur, wenn noch keine Kopplung besteht - oder wenn die
    // gespeicherte offensichtlich nicht mehr stimmt (siehe
    // BLE_AUTOCONNECT_RETRY_MS).
    //
    // Nach dem Verbindungsaufbau: Discovering (Dienste suchen) -> Securing
    // (auf die Verschluesselung warten, die seit onConnect im Hintergrund
    // laeuft) -> Connected (abonniert, sparsame Verbindungsparameter).
    enum class State : uint8_t {
        Idle, AutoConnect, Scanning, Connecting, Discovering, Securing, Connected
    };

    void startAutoConnect();
    void startScan();
    void beginConnect();
    void discoverServices();   // Dienstsuche, einmalig nach erfolgreicher Verbindung
    void completeSetup();      // abonnieren, aufraeumen, auf sparsam umstellen
    void backoff(uint32_t delayMs = 0);
    void readBatteryNow();
    bool pushCode(const String &code);   // aus dem NimBLE-Rueckruf

    volatile State _state = State::Idle;
    volatile uint32_t _connectStartedMs = 0;

    // Im Einrichtungsportal wird begin() nie aufgerufen. Ohne diese Sperre
    // wuerde loop() NimBLE ansprechen, bevor es initialisiert ist.
    bool _started = false;

    String _deviceName;
    String _address;
    String _buffer;            // Zeichen bis zum Enter (nur NimBLE-Task)

    // Fertige Codes, die auf readCode() warten.
    //
    // Vorher stand hier ein einzelner String, und readCode() sah ihn ohne
    // Mutex an ("isEmpty()"), waehrend der NimBLE-Task ihn gerade zuwies. Eine
    // String-Zuweisung gibt den alten Puffer frei und legt einen neuen an -
    // der Hauptloop las also unter Umstaenden auf einen bereits freigegebenen
    // Zeiger. Das faellt nicht als Absturz an der Fundstelle auf, sondern
    // spaeter als zerschossener Heap an voellig anderer Stelle.
    //
    // Jetzt: jeder Zugriff auf die Eintraege laeuft unter dem Mutex, und die
    // Zaehlvariable ist volatile, damit der schnelle Vorabblick in readCode()
    // ohne Mutex gueltig bleibt. Vier Plaetze statt einem, damit ein zweiter
    // Barcode nicht den ersten ueberschreibt, falls der Hauptloop gerade
    // einen Druckauftrag abarbeitet.
    static constexpr uint8_t CODE_QUEUE = 4;
    String   _codes[CODE_QUEUE];
    uint8_t  _codeHead = 0;
    volatile uint8_t _codeCount = 0;

    int    _battery = -1;

    uint32_t _nextTryMs   = 0;
    uint32_t _nextBattMs  = 0;
    uint8_t  _failures    = 0;

    // Von Hand getrennt: ohne diese Sperre wuerde sich der Scanner nach der
    // ueblichen Wartezeit sofort wieder verbinden und der Knopf
    // "Verbinden / Trennen" bliebe wirkungslos. Dient auch als Ruhepause nach
    // dem Trennen wegen Untaetigkeit.
    uint32_t _pauseUntilMs = 0;

    // Letzter Tastendruck des Scanners. Jeder Scan setzt ihn zurueck; bleibt
    // er zu lange her, wird die Verbindung getrennt (_idleTimeoutMs).
    // Wird aus dem NimBLE-Rueckruf geschrieben, deshalb volatile.
    volatile uint32_t _lastActivityMs = 0;

    // Naechster Durchlauf soll suchen statt auf die bekannte Kopplung zu
    // warten - gesetzt, wenn das gerichtete Warten zu lange erfolglos war.
    bool _forceScan = false;

    uint32_t _idleTimeoutMs = BLE_IDLE_TIMEOUT_MS;
    volatile bool     _secured = false;   // aus dem NimBLE-Rueckruf
    volatile int      _lastReason = 0;
    volatile uint32_t _linkUpMs = 0;      // Verbindung steht (onConnect)
    uint32_t _securingSinceMs = 0;
    uint32_t _setupMs = 0;

    SemaphoreHandle_t _mutex = nullptr;
};

extern BLEScanner bleScanner;
