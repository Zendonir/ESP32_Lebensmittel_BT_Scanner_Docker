#include "Printer.h"

#include <qrcode.h>

Printer printer;

static HardwareSerial uart(1);

void Printer::begin(uint32_t baud) {
    // Grosser Sendepuffer: bei 9600 Baud braucht ein Etikett rund eine halbe
    // Sekunde Leitungszeit. Passt es komplett in den Puffer, kehrt write()
    // sofort zurueck und der UART-Treiber sendet im Hintergrund weiter - sonst
    // steht der ganze Loop (und damit die Bedienung) waehrend des Druckens.
    uart.setTxBufferSize(2048);
    uart.begin(baud, SERIAL_8N1, PRINTER_RX, PRINTER_TX);
    _ready = true;
    reset();
}

void Printer::reset() {
    uart.write((const uint8_t *)"\x1B\x40", 2);   // ESC @ - Initialisieren
    uart.write((const uint8_t *)"\x1B\x74\x10", 3);  // ESC t 16 - Codepage WPC1252
}

// UTF-8 -> CP1252. Der Drucker kennt kein UTF-8; ohne diese Umsetzung werden
// aus Umlauten zwei Ersatzzeichen.
String Printer::toCp1252(const String &utf8) const {
    String out;
    out.reserve(utf8.length());
    for (size_t i = 0; i < utf8.length(); i++) {
        const uint8_t c = utf8[i];
        if (c < 0x80) {
            out += (char)c;
        } else if ((c & 0xE0) == 0xC0 && i + 1 < utf8.length()) {
            const uint16_t cp = ((c & 0x1F) << 6) | (utf8[++i] & 0x3F);
            out += (cp <= 0xFF) ? (char)cp : '?';
        } else if ((c & 0xF0) == 0xE0) {
            i += 2;          // 3-Byte-Zeichen (z.B. Emoji) hat CP1252 nicht
            out += '?';
        } else {
            out += '?';
        }
    }
    return out;
}

bool Printer::enqueue(int jobId, JsonDocument &job) {
    if (_count >= MAX_QUEUE) return false;
    const size_t slot = (_head + _count) % MAX_QUEUE;
    _queue[slot].jobId = jobId;
    _queue[slot].doc.clear();
    _queue[slot].doc.set(job);
    _count++;
    return true;
}

int Printer::process(bool &ok, String &error) {
    if (_count == 0) return 0;
    if (!_ready) {
        ok = false;
        error = "Drucker nicht bereit";
        const int id = _queue[_head].jobId;
        _head = (_head + 1) % MAX_QUEUE;
        _count--;
        return id;
    }

    // Erst anfangen, wenn das komplette Etikett in den Sendepuffer passt.
    // Sonst laeuft der Puffer bei mehreren Auftraegen hintereinander voll und
    // write() wartet doch wieder auf die 9600-Baud-Leitung. Der Auftrag bleibt
    // solange in der Warteschlange und kommt im naechsten Durchlauf dran.
    if (uart.availableForWrite() < 1024) return 0;

    Job &job = _queue[_head];
    const int id = job.jobId;

    _chars = job.doc["chars"] | 32;
    _rotate = job.doc["rotate"] | false;
    reset();
    writeBlocks(job.doc["blocks"].as<JsonArray>());
    // Bewusst kein uart.flush(): das wartet, bis das letzte Bit auf der
    // Leitung ist, und blockiert damit genau die halbe Sekunde, die der
    // Sendepuffer gerade vermeiden soll. Der Treiber sendet zuverlaessig zu
    // Ende; nur ein Neustart mitten im Druck koennte etwas abschneiden.

    job.doc.clear();
    _head = (_head + 1) % MAX_QUEUE;
    _count--;

    ok = true;
    error = "";
    return id;
}

void Printer::writeBlocks(JsonArray blocks) {
    if (blocks.isNull()) return;
    for (JsonObject block : blocks) {
        const String type = String(block["t"] | "");
        if (type == "text") {
            textLine(String(block["v"] | ""), block["align"] | 0,
                     block["bold"] | false, block["large"] | false);
        } else if (type == "row") {
            row(String(block["k"] | ""), String(block["v"] | ""),
                block["underline"] | false);
        } else if (type == "sep") {
            separator();
        } else if (type == "qr") {
            qr(String(block["v"] | ""), block["scale"] | 3);
        } else if (type == "code128") {
            code128(String(block["v"] | ""), block["height"] | 40);
        } else if (type == "feed") {
            feedDots(block["dots"] | 0);
        } else if (type == "back") {
            backfeedDots(block["dots"] | 0);
        }
    }
}

void Printer::textLine(const String &text, uint8_t align, bool bold, bool large) {
    // ESC V - 90 Grad gedreht. Hochkant laeuft die Schrift ueber die lange
    // Kante des Etiketts; ohne diesen Befehl muesste die Firmware die Zeilen
    // selbst als Bitmap rechnen.
    uart.write(0x1B); uart.write('V'); uart.write(_rotate ? 1 : 0);
    uart.write(0x1B); uart.write('a'); uart.write(align);           // ESC a - Ausrichtung
    uart.write(0x1B); uart.write('E'); uart.write(bold ? 1 : 0);    // ESC E - fett
    uart.write(0x1D); uart.write('!'); uart.write(large ? 0x11 : 0x00);  // GS ! - Groesse

    const String encoded = toCp1252(text);
    uart.write((const uint8_t *)encoded.c_str(), encoded.length());
    uart.write((const uint8_t *)"\r\n", 2);

    uart.write(0x1D); uart.write('!'); uart.write((uint8_t)0);
    uart.write(0x1B); uart.write('E'); uart.write((uint8_t)0);
    uart.write(0x1B); uart.write('V'); uart.write((uint8_t)0);
}

