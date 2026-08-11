#pragma once

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// BLE-HID-Barcodescanner.
//
// Der einzige Teil des Vorgaengerprojekts, der praktisch unveraendert bleibt -
// er hat funktioniert. Uebernommen wurden auch seine beiden Haerten:
//   * `_connecting` wird auf JEDEM Ausstiegspfad zurueckgesetzt, nicht nur in
//     onDisconnect. Sonst bleibt das Geraet fuer immer im Zustand "verbinde…".
//   * Ein Verbindungsversuch bricht nach 30 s ab.
class BLEScanner {
public:
    void begin();
    void loop();

    // Fertigen Barcode abholen (durch Enter abgeschlossen). true = es gab einen.
    bool readCode(String &code);

    bool isConnected() const { return _connected; }
    bool isConnecting() const { return _connecting; }
    int  battery() const { return _battery; }
    String deviceName() const { return _deviceName; }

    void disconnect();
    void forget();

    // Sofortigen Neuversuch erzwingen (System-Panel, "Verbinden"), statt auf
    // die gestaffelte Rueckversuchszeit nach einem Fehlschlag zu warten.
    void retryNow() { _nextTryMs = 0; }

    // Aus NimBLE-Rueckrufen aufgerufen - nicht direkt verwenden.
    void _onDisconnect();
    void _onReport(const uint8_t *data, size_t length);
    void _onBattery(uint8_t level);

private:
    void scanAndConnect();
    void readBatteryNow();

    volatile bool _connected  = false;
    volatile bool _connecting = false;
    volatile uint32_t _connectStartedMs = 0;

    String _deviceName;
    String _address;
    String _buffer;            // Zeichen bis zum Enter
    String _pending;           // fertiger Code, wartet auf readCode()
    int    _battery = -1;

    uint32_t _nextTryMs   = 0;
    uint32_t _nextBattMs  = 0;
    uint8_t  _failures    = 0;

    SemaphoreHandle_t _mutex = nullptr;
};

extern BLEScanner bleScanner;
