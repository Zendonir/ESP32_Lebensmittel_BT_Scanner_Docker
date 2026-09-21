#include "Printer.h"

#include <qrcode.h>

Printer printer;

static HardwareSerial uart(1);

void Printer::begin(uint32_t baud) {
    // Grosser Sendepuffer: bei 9600 Baud braucht ein Etikett rund eine halbe
    // Sekunde Leitungszeit. Passt es komplett in den Puffer, kehrt write()
    // sofort zurueck und der UART-Treiber sendet im Hintergrund weiter - sonst
    // steht der ganze Loop (und damit die Bedienung) waehrend des Druckens.
    //
    // 4 KB statt 2: ein QR-Code als Bitmap ist allein rund ein Kilobyte, dazu
    // kommen die Textzeilen. Mit 2 KB und der alten Schwelle von 1 KB konnte
    // ein Etikett groesser sein als der freie Platz - dann lief write() doch
    // wieder auf die 9600-Baud-Leitung und der Loop stand.
    uart.setTxBufferSize(4096);
    uart.begin(baud, SERIAL_8N1, PRINTER_RX, PRINTER_TX);
    _ready = true;
    reset();
}

void Printer::reset() {
    uart.write((const uint8_t *)"\x1B\x40", 2);   // ESC @ - Initialisieren
    uart.write((const uint8_t *)"\x1B\x74\x10", 3);  // ESC t 16 - Codepage WPC1252
    // ESC @ stellt den Standard-Zeilenabstand des Druckers wieder her (laut
    // Spezifikation 1/6 Zoll, also rund 34 Punkte bei 203 dpi). Was das
    // konkret ist, weiss nur der Drucker - deshalb gilt er hier als
    // unbekannt, und der erste Block setzt ihn in jedem Fall neu.
    _spacing = -1;
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

    // set() kopiert tief und kann dabei am Heap scheitern. Vorher wurde das
    // nicht geprueft: der halbe Auftrag kam in die Warteschlange, wurde als
    // leeres Etikett ausgeworfen und dem Server als gedruckt gemeldet. Das
    // Etikett fehlte damit endgueltig - kein erneuter Versuch, kein Hinweis.
    // Jetzt lehnt das Geraet ab, und der Server reiht den Auftrag wieder ein.
    if (_queue[slot].doc.overflowed()) {
        log_e("Druckauftrag %d passt nicht in den Speicher - abgelehnt", jobId);
        _queue[slot].doc.clear();
        return false;
    }

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
    if (uart.availableForWrite() < 3072) return 0;

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

// Zeilenabstand ausdruecklich setzen (`ESC 3 n`, Angabe in Punkten).
//
// Das ist die Stelle, an der das Etikettenkonzept vorher auseinanderlief.
// `ESC @` in reset() stellt den *Standardabstand* des Druckers ein - laut
// Spezifikation 1/6 Zoll, bei 203 dpi also rund 34 Punkte. Der Server rechnet
// aber mit 24 (services/labels.LINE_DOTS), und niemand hat dem Drucker je
// gesagt, dass er sich daran halten soll. Beim klassischen Zuschnitt kamen so
// 314 statt 240 Punkten heraus: jedes Etikett schob 9,2 mm zu weit, nach drei
// Stueck war der Stapel ein ganzes Etikett verschoben.
//
// Jetzt schickt der Server zu jedem Block die Hoehe mit, mit der er gerechnet
// hat, und hier wird sie durchgesetzt.
void Printer::lineSpacing(uint8_t dots) {
    if (_spacing == (int16_t)dots) return;   // schon gesetzt, nichts zu tun
    uart.write(0x1B); uart.write('3'); uart.write(dots);
    _spacing = (int16_t)dots;
}

void Printer::writeBlocks(JsonArray blocks) {
    if (blocks.isNull()) return;
    for (JsonObject block : blocks) {
        const String type = String(block["t"] | "");
        // `h` ist die Hoehe, mit der der Server gerechnet hat. Fehlt sie
        // (aelterer Server), bleibt es beim bisherigen Verhalten.
        const uint16_t height = block["h"] | 0;

        if (type == "text") {
            textLine(String(block["v"] | ""), block["align"] | 0,
                     block["bold"] | false, block["large"] | false, height);
        } else if (type == "row") {
            row(String(block["k"] | ""), String(block["v"] | ""),
                block["underline"] | false, height);
        } else if (type == "sep") {
            separator(height);
        } else if (type == "qr") {
            qr(String(block["v"] | ""), height);
        } else if (type == "code128") {
            code128(String(block["v"] | ""), block["height"] | 40);
        } else if (type == "feed") {
            feedDots(block["dots"] | 0);
        } else if (type == "back") {
            backfeedDots(block["dots"] | 0);
        }
    }
}

void Printer::textLine(const String &text, uint8_t align, bool bold, bool large,
                       uint16_t height) {
    // Ohne Angabe die Schrifthoehe nehmen - genau das, was der Server
    // ohnehin rechnet.
    lineSpacing(height ? (uint8_t)min<uint16_t>(height, 255) : (large ? 48 : 24));

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

void Printer::row(const String &key, const String &value, bool underline,
                  uint16_t height) {
    lineSpacing(height ? (uint8_t)min<uint16_t>(height, 255) : 24);
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

void Printer::separator(uint16_t height) {
    lineSpacing(height ? (uint8_t)min<uint16_t>(height, 255) : 24);
    uart.write(0x1B); uart.write('a'); uart.write((uint8_t)0);
    for (uint8_t i = 0; i < _chars; i++) uart.write('-');
    uart.write((const uint8_t *)"\r\n", 2);
}

void Printer::code128(const String &data, uint8_t height) {
    if (data.isEmpty()) return;

    // Codeset B kennt genau die druckbaren ASCII-Zeichen (0x20-0x7E). Alles
    // andere - ein Umlaut aus einem Produktnamen kommt als zwei Bytes >= 0x80
    // an - ergibt keinen lesbaren Strichcode, sondern Streifen, die kein
    // Scanner entziffert. Und die geschweifte Klammer ist in diesem Befehl das
    // Fluchtzeichen fuer den Codesatzwechsel: eine einzelne "{" im Text haette
    // den Drucker mitten im Code auf Codeset A oder C umgeschaltet. Deshalb
    // wird hier gefiltert und "{" wie vorgesehen verdoppelt.
    String safe;
    safe.reserve(data.length() + 4);
    for (size_t i = 0; i < data.length(); i++) {
        const uint8_t c = (uint8_t)data[i];
        if (c < 0x20 || c > 0x7E) continue;
        if (c == '{') safe += '{';          // "{{" steht fuer eine echte "{"
        safe += (char)c;
    }

    // Die Laengenangabe ist ein einzelnes Byte. Passt der Code nicht hinein,
    // lieber gar keinen Strichcode drucken als einen abgeschnittenen - und
    // zwar bevor die Vorbereitungsbefehle auf der Leitung sind, sonst bleibt
    // der Drucker mit halber Einstellung zurueck.
    const size_t length = safe.length() + 2;
    if (safe.isEmpty() || length > 255) {
        log_w("Strichcode uebersprungen (%u brauchbare Zeichen)", (unsigned)safe.length());
        return;
    }

    // Abstand auf 0: `GS k` schiebt selbst genau die Strichcodehoehe vor. Der
    // Zeilenabstand des abschliessenden `\r\n` kaeme sonst obendrauf, und
    // genau den hatte der Server mit CODE128_FEED = 10 nur geraten.
    lineSpacing(0);
    uart.write(0x1B); uart.write('a'); uart.write(1);      // zentriert
    uart.write(0x1D); uart.write('h'); uart.write(height); // Hoehe in Dots
    uart.write(0x1D); uart.write('w'); uart.write(2);      // Modulbreite
    uart.write(0x1D); uart.write('H'); uart.write((uint8_t)0);  // keine Klartextzeile

    // GS k 73 n {B <Daten> - Codeset B deckt Ziffern und Grossbuchstaben ab.
    uart.write(0x1D); uart.write('k'); uart.write(73); uart.write((uint8_t)length);
    uart.write('{'); uart.write('B');
    uart.write((const uint8_t *)safe.c_str(), safe.length());
    uart.write((const uint8_t *)"\r\n", 2);
}

void Printer::qr(const String &data, uint16_t reserved) {
    if (data.isEmpty() || reserved == 0) return;

    // Der eingebaute QR-Befehl (GS ( k) fehlt vielen guenstigen Druckern.
    // Deshalb wird der Code selbst gerechnet und als Bitmap gedruckt - das
    // funktioniert auf jedem ESC/POS-Geraet.
    QRCode qrcode;
    uint8_t buffer[qrcode_getBufferSize(3)];
    uint8_t version = 0;
    for (uint8_t v = 1; v <= 3; v++) {
        if (qrcode_initText(&qrcode, buffer, v, ECC_MEDIUM, data.c_str()) == 0) { version = v; break; }
    }
    if (version == 0) {
        // Nicht einfach nichts tun: der Platz ist eingeplant, und wer ihn
        // nicht vorschiebt, verschiebt alle folgenden Etiketten.
        log_w("QR-Code passt in keine Version - Platz wird vorgeschoben");
        feedDots(reserved);
        return;
    }

    // Quadratische Module mit Ruhezone.
    //
    // Vorher steckten zwei Modulzeilen in einem `ESC *`-Durchgang (0xF0 oben,
    // 0x0F unten) - senkrecht also fest 4 Punkte je Modul, waagerecht aber
    // `scale`. Quadratisch war der Code damit nur bei Skalierung 4; bei der
    // Vorgabe "mittel" war er 3:4 gestaucht, bei "klein" 2:4. Eine Ruhezone
    // wurde gar nicht gedruckt. Beides zusammen ergibt Codes, an denen ein
    // Scanner scheitert.
    //
    // Die Skalierung wird jetzt aus dem Platz abgeleitet, den der Server
    // reserviert hat. Damit passt der Code immer genau hinein, egal welche
    // Version die Daten verlangen.
    const int modules = qrcode.size + 2 * QR_QUIET;
    int scale = reserved / modules;
    while (scale > 0 && (((modules * scale) + 7) / 8) * 8 > reserved) scale--;
    if (scale < 1) {
        log_w("QR-Code passt nicht in %u Punkte - Platz wird vorgeschoben", reserved);
        feedDots(reserved);
        return;
    }

    const int side = modules * scale;
    const int bands = (side + 7) / 8;
    const int emitted = bands * 8;

    uart.write(0x1B); uart.write('a'); uart.write(1);      // zentriert
    lineSpacing(8);                                        // ein Band je Zeile

    for (int band = 0; band < bands; band++) {
        uart.write(0x1B); uart.write('*'); uart.write((uint8_t)0);   // 8-Punkt-Einfachdichte
        uart.write((uint8_t)(side & 0xFF));
        uart.write((uint8_t)(side >> 8));

        for (int x = 0; x < side; x++) {
            const int mx = x / scale - QR_QUIET;
            uint8_t column = 0;
            for (uint8_t bit = 0; bit < 8; bit++) {
                const int y = band * 8 + bit;
                if (y >= side) break;
                const int my = y / scale - QR_QUIET;
                if (mx >= 0 && my >= 0 && mx < qrcode.size && my < qrcode.size &&
                    qrcode_getModule(&qrcode, mx, my)) {
                    column |= (uint8_t)(0x80 >> bit);
                }
            }
            uart.write(column);
        }
        uart.write((const uint8_t *)"\r\n", 2);
    }

    // Auf die reservierte Hoehe auffuellen. Die Baender sind ein Vielfaches
    // von 8, die Reservierung ist es auch - der Rest ist in aller Regel 0.
    if (reserved > emitted) feedDots((uint16_t)(reserved - emitted));
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
