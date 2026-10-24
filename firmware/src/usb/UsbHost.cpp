#include "UsbHost.h"

#include <algorithm>

#include <esp_heap_caps.h>
#include <esp_intr_alloc.h>
#include <mbedtls/base64.h>
#include <usb/usb_host.h>

UsbHost usbHost;

// Zustand des USB-Stacks. Die Rueckrufe sind C-Funktionen und laufen in
// usb_host_client_handle_events() - also im Hauptloop, aus UsbHost::loop().
namespace {

enum class Kind : uint8_t { None, Printer, Keyboard };

usb_host_client_handle_t client = nullptr;
usb_device_handle_t      dev    = nullptr;
Kind     kind      = Kind::None;
uint8_t  ifNum     = 0;
uint8_t  epAddr    = 0;
uint16_t epMps     = 0;
uint8_t  hidSub    = 0;
bool     claimed   = false;
uint8_t  newAddr   = 0;      // gemeldet, noch nicht geoeffnet
bool     gone      = false;  // Geraet abgezogen, Aufraeumen steht aus

usb_transfer_t *outXfer  = nullptr;
usb_transfer_t *inXfer   = nullptr;
usb_transfer_t *ctrlXfer = nullptr;
volatile bool outBusy  = false;
volatile bool outDone  = false;
volatile bool inBusy   = false;
volatile bool ctrlBusy = false;
uint32_t outSince = 0;

// Berichte der Tastatur, bis loop() sie abholt.
uint8_t  report[64];
int      reportLen = 0;
bool     reportNew = false;

void clientEvent(const usb_host_client_event_msg_t *msg, void *) {
    if (msg->event == USB_HOST_CLIENT_EVENT_NEW_DEV) {
        newAddr = msg->new_dev.address;
    } else if (msg->event == USB_HOST_CLIENT_EVENT_DEV_GONE) {
        if (msg->dev_gone.dev_hdl == dev) gone = true;
    }
}

void outDoneCb(usb_transfer_t *) {
    outBusy = false;
    outDone = true;
}

void ctrlDoneCb(usb_transfer_t *) { ctrlBusy = false; }

void inDoneCb(usb_transfer_t *t) {
    inBusy = false;
    if (t->status == USB_TRANSFER_STATUS_COMPLETED && t->actual_num_bytes > 0) {
        reportLen = std::min(t->actual_num_bytes, (int)sizeof(report));
        memcpy(report, t->data_buffer, reportLen);
        reportNew = true;
    }
    // Gleich wieder lauschen - ausser das Geraet ist weg.
    if (!gone && dev && (t->status == USB_TRANSFER_STATUS_COMPLETED ||
                         t->status == USB_TRANSFER_STATUS_TIMED_OUT)) {
        if (usb_host_transfer_submit(t) == ESP_OK) inBusy = true;
    }
}

String asciiFrom(const usb_str_desc_t *d) {
    String s;
    if (!d) return s;
    for (int i = 0; i < (d->bLength - 2) / 2; i++) {
        const uint16_t c = d->wData[i];
        s += (c >= 0x20 && c < 0x7f) ? (char)c : '?';
    }
    s.trim();
    return s;
}

// HID-Tastaturcodes (Usage Page 7) in US-Belegung - Barcodescanner senden in
// der Werkseinstellung so. Index = Code, [0] ohne, [1] mit Umschalt.
char keyChar(uint8_t code, bool shift) {
    if (code >= 0x04 && code <= 0x1d) return (shift ? 'A' : 'a') + (code - 0x04);
    if (code >= 0x1e && code <= 0x26) return shift ? "!@#$%^&*("[code - 0x1e] : '1' + (code - 0x1e);
    if (code == 0x27) return shift ? ')' : '0';
    if (code >= 0x59 && code <= 0x61) return '1' + (code - 0x59);   // Ziffernblock
    switch (code) {
        case 0x2c: return ' ';
        case 0x2d: return shift ? '_' : '-';
        case 0x2e: return shift ? '+' : '=';
        case 0x2f: return shift ? '{' : '[';
        case 0x30: return shift ? '}' : ']';
        case 0x31: return shift ? '|' : '\\';
        case 0x33: return shift ? ':' : ';';
        case 0x34: return shift ? '"' : '\'';
        case 0x35: return shift ? '~' : '`';
        case 0x36: return shift ? '<' : ',';
        case 0x37: return shift ? '>' : '.';
        case 0x38: return shift ? '?' : '/';
        case 0x54: return '/';
        case 0x55: return '*';
        case 0x56: return '-';
        case 0x57: return '+';
        case 0x62: return '0';
        case 0x63: return '.';
        default:   return 0;
    }
}

bool isEnter(uint8_t code) { return code == 0x28 || code == 0x58 || code == 0x2b; }

// Schnittstelle suchen: Druckerklasse mit Bulk-OUT, sonst eine Tastatur
// (HID) mit Interrupt-IN. Die Deskriptorkette wird so vorsichtig gelesen
// wie im Printserver (usb_desc_report.c) - ein kaputtes Geraet darf das
// Terminal nicht umwerfen.
bool findInterface(const uint8_t *cfg, size_t len) {
    Kind best = Kind::None;
    uint8_t curIf = 0, curClass = 0, curSub = 0, curProto = 0;
    size_t off = 0;
    while (off + 2 <= len) {
        const uint8_t blen = cfg[off];
        if (blen < 2 || blen > len - off) break;
        const uint8_t *d = cfg + off;
        off += blen;
        if (d[1] == USB_B_DESCRIPTOR_TYPE_INTERFACE && blen >= 9) {
            curIf = d[2];
            curClass = d[5];
            curSub = d[6];
            curProto = d[7];
            if (d[3] != 0) curClass = 0;   // nur Grundeinstellung
        } else if (d[1] == USB_B_DESCRIPTOR_TYPE_ENDPOINT && blen >= 7) {
            const uint8_t addr = d[2], type = d[3] & 0x03;
            const uint16_t mps = (d[4] | (d[5] << 8)) & 0x07ff;
            if (curClass == 0x07 && type == 0x02 && !(addr & 0x80)) {
                ifNum = curIf; epAddr = addr; epMps = mps;
                kind = Kind::Printer;                     // Drucker geht vor
                return true;
            }
            if (curClass == 0x03 && type == 0x03 && (addr & 0x80) && best == Kind::None &&
                (curProto == 1 || curProto == 0)) {
                best = Kind::Keyboard;
                ifNum = curIf; epAddr = addr; epMps = mps; hidSub = curSub;
            }
        }
    }
    kind = best;
    return best != Kind::None;
}

}  // namespace