void Printer::row(const String &key, const String &value, bool underline) {
    uart.write(0x1B); uart.write('a'); uart.write((uint8_t)0);

    const String k = toCp1252(key);
    const String v = toCp1252(value);
    int pad = _chars - (int)k.length() - (int)v.length() - 1;
    if (pad < 1) pad = 1;

    uart.write((const uint8_t *)k.c_str(), k.length());
    for (int i = 0; i < pad; i++) uart.write(' ');

    if (underline) { uart.write(0x1B); uart.write('-'); uart.write(1); }
    uart.write((const uint8_t *)v.c_str(), v.length());
    if (underline) { uart.write(0x1B); uart.write('-'); uart.write((uint8_t)0); }
    uart.write((const uint8_t *)"\r\n", 2);
}

void Printer::separator() {
    uart.write(0x1B); uart.write('a'); uart.write((uint8_t)0);
    for (uint8_t i = 0; i < _chars; i++) uart.write('-');
    uart.write((const uint8_t *)"\r\n", 2);
}

void Printer::code128(const String &data, uint8_t height) {
    if (data.isEmpty()) return;
    uart.write(0x1B); uart.write('a'); uart.write(1);      // zentriert
    uart.write(0x1D); uart.write('h'); uart.write(height); // Hoehe in Dots
    uart.write(0x1D); uart.write('w'); uart.write(2);      // Modulbreite
    uart.write(0x1D); uart.write('H'); uart.write((uint8_t)0);  // keine Klartextzeile

    // GS k 73 n {B <Daten> - Codeset B deckt Ziffern und Grossbuchstaben ab.
    const size_t length = data.length() + 2;
    if (length > 255) return;
    uart.write(0x1D); uart.write('k'); uart.write(73); uart.write((uint8_t)length);
    uart.write('{'); uart.write('B');
    uart.write((const uint8_t *)data.c_str(), data.length());
    uart.write((const uint8_t *)"\r\n", 2);
}

void Printer::qr(const String &data, uint8_t scale) {
    if (data.isEmpty()) return;
    if (scale < 2) scale = 2;

    // Der eingebaute QR-Befehl (GS ( k) fehlt vielen guenstigen Druckern.
    // Deshalb wird der Code selbst gerechnet und als Bitmap gedruckt - das
    // funktioniert auf jedem ESC/POS-Geraet.
    // Kleinste Version nehmen, die die Daten fasst. Version 3 war fest
    // verdrahtet und damit immer 29 Module breit - eine Etikettennummer wie
    // "LEB000123" passt in Version 1 mit 21 Modulen. Auf 30 mm Etikettenhoehe
    // sind das gesparte 8 Module mal Skalierung, also gut ein Viertel.
    QRCode qrcode;
    uint8_t buffer[qrcode_getBufferSize(3)];
    uint8_t version = 0;
    for (uint8_t v = 1; v <= 3; v++) {
        if (qrcode_initText(&qrcode, buffer, v, ECC_MEDIUM, data.c_str()) == 0) { version = v; break; }
    }
    if (version == 0) return;

    // Ein ESC-*-Durchgang druckt 8 Punktzeilen. Damit der Code nicht in die
    // Laenge gezogen wird, stecken zwei Modulzeilen in einem Durchgang -
    // senkrecht und waagerecht also derselbe Faktor.
    const int size = qrcode.size;
    const int widthDots = size * scale;

    uart.write(0x1B); uart.write('a'); uart.write(1);      // zentriert
    uart.write(0x1B); uart.write('3'); uart.write(8);      // Zeilenabstand = 8 Punkte

    for (int y = 0; y < size; y += 2) {
        uart.write(0x1B); uart.write('*'); uart.write((uint8_t)0);   // 8-Punkt-Einfachdichte
        uart.write((uint8_t)(widthDots & 0xFF));
        uart.write((uint8_t)(widthDots >> 8));

        for (int x = 0; x < widthDots; x++) {
            const int mx = x / scale;
            const bool top = qrcode_getModule(&qrcode, mx, y);
            const bool bottom = (y + 1 < size) && qrcode_getModule(&qrcode, mx, y + 1);
            uart.write((uint8_t)((top ? 0xF0 : 0x00) | (bottom ? 0x0F : 0x00)));
        }
        uart.write((const uint8_t *)"\r\n", 2);
    }
    uart.write(0x1B); uart.write('2');                     // Zeilenabstand zurueck
}

void Printer::backfeedDots(uint8_t dots) {
    // ESC j n - drucken und n Punkte zurueckfahren. Holt den Totbereich
    // zwischen Druckkopf und Abrisskante zurueck, der sonst ungenutzt am
    // Etikettenanfang stehen bleibt.
    //
    // Nicht jeder ESC/POS-Drucker kann rueckwaerts; wer es nicht kann,
    // ignoriert den Befehl in aller Regel stillschweigend und druckt wie
    // bisher. Deshalb ist er in den Einstellungen aus, bis jemand ihn
    // einschaltet - und deshalb nur ein Befehl, kein Stueckeln ueber 255:
    // weiter zurueck als 255 Punkte gehoert das Etikett nicht gezogen.
    if (dots == 0) return;
    uart.write(0x1B); uart.write('j'); uart.write(dots);
}

void Printer::feedDots(uint16_t dots) {
    // ESC J n - maximal 255 Dots pro Befehl, deshalb in Stuecken.
    while (dots > 0) {
        const uint8_t chunk = dots > 255 ? 255 : (uint8_t)dots;
        uart.write(0x1B); uart.write('J'); uart.write(chunk);
        dots -= chunk;
    }
}
