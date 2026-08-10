#include "BLEScanner.h"

#include <NimBLEDevice.h>

#include "config.h"

BLEScanner bleScanner;

static constexpr uint16_t SVC_HID = 0x1812;
static constexpr uint16_t CHR_REPORT = 0x2A4D;
static constexpr uint16_t SVC_BATTERY = 0x180F;
static constexpr uint16_t CHR_BATTERY = 0x2A19;

static NimBLEClient *client = nullptr;
static NimBLEAdvertisedDevice targetDevice;
static bool haveTarget = false;

// ---------------------------------------------------------------------------
// HID-Tastatur -> Zeichen (US-Layout, wie ihn die ueblichen Scanner senden)
// ---------------------------------------------------------------------------
static char hidToChar(uint8_t key, bool shift) {
    if (key >= 0x04 && key <= 0x1D) {           // a-z
        const char base = 'a' + (key - 0x04);
        return shift ? (char)toupper(base) : base;
    }
    if (key >= 0x1E && key <= 0x26) {           // 1-9
        static const char *shifted = "!@#$%^&*(";
        return shift ? shifted[key - 0x1E] : (char)('1' + (key - 0x1E));
    }
    if (key == 0x27) return shift ? ')' : '0';

    switch (key) {
        case 0x2D: return shift ? '_' : '-';
        case 0x2E: return shift ? '+' : '=';
        case 0x2F: return shift ? '{' : '[';
        case 0x30: return shift ? '}' : ']';
        case 0x31: return shift ? '|' : '\\';
        case 0x33: return shift ? ':' : ';';
        case 0x34: return shift ? '"' : '\'';
        case 0x36: return shift ? '<' : ',';
        case 0x37: return shift ? '>' : '.';
        case 0x38: return shift ? '?' : '/';
        case 0x2C: return ' ';
        default:   return 0;
    }
}

// ---------------------------------------------------------------------------
// NimBLE-Rueckrufe
// ---------------------------------------------------------------------------
class ClientCallbacks : public NimBLEClientCallbacks {
    void onDisconnect(NimBLEClient *, int reason) override {
        log_w("Scanner getrennt (Grund %d)", reason);
        bleScanner._onDisconnect();
    }
};
static ClientCallbacks clientCallbacks;

class ScanCallbacks : public NimBLEScanCallbacks {
    void onResult(const NimBLEAdvertisedDevice *device) override {
        if (!device->isAdvertisingService(NimBLEUUID(SVC_HID))) return;
        targetDevice = *device;
        haveTarget = true;
        NimBLEDevice::getScan()->stop();
    }
};
static ScanCallbacks scanCallbacks;

static void notifyCallback(NimBLERemoteCharacteristic *chr, uint8_t *data,
                           size_t length, bool) {
    if (chr->getUUID().equals(NimBLEUUID(CHR_BATTERY))) {
        if (length >= 1) bleScanner._onBattery(data[0]);
        return;
    }
    bleScanner._onReport(data, length);
}

// ---------------------------------------------------------------------------
void BLEScanner::begin() {
    _mutex = xSemaphoreCreateMutex();

    NimBLEDevice::init("Lebensmittel-Terminal");
    NimBLEDevice::setPower(ESP_PWR_LVL_P9);
    NimBLEDevice::setSecurityAuth(true, false, true);   // Bonding, kein MITM
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

    client = NimBLEDevice::createClient();
    client->setClientCallbacks(&clientCallbacks, false);
    client->setConnectionParams(12, 12, 0, 200);
    client->setConnectTimeout(10 * 1000);

    log_i("BLE bereit");
}

void BLEScanner::_onDisconnect() {
    _connected = false;
    _connecting = false;          // auch hier, nicht nur im Fehlerpfad
    _battery = -1;
    _nextTryMs = millis() + 2000;
}

void BLEScanner::_onBattery(uint8_t level) {
    if (level <= 100) _battery = level;
}

void BLEScanner::_onReport(const uint8_t *data, size_t length) {
    // Standard-Tastaturreport: [Modifier][reserviert][6 Tasten]
    if (length < 3) return;
    const bool shift = data[0] & 0x22;

    for (size_t i = 2; i < length && i < 8; i++) {
        const uint8_t key = data[i];
        if (key == 0) continue;

        if (key == 0x28 || key == 0x58) {        // Enter / Ziffernblock-Enter
            if (_buffer.length() > 0) {
                if (xSemaphoreTake(_mutex, pdMS_TO_TICKS(20)) == pdTRUE) {
                    _pending = _buffer;
                    xSemaphoreGive(_mutex);
                }
                _buffer = "";
            }
            continue;
        }
        if (key == 0x2A) {                        // Backspace
            if (_buffer.length()) _buffer.remove(_buffer.length() - 1);
            continue;
        }

        const char c = hidToChar(key, shift);
        // Obergrenze gegen einen klemmenden Scanner, der endlos Zeichen sendet.
        if (c && _buffer.length() < 64) _buffer += c;
    }
}