// ---------------------------------------------------------------------------
bool UsbHost::begin() {
    if (_installed) return true;
    usb_host_config_t host = {};
    host.skip_phy_setup = false;
    host.intr_flags = ESP_INTR_FLAG_LEVEL1;
    esp_err_t err = usb_host_install(&host);
    if (err != ESP_OK) {
        log_e("USB-Host: Installation fehlgeschlagen (%s)", esp_err_to_name(err));
        return false;
    }
    usb_host_client_config_t cfg = {};
    cfg.is_synchronous = false;
    cfg.max_num_event_msg = 5;
    cfg.async.client_event_callback = clientEvent;
    cfg.async.callback_arg = nullptr;
    if (usb_host_client_register(&cfg, &client) != ESP_OK ||
        usb_host_transfer_alloc(OUT_CHUNK, 0, &outXfer) != ESP_OK ||
        usb_host_transfer_alloc(64, 0, &inXfer) != ESP_OK ||
        usb_host_transfer_alloc(sizeof(usb_setup_packet_t) + 8, 0, &ctrlXfer) != ESP_OK) {
        log_e("USB-Host: Client/Puffer fehlgeschlagen");
        return false;
    }
    _installed = true;
    log_i("USB-Host an - die serielle Konsole ueber USB ist ab jetzt stumm");
    return true;
}

const char *UsbHost::deviceKind() const {
    if (!dev || gone) return "";
    return kind == Kind::Printer ? "drucker" : kind == Kind::Keyboard ? "scanner" : "";
}

void UsbHost::openDevice(uint8_t addr) {
    if (dev) return;   // ein Geraet zur Zeit
    if (usb_host_device_open(client, addr, &dev) != ESP_OK) {
        dev = nullptr;
        return;
    }
    gone = false;
    usb_device_info_t info;
    const usb_config_desc_t *cd = nullptr;
    if (usb_host_device_info(dev, &info) != ESP_OK ||
        usb_host_get_active_config_descriptor(dev, &cd) != ESP_OK || !cd) {
        closeDevice();
        return;
    }
    _name = asciiFrom(info.str_desc_product);
    if (!findInterface(reinterpret_cast<const uint8_t *>(cd), cd->wTotalLength)) {
        log_w("USB: %s ist weder Drucker noch Tastatur", _name.c_str());
        _name = "";
        closeDevice();
        return;
    }
    if (usb_host_interface_claim(client, dev, ifNum, 0) != ESP_OK) {
        log_e("USB: Schnittstelle %u nicht belegbar", ifNum);
        closeDevice();
        return;
    }
    claimed = true;
    log_i("USB: %s als %s", _name.c_str(), deviceKind());
    if (kind == Kind::Keyboard) startKeyboard();
}

