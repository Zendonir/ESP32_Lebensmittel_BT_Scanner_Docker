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
    // Vorher lief das in einem Rutsch durch (4 s Scan + bis zu 10 s
    // Verbindungsaufbau) - das Geraet stand waehrenddessen still und nahm
    // keine Beruehrung an, und der Zeitueberschreitungsschutz in loop() konnte
    // gar nicht greifen, weil loop() nie erreicht wurde.
    enum class State : uint8_t { Idle, Scanning, Connecting, Discovering, Connected };

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
    // "Verbinden / Trennen" bliebe wirkungslos.
    uint32_t _pauseUntilMs = 0;

    SemaphoreHandle_t _mutex = nullptr;
};

extern BLEScanner bleScanner;
