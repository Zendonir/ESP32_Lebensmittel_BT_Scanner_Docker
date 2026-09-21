#include "BLEScanner.h"

#include <NimBLEDevice.h>
#include <esp_task_wdt.h>

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
    void onConnect(NimBLEClient *) override { bleScanner._onConnect(); }

    void onConnectFail(NimBLEClient *, int reason) override {
        log_w("Verbindung zum Scanner fehlgeschlagen (Grund %d)", reason);
        bleScanner._onConnectFail();
    }

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
    // Beides kann fehlschlagen (Heap erschoepft, alle Client-Plaetze belegt).
    // Vorher wurde das nicht geprueft und _started trotzdem gesetzt - loop()
    // hat den Nullzeiger dann beim ersten Kopplungsversuch dereferenziert.
    // Lieber gar kein Scanner als ein Geraet, das beim Einschalten des
    // Scanners neu startet.
    _mutex = xSemaphoreCreateMutex();
    if (_mutex == nullptr) {
        log_e("Kein Speicher fuer den Scanner-Mutex - BLE bleibt aus");
        return;
    }

    NimBLEDevice::init("Lebensmittel-Terminal");
    NimBLEDevice::setPower(9);   // dBm - NimBLE 2.x nimmt keine esp_power_level_t mehr
    NimBLEDevice::setSecurityAuth(true, false, true);   // Bonding, kein MITM
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

    client = NimBLEDevice::createClient();
    if (client == nullptr) {
        log_e("NimBLE-Client liess sich nicht anlegen - BLE bleibt aus");
        vSemaphoreDelete(_mutex);
        _mutex = nullptr;
        return;
    }
    client->setClientCallbacks(&clientCallbacks, false);

    // Intervall 30-50 ms, Slave-Latenz 4, Aufsichtszeit 6 s.
    //
    // Die Latenz verzoegert keinen Barcode: sie erlaubt dem Scanner nur, ein
    // Verbindungsereignis auszulassen, wenn er *nichts* zu senden hat. Sobald
    // er etwas hat, sendet er sofort. Vorher stand hier 15 ms ohne Latenz -
    // das zwang den Handscanner, siebzigmal pro Sekunde aufzuwachen, obwohl
    // er stundenlang nichts zu melden hat. Das kostet dessen Akku und ist ein
    // Grund, warum solche Geraete die Verbindung von sich aus fallen lassen
    // (Trenngrund 0x08, Aufsichtszeit abgelaufen).
    //
    // Die 6 s Aufsichtszeit (vorher 2) geben Luft, wenn WLAN und BLE sich hier
    // eine Antenne im 2,4-GHz-Band teilen. Bedingung ist erfuellt:
    // (1+4) * 50 ms * 2 = 500 ms liegt weit unter 6 s.
    client->setConnectionParams(24, 40, 4, 600);
    client->setConnectTimeout(BLE_CONNECT_TIMEOUT_MS);

    _started = true;
    log_i("BLE bereit");
}

void BLEScanner::_onDisconnect() {
    _state = State::Idle;         // auch hier, nicht nur im Fehlerpfad
    _battery = -1;

    // Angefangenen Code wegwerfen. Bricht die Verbindung mitten in einem
    // Barcode ab (Leerlauf-Timeout, Akku leer, Funkloch), blieben die schon
    // empfangenen Ziffern sonst stehen und klebten beim naechsten Mal vorn an
    // den neuen Code. Heraus kaeme eine gueltig aussehende, aber falsche
    // Nummer - und damit ein falsches Produkt im Bestand. Der Rueckruf laeuft
    // auf dem NimBLE-Task, dem _buffer ohnehin allein gehoert.
    _buffer = "";
    // Kurz durchatmen, dann steht sofort wieder der gerichtete
    // Verbindungswunsch - der kostet nichts und faengt den Scanner in dem
    // Moment ein, in dem er sich wieder meldet.
    _nextTryMs = millis() + 800;
}