void UsbHost::startKeyboard() {
    memset(_lastKeys, 0, sizeof(_lastKeys));
    // Bootprotokoll: 8 Bytes je Bericht, ohne Report-ID. Ohne Antwort ist es
    // auch gut - die meisten Scanner senden ohnehin so.
    if (hidSub == 1) {
        auto *s = reinterpret_cast<usb_setup_packet_t *>(ctrlXfer->data_buffer);
        s->bmRequestType = 0x21;   // Klasse, an die Schnittstelle
        s->bRequest = 0x0b;        // SET_PROTOCOL
        s->wValue = 0;             // Boot
        s->wIndex = ifNum;
        s->wLength = 0;
        ctrlXfer->num_bytes = sizeof(usb_setup_packet_t);
        ctrlXfer->device_handle = dev;
        ctrlXfer->bEndpointAddress = 0;
        ctrlXfer->callback = ctrlDoneCb;
        ctrlXfer->timeout_ms = 1000;
        if (usb_host_transfer_submit_control(client, ctrlXfer) == ESP_OK) ctrlBusy = true;
    }
    const int mps = epMps ? std::min<int>(epMps, 64) : 8;
    inXfer->num_bytes = mps;
    inXfer->device_handle = dev;
    inXfer->bEndpointAddress = epAddr;
    inXfer->callback = inDoneCb;
    inXfer->timeout_ms = 0;
    if (usb_host_transfer_submit(inXfer) == ESP_OK) inBusy = true;
}

void UsbHost::closeDevice() {
    if (!dev) return;
    if (claimed) usb_host_interface_release(client, dev, ifNum);
    claimed = false;
    usb_host_device_close(client, dev);
    dev = nullptr;
    kind = Kind::None;
    gone = false;
    reportNew = false;
    _name = "";
}

// ---------------------------------------------------------------------------
void UsbHost::loop() {
    if (!_installed) return;
    uint32_t flags = 0;
    usb_host_lib_handle_events(0, &flags);
    usb_host_client_handle_events(client, 0);

    if (newAddr) {
        const uint8_t a = newAddr;
        newAddr = 0;
        openDevice(a);
    }
    // Abgezogen: erst schliessen, wenn keine Uebertragung mehr unterwegs ist
    // (die melden sich nach dem Abziehen mit NO_DEVICE zurueck).
    if (gone && !outBusy && !inBusy && !ctrlBusy) {
        log_i("USB: %s abgezogen", _name.c_str());
        closeDevice();
    }

    if (reportNew) {
        reportNew = false;
        onKeyboardReport(report, reportLen);
    }
    if (_typed.length() && millis() - _lastKeyAt > CODE_GAP_MS) finishCode();
}

void UsbHost::onKeyboardReport(const uint8_t *r, int len) {
    // Bootbericht: [Umschalttasten, 0, Taste1..Taste6]. Mit Report-ID davor
    // ist er ein Byte laenger.
    if (len == 9) { r++; len--; }
    if (len < 8) return;
    const bool shift = r[0] & 0x22;
    for (int i = 2; i < 8; i++) {
        const uint8_t code = r[i];
        if (code < 0x04) continue;                     // leer oder Rollover-Fehler
        bool held = false;                             // schon im letzten Bericht
        for (uint8_t k : _lastKeys) held |= (k == code);
        if (held) continue;
        _lastKeyAt = millis();
        if (isEnter(code)) {
            finishCode();
        } else if (const char c = keyChar(code, shift)) {
            if (_typed.length() < 128) _typed += c;
        }
    }
    memcpy(_lastKeys, r + 2, 6);
}

void UsbHost::finishCode() {
    _typed.trim();
    if (_typed.length() && _codes.size() < 8) _codes.push_back(_typed);
    _typed = "";
}

bool UsbHost::readCode(String &code) {
    if (_codes.empty()) return false;
    code = _codes.front();
    _codes.pop_front();
    return true;
}

// ---------------------------------------------------------------------------
// Drucken
// ---------------------------------------------------------------------------
void UsbHost::dropJob(size_t index) {
    // Laeuft der Auftrag schon zum Drucker, gehoert ihm die Uebertragung
    // unterwegs - dann nur als gescheitert markieren, process() raeumt ab.
    if (_jobs[index].started) {
        _jobs[index].broken = true;
        _jobs[index].complete = true;
        return;
    }
    heap_caps_free(_jobs[index].data);
    _jobs.erase(_jobs.begin() + index);
}

