#pragma once

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// BLE-HID-Barcodescanner.
//
// Uebernommen aus dem Vorgaengerprojekt, mit seinen beiden Haerten:
//   * Der Zustand wird auf JEDEM Ausstiegspfad zurueckgesetzt, nicht nur in
//     onDisconnect. Sonst bleibt das Geraet fuer immer im Zustand "verbinde…".
//   * Ein Verbindungsversuch bricht nach 30 s ab.
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
               _state == State::Discovering;
    }
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
    void _onDisconnect();
    void _onConnect();
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
    enum class State : uint8_t {
        Idle, AutoConnect, Scanning, Connecting, Discovering, Connected
    };

    void startAutoConnect();
    void startScan();
    void beginConnect();
    void finishConnect();      // Dienstsuche, einmalig nach erfolgreicher Verbindung
    void backoff(uint32_t delayMs = 0);
    void readBatteryNow();

    volatile State _state = State::Idle;
    volatile uint32_t _connectStartedMs = 0;

    // Im Einrichtungsportal wird begin() nie aufgerufen. Ohne diese Sperre
    // wuerde loop() NimBLE ansprechen, bevor es initialisiert ist.
    bool _started = false;

    String _deviceName;
    String _address;
    String _buffer;            // Zeichen bis zum Enter
    String _pending;           // fertiger Code, wartet auf readCode()
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
    // er zu lange her, wird die Verbindung getrennt (BLE_IDLE_TIMEOUT_MS).
    // Wird aus dem NimBLE-Rueckruf geschrieben, deshalb volatile.
    volatile uint32_t _lastActivityMs = 0;

    // Naechster Durchlauf soll suchen statt auf die bekannte Kopplung zu
    // warten - gesetzt, wenn das gerichtete Warten zu lange erfolglos war.
    bool _forceScan = false;

    SemaphoreHandle_t _mutex = nullptr;
};

extern BLEScanner bleScanner;