void BLEScanner::_onConnect() {
    // Nur umschalten - die Dienstsuche blockiert und gehoert deshalb in den
    // Hauptloop, nicht in diesen Rueckruf auf dem NimBLE-Task.
    _state = State::Discovering;
}

void BLEScanner::_onConnectFail() {
    _state = State::Idle;
    _failures++;
    _nextTryMs = millis() + 3000;
}

void BLEScanner::_onBattery(uint8_t level) {
    if (level <= 100) _battery = level;
}

void BLEScanner::_onReport(const uint8_t *data, size_t length) {
    // Jede Eingabe haelt die Verbindung am Leben - siehe BLE_IDLE_TIMEOUT_MS.
    // Bewusst hier und nicht erst beim fertigen Barcode: auch ein halb
    // getippter Code ist Benutzung.
    _lastActivityMs = millis();

    // Standard-Tastaturreport: [Modifier][reserviert][6 Tasten]
    if (length < 3) return;
    const bool shift = data[0] & 0x22;

    for (size_t i = 2; i < length && i < 8; i++) {
        const uint8_t key = data[i];
        if (key == 0) continue;

        if (key == 0x28 || key == 0x58) {        // Enter / Ziffernblock-Enter
            if (_buffer.length() > 0 && pushCode(_buffer)) _buffer = "";
            // Schlug die Uebergabe fehl, bleibt der Code im Puffer stehen und
            // der naechste Enter versucht es erneut. Vorher wurde _buffer in
            // jedem Fall geleert - ein Scan verschwand dann spurlos, und der
            // Benutzer stand vor einem Geraet, das auf seinen Piep hin nichts
            // tat.
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

// Fertigen Code in die Warteschlange legen. Laeuft auf dem NimBLE-Task.
// false = nicht uebernommen, der Aufrufer behaelt ihn.
bool BLEScanner::pushCode(const String &code) {
    if (_mutex == nullptr) return false;
    if (xSemaphoreTake(_mutex, pdMS_TO_TICKS(20)) != pdTRUE) return false;

    // Ueber eine gewoehnliche Ortsvariable rechnen: ++/-- direkt auf einem
    // volatile ist seit C++20 verworfen (-Wvolatile), und hier schuetzt
    // ohnehin der Mutex - volatile ist nur fuer den mutexlosen Vorabblick in
    // readCode() da.
    uint8_t count = _codeCount;

    if (count >= CODE_QUEUE) {
        // Kann nur passieren, wenn der Hauptloop minutenlang nicht abholt -
        // dann ist der aelteste Code ohnehin nichts mehr wert.
        log_w("Scan-Warteschlange voll - aeltester Code verworfen");
        _codeHead = (uint8_t)((_codeHead + 1) % CODE_QUEUE);
        count = (uint8_t)(count - 1);
    }
    _codes[(_codeHead + count) % CODE_QUEUE] = code;
    _codeCount = (uint8_t)(count + 1);

    xSemaphoreGive(_mutex);
    return true;
}

bool BLEScanner::readCode(String &code) {
    // Vorabblick ohne Mutex: _codeCount ist volatile, der Rest wird erst
    // unter dem Mutex angefasst. So kostet der Normalfall (nichts da) nichts.
    if (_codeCount == 0 || _mutex == nullptr) return false;
    if (xSemaphoreTake(_mutex, pdMS_TO_TICKS(20)) != pdTRUE) return false;

    bool got = false;
    const uint8_t count = _codeCount;
    if (count > 0) {
        code = _codes[_codeHead];
        _codes[_codeHead] = "";          // Speicher gleich zurueckgeben
        _codeHead = (uint8_t)((_codeHead + 1) % CODE_QUEUE);
        _codeCount = (uint8_t)(count - 1);
        got = code.length() > 0;
    }

    xSemaphoreGive(_mutex);
    return got;
}

// Gestaffelter Rueckzug in den Ruhezustand. Ohne Angabe waechst die Wartezeit
// mit der Zahl der Fehlversuche (2 s, 4 s, ... bis 30 s), damit ein
// abgeschalteter Scanner nicht dauernd Funkzeit und Strom kostet.
void BLEScanner::backoff(uint32_t delayMs) {
    _state = State::Idle;
    if (delayMs == 0) {
        _failures++;
        delayMs = min<uint32_t>(30000, 2000UL * _failures);
    }
    _nextTryMs = millis() + delayMs;
}

// Gerichteter Verbindungswunsch an den bereits gekoppelten Scanner, ohne
// Zeitgrenze. Das ist der schnellste Weg ueberhaupt: der Controller haelt den
// Wunsch offen und verbindet in dem Augenblick, in dem der Scanner zu werben
// beginnt - ohne Suchlauf, ohne Zutun der Firmware, ohne Wartezeit dazwischen.
void BLEScanner::startAutoConnect() {
    const NimBLEAddress peer = NimBLEDevice::getBondedAddress(0);

    _state = State::AutoConnect;
    _connectStartedMs = millis();
    client->setConnectTimeout(BLE_HS_FOREVER);

    // deleteAttributes bewusst true: mit zwischengespeicherten Handles aus
    // einer frueheren Verbindung schlaegt das Abonnieren fehl, wenn der
    // Scanner seine Handles neu vergibt - dann legen wir in finishConnect()
    // wieder auf (Trenngrund 0x16) und das Ganze beginnt von vorn. Die eine
    // Dienstsuche pro Verbindung ist dagegen billig.
    if (!client->connect(peer, true, true, true)) {
        log_w("Gerichtetes Warten auf %s nicht moeglich - es wird gesucht",
              peer.toString().c_str());
        _forceScan = true;
        backoff(1000);
        return;
    }
    log_i("Warte auf bekannten Scanner %s", peer.toString().c_str());
}

void BLEScanner::startScan() {
    haveTarget = false;
    _forceScan = false;

    NimBLEScan *scan = NimBLEDevice::getScan();
    scan->setScanCallbacks(&scanCallbacks, false);
    scan->setActiveScan(true);
    scan->setInterval(100);
    scan->setWindow(80);

    // start() statt getResults(): kehrt sofort zurueck, das Ergebnis holt
    // loop() ueber isScanning() ab. Genau hier stand vorher der 4-Sekunden-
    // Stillstand des ganzen Geraets.
    if (!scan->start(BLE_SCAN_DURATION_MS, false, true)) {
        log_w("BLE-Suche konnte nicht gestartet werden");
        backoff();
        return;
    }
    _state = State::Scanning;
    _connectStartedMs = millis();
}

void BLEScanner::beginConnect() {
    log_i("Verbinde mit %s", targetDevice.getName().c_str());
    _state = State::Connecting;
    _connectStartedMs = millis();
    client->setConnectTimeout(BLE_CONNECT_TIMEOUT_MS);

    // asyncConnect = true: kehrt sofort zurueck, das Ergebnis kommt ueber
    // onConnect bzw. onConnectFail.
    if (!client->connect(&targetDevice, true, true, true)) {
        backoff(3000);
    }
}

// Dienstsuche nach erfolgreicher Verbindung. Diese GATT-Abfragen blockieren
// (die Bibliothek bietet dafuer keine asynchrone Variante), aber sie laufen
// genau einmal pro Kopplung und nur, wenn wirklich ein Scanner geantwortet
// hat - nicht mehr bei jedem erfolglosen Suchlauf.
void BLEScanner::finishConnect() {
    // Die Dienstsuche ist der einzige Punkt in der Firmware, an dem ein
    // einzelner Loop-Durchlauf laenger als der Watchdog (WDT_TIMEOUT_S)
    // dauern kann: jede GATT-Abfrage wartet auf die Gegenstelle, und bei
    // einer angeschlagenen Funkstrecke laeuft sie in die NimBLE-eigene
    // Zeitgrenze von 30 s. Der Reset am Loop-Anfang liegt dann schon eine
    // Weile zurueck - ohne das Fuettern hier starten wir das Geraet neu,
    // obwohl nur der Scanner klemmt.
    esp_task_wdt_reset();

    NimBLERemoteService *hid = client->getService(NimBLEUUID(SVC_HID));
    esp_task_wdt_reset();
    if (hid == nullptr) {
        // Wichtig zu protokollieren: von aussen sieht das wie ein spontaner
        // Verbindungsabbruch aus (Trenngrund 0x16 - "vom Geraet selbst
        // getrennt"), obwohl wir es sind, die auflegen.
        log_w("Kein HID-Dienst gefunden - Verbindung wird beendet");
        client->disconnect();
        backoff(5000);
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
        esp_task_wdt_reset();
    }

    if (subscribed == 0) {
        log_w("Keine Report-Charakteristik abonnierbar - Verbindung wird beendet");
        client->disconnect();
        backoff(5000);
        return;
    }

    if (NimBLERemoteService *batt = client->getService(NimBLEUUID(SVC_BATTERY))) {
        if (NimBLERemoteCharacteristic *chr = batt->getCharacteristic(NimBLEUUID(CHR_BATTERY))) {
            if (chr->canNotify()) chr->subscribe(true, notifyCallback);
            if (chr->canRead()) _onBattery(chr->readValue<uint8_t>());
        }
    }

    esp_task_wdt_reset();
    const NimBLEAddress peer = client->getPeerAddress();

    // Genau eine Kopplung behalten. Sonst zeigt getBondedAddress(0) nach einem
    // Scannerwechsel womoeglich auf den alten - und das gerichtete Warten
    // liefe dauerhaft ins Leere, waehrend der neue Scanner danebensteht.
    for (int i = NimBLEDevice::getNumBonds() - 1; i >= 0; i--) {
        const NimBLEAddress bonded = NimBLEDevice::getBondedAddress(i);
        if (bonded != peer) NimBLEDevice::deleteBond(bonded);
    }

    // Der Name stammt normalerweise aus dem Suchtreffer. Nach dem gerichteten
    // Warten gab es keinen - dann direkt beim Geraet nachfragen (GAP-Dienst
    // 0x1800, Merkmal "Device Name" 0x2A00), sonst stuende im System-Panel
    // nach jedem Neustart nur eine leere Zeile.
    String name = String(targetDevice.getName().c_str());
    if (name.isEmpty() || targetDevice.getAddress() != peer) {
        const NimBLEAttValue gapName =
            client->getValue(NimBLEUUID((uint16_t)0x1800), NimBLEUUID((uint16_t)0x2A00));
        if (gapName.length()) name = String(gapName.c_str());
    }
    esp_task_wdt_reset();
    _deviceName = name.isEmpty() ? String(peer.toString().c_str()) : name;
    _address = String(peer.toString().c_str());
    _state = State::Connected;           // Erfolgspfad
    _failures = 0;
    _lastActivityMs = millis();          // die vollen 10 Minuten ab jetzt
    _nextBattMs = millis() + BATTERY_POLL_MS;
    log_i("Scanner verbunden: %s (%d Reports)", _deviceName.c_str(), subscribed);
}

void BLEScanner::readBatteryNow() {
    if (_state != State::Connected || client == nullptr) return;
    NimBLERemoteService *batt = client->getService(NimBLEUUID(SVC_BATTERY));
    if (batt == nullptr) return;
    NimBLERemoteCharacteristic *chr = batt->getCharacteristic(NimBLEUUID(CHR_BATTERY));
    if (chr && chr->canRead()) _onBattery(chr->readValue<uint8_t>());
}

void BLEScanner::loop() {
    if (!_started) return;
    const uint32_t now = millis();

    // Sicherheitsnetz: haengt ein Suchlauf oder Verbindungsversuch, darf das
    // die Kopplung nicht dauerhaft lahmlegen. Anders als zuvor wird das hier
    // auch wirklich erreicht - loop() kehrt in jedem Zustand sofort zurueck.
    // AutoConnect ist hier bewusst ausgenommen: das Warten auf den bekannten
    // Scanner soll gerade unbegrenzt laufen. Dafuer gibt es weiter unten den
    // eigenen, viel laengeren Ausweg.
    if ((_state == State::Scanning || _state == State::Connecting) &&
        now - _connectStartedMs > BLE_CONNECT_TIMEOUT_MS) {
        log_w("Kopplungsversuch abgebrochen (Zeitueberschreitung)");
        NimBLEDevice::getScan()->stop();
        client->cancelConnect();
        backoff(3000);
        return;
    }

    switch (_state) {
        case State::Idle:
            if (now < _nextTryMs || now < _pauseUntilMs) break;
            // Bekannten Scanner nicht suchen, sondern auf ihn warten - das ist
            // sofort da, wenn er eingeschaltet wird.
            if (!_forceScan && NimBLEDevice::getNumBonds() > 0) startAutoConnect();
            else                                                startScan();
            break;

        case State::AutoConnect:
            // Nichts zu tun - das laeuft im Controller. Nur der Ausweg, falls
            // die gespeicherte Kopplung nicht mehr stimmt.
            if (now - _connectStartedMs > BLE_AUTOCONNECT_RETRY_MS) {
                log_i("Bekannter Scanner meldet sich nicht - einmal suchen");
                client->cancelConnect();
                _forceScan = true;
                _state = State::Idle;
            }
            break;

        case State::Scanning: {
            NimBLEScan *scan = NimBLEDevice::getScan();
            if (scan->isScanning()) break;      // laeuft noch, naechster Durchlauf
            scan->clearResults();
            if (haveTarget) {
                beginConnect();
            } else if (NimBLEDevice::getNumBonds() > 0) {
                // Kopplung vorhanden: gleich zurueck ins gerichtete Warten.
                // Die wachsende Wartezeit waere hier schaedlich - sie wuerde
                // genau das Fenster aufreissen, in dem der Scanner
                // eingeschaltet wird und niemand zuhoert.
                backoff(500);
            } else {
                // Noch nichts gekoppelt und nichts gefunden - dann ist auch
                // nichts da. Hier darf die Wartezeit wachsen.
                backoff();
            }
            break;
        }

        case State::Connecting:
            break;                              // wartet auf onConnect/onConnectFail

        case State::Discovering:
            finishConnect();
            break;

        case State::Connected:
            // Nach dem letzten Barcode noch eine Weile verbunden bleiben,
            // dann auflegen, damit der Handscanner schlafen kann.
            if (now - _lastActivityMs > BLE_IDLE_TIMEOUT_MS) {
                log_i("Seit %lu Minuten kein Scan - Verbindung wird getrennt",
                      (unsigned long)(BLE_IDLE_TIMEOUT_MS / 60000));
                if (client->isConnected()) client->disconnect();
                _state = State::Idle;
                _pauseUntilMs = now + BLE_IDLE_COOLDOWN_MS;
                break;
            }
            if (now >= _nextBattMs) {
                readBatteryNow();
                _nextBattMs = now + BATTERY_POLL_MS;
            }
            break;
    }
}

void BLEScanner::disconnect() {
    if (!_started) return;
    // Von Hand getrennt - eine Weile nicht selbsttaetig neu verbinden, sonst
    // haengt der Scanner nach zwei Sekunden wieder dran (siehe _pauseUntilMs).
    _pauseUntilMs = millis() + 60000;
    NimBLEDevice::getScan()->stop();
    if (client) {
        client->cancelConnect();
        if (client->isConnected()) client->disconnect();
    }
    _state = State::Idle;
}

void BLEScanner::forget() {
    if (!_started) return;
    disconnect();
    NimBLEDevice::deleteAllBonds();
    _nextTryMs = millis() + 1000;
}