bool UsbHost::addChunk(int id, int part, bool last, const char *b64, String &error) {
    if (!_installed) {
        error = "USB-Host aus";
        return false;
    }
    Job *job = nullptr;
    size_t index = 0;
    for (; index < _jobs.size(); index++) {
        if (_jobs[index].id == id) { job = &_jobs[index]; break; }
    }
    if (part == 0 && job) {
        // Nach einem Reconnect schickt der Server den Auftrag neu. Laeuft er
        // schon zum Drucker, wird der zweite ignoriert - sonst kaeme das
        // Etikett doppelt.
        if (job->started) return true;
        dropJob(index);
        job = nullptr;
    }
    if (part == 0) {
        if (_jobs.size() >= MAX_JOBS) {
            error = "Warteschlange voll";
            return false;
        }
        Job neu;
        neu.id = id;
        neu.since = neu.lastData = millis();
        _jobs.push_back(neu);
        job = &_jobs.back();
        index = _jobs.size() - 1;
    }
    // Laeuft der Auftrag schon zum Drucker, meldet process() das Scheitern,
    // wenn die Uebertragung unterwegs zurueck ist - sonst kaeme es doppelt.
    auto lose = [&](const char *why) {
        const bool later = job->started;
        dropJob(index);
        error = why;
        return later;
    };
    if (!job) {
        error = "Druckdaten unvollstaendig";
        return false;
    }
    if (job->complete || part != job->nextPart) {
        if (job->started) return true;   // Rest eines ignorierten Neuversands
        return lose("Druckdaten unvollstaendig");
    }

    const size_t b64len = strlen(b64);
    size_t need = 0;
    mbedtls_base64_decode(nullptr, 0, &need, (const unsigned char *)b64, b64len);
    if (job->len + need > MAX_JOB_BYTES) return lose("Auftrag zu gross");
    auto *grown = (uint8_t *)heap_caps_realloc(job->data, job->len + need + 1,
                                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!grown) return lose("Kein Speicher");
    job->data = grown;
    size_t got = 0;
    if (mbedtls_base64_decode(job->data + job->len, need + 1, &got,
                              (const unsigned char *)b64, b64len) != 0) {
        return lose("Druckdaten beschaedigt");
    }
    job->len += got;
    job->nextPart++;
    job->complete = last;
    job->lastData = millis();
    return true;
}

int UsbHost::process(bool &ok, String &error) {
    if (_jobs.empty()) return 0;
    Job &job = _jobs.front();
    const int id = job.id;
    auto fail = [&](const char *why) {
        ok = false;
        error = why;
        heap_caps_free(job.data);
        _jobs.pop_front();
        return id;
    };

    if (outDone) {
        outDone = false;
        if (outXfer->status != USB_TRANSFER_STATUS_COMPLETED) {
            return fail(gone ? "Drucker abgezogen" : "USB-Fehler beim Senden");
        }
        job.sent += outXfer->actual_num_bytes;
    }
    if (outBusy) {
        // Nimmt der Drucker nichts mehr an (Deckel offen, Papier zu Ende),
        // haengt die Uebertragung - abbrechen, der Rueckruf meldet CANCELED.
        if (millis() - outSince > OUT_TIMEOUT_MS && dev) {
            usb_host_endpoint_halt(dev, epAddr);
            usb_host_endpoint_flush(dev, epAddr);
            usb_host_endpoint_clear(dev, epAddr);
            outSince = millis();
        }
        return 0;
    }

    if (job.broken) return fail("Druckdaten unvollstaendig");
    // Fehlt ein Stueck dauerhaft (Verbindung weg), nicht ewig warten.
    if (!job.complete && millis() - job.lastData > PART_TIMEOUT_MS) {
        return fail("Druckdaten unvollstaendig");
    }
    if (job.complete && job.sent >= job.len) {
        ok = true;
        error = "";
        heap_caps_free(job.data);
        _jobs.pop_front();
        return id;
    }
    if (!dev || gone || kind != Kind::Printer) {
        if (job.started) return fail("Drucker abgezogen");
        if (millis() - job.since > WAIT_PRINTER_MS) return fail("Kein Drucker am USB");
        return 0;
    }
    if (job.sent < job.len) {
        const size_t n = std::min(job.len - job.sent, OUT_CHUNK);
        memcpy(outXfer->data_buffer, job.data + job.sent, n);
        outXfer->num_bytes = n;
        outXfer->device_handle = dev;
        outXfer->bEndpointAddress = epAddr;
        outXfer->callback = outDoneCb;
        outXfer->timeout_ms = 0;
        if (usb_host_transfer_submit(outXfer) != ESP_OK) return fail("USB-Fehler beim Senden");
        outBusy = true;
        job.started = true;
        outSince = millis();
    }
    return 0;
}