bool BLEScanner::readCode(String &code) {
    if (_pending.isEmpty()) return false;
    if (xSemaphoreTake(_mutex, pdMS_TO_TICKS(20)) != pdTRUE) return false;
    code = _pending;
    _pending = "";
    xSemaphoreGive(_mutex);
    return code.length() > 0;
}

void BLEScanner::scanAndConnect() {
    _connecting = true;
    _connectStartedMs = millis();
    haveTarget = false;

    NimBLEScan *scan = NimBLEDevice::getScan();
    scan->setScanCallbacks(&scanCallbacks, false);
    scan->setActiveScan(true);
    scan->setInterval(100);
    scan->setWindow(80);
    scan->getResults(4 * 1000, false);   // blockierend, aber nur 4 s
    scan->clearResults();

    if (!haveTarget) {
        _connecting = false;             // Ausstiegspfad 1
        _failures++;
        _nextTryMs = millis() + min<uint32_t>(30000, 2000UL * _failures);
        return;
    }

    log_i("Verbinde mit %s", targetDevice.getName().c_str());
    if (!client->connect(&targetDevice)) {
        _connecting = false;             // Ausstiegspfad 2
        _failures++;
        _nextTryMs = millis() + 3000;
        return;
    }

    NimBLERemoteService *hid = client->getService(NimBLEUUID(SVC_HID));
    if (hid == nullptr) {
        client->disconnect();
        _connecting = false;             // Ausstiegspfad 3
        _nextTryMs = millis() + 5000;
        return;
    }

    // Ein Scanner meldet mehrere Report-Charakteristiken (Tastatur, Consumer,
    // Vendor). Alle abonnieren, die es erlauben - welche die Barcodes liefert,
    // unterscheidet sich je Modell.
    int subscribed = 0;
    for (auto &chr : hid->getCharacteristics(true)) {
        if (!chr->getUUID().equals(NimBLEUUID(CHR_REPORT))) continue;
        if (!chr->canNotify()) continue;
        if (chr->subscribe(true, notifyCallback)) subscribed++;
    }

    if (subscribed == 0) {
        client->disconnect();
        _connecting = false;             // Ausstiegspfad 4
        _nextTryMs = millis() + 5000;
        return;
    }

    if (NimBLERemoteService *batt = client->getService(NimBLEUUID(SVC_BATTERY))) {
        if (NimBLERemoteCharacteristic *chr = batt->getCharacteristic(NimBLEUUID(CHR_BATTERY))) {
            if (chr->canNotify()) chr->subscribe(true, notifyCallback);
            if (chr->canRead()) _onBattery(chr->readValue<uint8_t>());
        }
    }

    _deviceName = String(targetDevice.getName().c_str());
    _address = String(targetDevice.getAddress().toString().c_str());
    _connected = true;
    _connecting = false;                 // Erfolgspfad
    _failures = 0;
    _nextBattMs = millis() + BATTERY_POLL_MS;
    log_i("Scanner verbunden: %s (%d Reports)", _deviceName.c_str(), subscribed);
}

void BLEScanner::readBatteryNow() {
    if (!_connected || client == nullptr) return;
    NimBLERemoteService *batt = client->getService(NimBLEUUID(SVC_BATTERY));
    if (batt == nullptr) return;
    NimBLERemoteCharacteristic *chr = batt->getCharacteristic(NimBLEUUID(CHR_BATTERY));
    if (chr && chr->canRead()) _onBattery(chr->readValue<uint8_t>());
}

void BLEScanner::loop() {
    const uint32_t now = millis();

    // Sicherheitsnetz: ein Verbindungsversuch, der nicht zurueckkehrt, darf das
    // Geraet nicht dauerhaft blockieren.
    if (_connecting && now - _connectStartedMs > BLE_CONNECT_TIMEOUT_MS) {
        log_w("Verbindungsversuch abgebrochen (Zeitueberschreitung)");
        _connecting = false;
        if (client && client->isConnected()) client->disconnect();
        _nextTryMs = now + 3000;
    }

    if (!_connected && !_connecting && now >= _nextTryMs) {
        scanAndConnect();
        return;
    }

    if (_connected && now >= _nextBattMs) {
        readBatteryNow();
        _nextBattMs = now + BATTERY_POLL_MS;
    }
}

void BLEScanner::disconnect() {
    if (client && client->isConnected()) client->disconnect();
    _connected = false;
    _connecting = false;
}

void BLEScanner::forget() {
    disconnect();
    NimBLEDevice::deleteAllBonds();
    _nextTryMs = millis() + 1000;
}
