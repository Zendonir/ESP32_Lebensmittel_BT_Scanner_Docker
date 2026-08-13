#include "Screen.h"

#include "config.h"

Screen screen;

// Layoutraster fuer 480x320 im Querformat.
static constexpr int16_t W = UI_WIDTH;
static constexpr int16_t H = UI_HEIGHT;
static constexpr int16_t STATUS_H = 26;
static constexpr int16_t TITLE_H = 40;
// Keine Fussleiste mehr: Zurueck laeuft ueber das Wischen (und den
// Zurueck-Taster), Bestaetigen steht dort, wo es hingehoert - im Bildschirm
// selbst. Die gewonnenen 46 Pixel gehen an die Inhalte, dadurch werden vor
// allem die Knoepfe in den Karten fingerfreundlich gross.
// Der Kopfbereich ist nicht fest hoch: mit Untertitel braucht er mehr. Vorher
// stand hier eine Konstante von 40 Pixeln - Titel (26) und Untertitel (18)
// zusammen sind aber 46, und die Differenz lief unter den Inhalt. Auf dem
// Startbildschirm schrieb der Untertitel quer durch die Kennzahlenkarten.
#define BODY_Y (_bodyY)
#define BODY_H (H - _bodyY)

// Exakt die Farbwerte aus dem Vorgaengerprojekt (display.cpp, RGB()-Makro auf
// RGB565 abgebildet) - nicht neu erfunden, 1:1 uebernommen.
static constexpr uint16_t C_BG = 0x0862;        // RGB(0x08,0x0C,0x10)
static constexpr uint16_t C_SURFACE = 0x10A3;   // RGB(0x12,0x17,0x1E)
static constexpr uint16_t C_SURFACE2 = 0x1905;  // RGB(0x1C,0x22,0x2A)
static constexpr uint16_t C_BORDER = 0x2967;    // RGB(0x28,0x2E,0x38)
static constexpr uint16_t C_TEXT = 0xEF9E;      // RGB(0xEC,0xF0,0xF4)
static constexpr uint16_t C_MUTED = 0x7C32;     // RGB(0x7A,0x84,0x90) - C_SUBTEXT
static constexpr uint16_t C_PRIMARY = 0x4CFF;   // RGB(0x4C,0x9E,0xFF) - C_ACCENT
static constexpr uint16_t C_OK = 0x2D89;        // RGB(0x2E,0xB0,0x48) - C_GREEN
static constexpr uint16_t C_WARN = 0xCC83;      // RGB(0xCC,0x92,0x18) - C_YELLOW
static constexpr uint16_t C_DANGER = 0xF228;    // RGB(0xF0,0x46,0x40) - C_RED

void Screen::begin() {
    if (!_spr.begin()) {
        log_e("Display-Puffer konnte nicht initialisiert werden");
        return;
    }
    _spr.fillSprite(C_BG);

    // PWM einmalig einrichten; setBrightness() schreibt danach nur noch den
    // Tastgrad. Ein erneutes ledcAttach() bei jeder Einstellungsaenderung
    // laesst die Hintergrundbeleuchtung kurz flackern.
    ledcAttach(LCD_BL, 5000, 8);
    setBrightness(_brightness);

    // Der Sprite liegt in PSRAM (TFT_eSPI nutzt bei CONFIG_SPIRAM_SUPPORT
    // heap_caps_malloc). Alles wird hinein gezeichnet und in einem Rutsch
    // ausgegeben - so gibt es kein Flackern und keine Teilbilder.
    _spr.setTextDatum(TL_DATUM);
    _ready = true;
}

void Screen::setBrightness(uint8_t percent) {
    _brightness = constrain(percent, 5, 100);
    ledcWrite(LCD_BL, map(_brightness, 0, 100, 0, 255));
}

// ---------------------------------------------------------------------------
uint16_t Screen::parseColor(const char *hex, uint16_t fallback) {
    if (!hex || hex[0] != '#' || strlen(hex) < 7) return fallback;
    const long value = strtol(hex + 1, nullptr, 16);
    const uint8_t r = (value >> 16) & 0xFF, g = (value >> 8) & 0xFF, b = value & 0xFF;
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

void Screen::apply(JsonDocument &doc) {
    _screen.clear();
    _screen.set(doc);
    _screenId = _screen["id"] | 0;
    _kind = String(_screen["kind"] | "message");
    _scroll = 0;
    _scrollAccumPx = 0;

    if (_kind == "date") {
        _dateValue = String(_screen["value"] | "");
    } else if (_kind == "number") {
        _numberValue = _screen["value"] | 1.0f;
    } else if (_kind == "keyboard") {
        _textValue = String(_screen["value"] | "");
        _kbShift = false;
        _kbNumeric = false;
    } else if (_kind == "datepad") {
        _dateDigits = "";
    }
    redraw();
}

void Screen::toast(const String &text, const String &level) {
    _toastText = text;
    _toastLevel = level;
    _toastUntil = millis() + 2500;
    redraw();
}

void Screen::setBanner(const String &text) {
    if (_banner == text) return;
    _banner = text;
    redraw();
}

void Screen::showBoot(const String &line1, const String &line2) {
    _spr.fillSprite(C_BG);
    _spr.setTextColor(C_TEXT, C_BG);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextFont(4);
    _spr.drawString(line1, W / 2, H / 2 - 16);
    _spr.setTextFont(2);
    _spr.setTextColor(C_MUTED, C_BG);
    _spr.drawString(line2, W / 2, H / 2 + 18);
    _spr.setTextDatum(TL_DATUM);
    commit();
}

void Screen::loop() {
    if (_toastUntil && millis() > _toastUntil) {
        _toastUntil = 0;
        _toastText = "";
        redraw();
    }
}

// ---------------------------------------------------------------------------
// Zeichnen
// ---------------------------------------------------------------------------
void Screen::redraw() {
    if (!_ready) return;
    _hits.clear();
    _spr.fillSprite(C_BG);

    _bodyY = bodyTop();
    drawStatusBar();
    drawTitle();

    if (_kind == "tiles") drawTiles();
    else if (_kind == "list") drawList();
    else if (_kind == "date") drawDate();
    else if (_kind == "number") drawNumber();
    else if (_kind == "keyboard") drawKeyboard();
    else if (_kind == "home") drawHome();
    else if (_kind == "cards") drawCards();
    else if (_kind == "datepad") drawDatePad();
    else drawMessage();

    if (_toastUntil) drawToast();
    commit();
}

void Screen::commit() {
    if (_spr.created()) _spr.pushSprite(0, 0);
}

void Screen::addHit(int16_t x, int16_t y, int16_t w, int16_t h, const String &id, bool isInput) {
    _hits.push_back({x, y, w, h, id, isInput});
}

void Screen::drawStatusBar() {
    _spr.fillRect(0, 0, W, STATUS_H, C_SURFACE);
    _spr.setTextFont(2);

    JsonObject status = _screen["status"].as<JsonObject>();
    const char *location = status["location"] | "";

    // Kein Ein-/Auslagern-Umschalter mehr: die Richtung entscheidet der
    // gescannte Code (Produktbarcode einlagern, Etikett auslagern). Ein
    // Umschalter waere nicht nur ueberfluessig, sondern irrefuehrend - und
    // eine Bestandsaenderung soll am Geraet nicht aus Versehen per Finger
    // ausloesbar sein.

    // Links die Bestandszahlen. Die standen bisher nur auf dem Startbildschirm,
    // waehrend die halbe Leiste leer blieb - dabei ist "wie viel laeuft ab"
    // genau die Zahl, die man beim Einraeumen im Auge behalten will.
    const int total = status["total"] | 0;
    const int expiring = status["expiring"] | 0;
    char counts[40];
    snprintf(counts, sizeof(counts), "%d im Bestand", total);
    _spr.setTextColor(C_MUTED, C_SURFACE);
    _spr.drawString(counts, 10, 5);
    if (expiring > 0) {
        char warn[24];
        snprintf(warn, sizeof(warn), "%d laufen ab", expiring);
        _spr.setTextColor(C_WARN, C_SURFACE);
        _spr.drawString(warn, 10 + _spr.textWidth(counts) + 12, 5);
    }

    // Lagerort als antippbare Pille rechts - oeffnet die Ortsauswahl, wie im
    // Vorgaengerprojekt das Badge oben rechts. Der Name wird eingepasst:
    // "Vorratskammer Keller" schrieb sonst ueber den Pillenrand hinaus.
    const int16_t pillH = STATUS_H - 6;
    const int16_t pillW = 150;
    const int16_t pillX = W - 84 - pillW;
    const int16_t pillY = 3;
    _spr.fillRoundRect(pillX, pillY, pillW, pillH, pillH / 2, C_PRIMARY);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextColor(TFT_WHITE, C_PRIMARY);
    _spr.drawString(fitText(location[0] ? location : "kein Ort", pillW - 16, 2),
                    pillX + pillW / 2, pillY + pillH / 2 + 1);
    _spr.setTextFont(2);
    _spr.setTextDatum(TL_DATUM);
    addHit(pillX, pillY, pillW, pillH, "locations");

    // Rechts: Scanner-Akku und Verbindungspunkt.
    const int battery = status["battery"] | -1;
    if (battery >= 0) {
        char buf[12];
        snprintf(buf, sizeof(buf), "%d%%", battery);
        _spr.setTextColor(battery < 10 ? C_DANGER : C_MUTED, C_SURFACE);
        _spr.drawString(buf, W - 74, 5);
    }
    const bool scanner = status["scanner"] | false;
    _spr.fillCircle(W - 22, STATUS_H / 2, 5, scanner ? C_OK : C_DANGER);

    if (!_banner.isEmpty()) {
        _spr.fillRect(0, STATUS_H, W, 18, C_WARN);
        _spr.setTextColor(TFT_BLACK, C_WARN);
        _spr.drawString(_banner, 8, STATUS_H + 1);
    }
}

void Screen::drawTitle() {
    const int16_t y = STATUS_H + (_banner.isEmpty() ? 0 : 18);
    _spr.setTextColor(C_TEXT, C_BG);
    _spr.drawString(fitText(String(_screen["title"] | ""), W - 20, 4), 10, y + 3);

    const char *subtitle = _screen["subtitle"] | "";
    if (subtitle[0]) {
        _spr.setTextColor(C_MUTED, C_BG);
        _spr.drawString(fitText(subtitle, W - 20, 2), 10, y + 29);
    }
}

// Oberkante des Inhalts. Haengt am Banner und daran, ob es einen Untertitel
// gibt - beides bekommt eigenen Platz, statt in den Inhalt zu laufen.
int16_t Screen::bodyTop() const {
    const char *subtitle = _screen["subtitle"] | "";
    return STATUS_H + (_banner.isEmpty() ? 0 : 18) + (subtitle[0] ? 50 : 34);
}

// Schriftfarbe passend zur Flaeche: auf den bunten Knoepfen dunkel, auf den
// grauen hell. Im Vorgaengerprojekt stand das je Knopf von Hand da (C_BG auf
// Blau/Gruen/Gelb, C_TEXT auf Grau). Hier gerechnet statt aufgeschrieben,
// damit es auch fuer Farben stimmt, die der Server frei mitschickt.
static uint16_t textOn(uint16_t bg) {
    const uint8_t r = ((bg >> 11) & 0x1F) << 3;
    const uint8_t g = ((bg >> 5) & 0x3F) << 2;
    const uint8_t b = (bg & 0x1F) << 3;
    const uint16_t luminance = (299u * r + 587u * g + 114u * b) / 1000u;
    return luminance > 110 ? C_BG : C_TEXT;
}

// Beschriftung so einpassen, dass sie innerhalb der Flaeche bleibt: erst eine
// Schriftstufe kleiner, dann notfalls kuerzen. Die Schrift setzt die Funktion
// gleich mit - der Aufrufer zeichnet danach einfach.
//
// Vorher wurde nur eine Stufe kleiner gewaehlt und dann gezeichnet, egal ob es
// passte. Bei einer proportionalen Schrift laesst sich die Breite nicht mehr
// aus der Zeichenzahl abschaetzen, deshalb wird hier wirklich gemessen.
String Screen::fitText(const String &text, int16_t maxWidth, uint8_t font) {
    static const uint8_t STEPS[] = {6, 4, 2, 1};

    uint8_t chosen = font;
    for (uint8_t step : STEPS) {
        if (step > font) continue;
        chosen = step;
        _spr.setTextFont(step);
        if (_spr.textWidth(text) <= maxWidth) return text;
    }

    // Auch in der kleinsten Stufe zu breit - kuerzen. Als Kuerzungszeichen "~"
    // wie im Vorgaengerprojekt: die Auslassungspunkte "…" liegen ausserhalb
    // von Latin-1 und haetten in der Schrift kein Zeichen.
    _spr.setTextFont(chosen);
    String out = text;
    while (out.length() > 1 && _spr.textWidth(out + "~") > maxWidth) {
        out.remove(out.length() - 1);
        // Eine Mehrbyte-Folge nicht mittendrin abschneiden, sonst steht dort
        // ein kaputtes Zeichen.
        while (out.length() && ((uint8_t)out[out.length() - 1] & 0xC0) == 0x80) {
            out.remove(out.length() - 1);
        }
    }
    return out + "~";
}

// Groesste Schriftstufe, in der *alle* Beschriftungen des Rasters passen.
//
// fitText entscheidet je Kachel. Bei "Backwaren" neben "Fleisch & Fisch" steht
// die eine dann in Stufe 4 und die andere in Stufe 2 - dasselbe Raster in zwei
// Schriftgroessen sieht nach Versehen aus. Deshalb einmal fuer alle bestimmen.
uint8_t Screen::gridFont(JsonArray items, int16_t maxTextW) {
    static const uint8_t STEPS[] = {4, 2, 1};
    for (uint8_t step : STEPS) {
        _spr.setTextFont(step);
        bool passt = true;
        for (JsonObject item : items) {
            if (_spr.textWidth(String(item["label"] | "")) > maxTextW) { passt = false; break; }
        }
        if (passt) return step;
    }
    return 1;
}

void Screen::tile(int16_t x, int16_t y, int16_t w, int16_t h, const String &label,
                  const String &sub, uint16_t color, const String &id, uint8_t font) {
    _spr.fillRoundRect(x, y, w, h, 8, color);
    _spr.drawRoundRect(x, y, w, h, 8, C_BORDER);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextColor(textOn(color), color);

    const int16_t maxTextW = w - 12;
    _spr.drawString(fitText(label, maxTextW, font), x + w / 2,
                    y + h / 2 - (sub.isEmpty() ? 0 : 10));
    if (!sub.isEmpty()) {
        _spr.drawString(fitText(sub, maxTextW, 2), x + w / 2, y + h / 2 + 14);
    }
    _spr.setTextDatum(TL_DATUM);
    addHit(x, y, w, h, id);
}

void Screen::button(int16_t x, int16_t y, int16_t w, int16_t h, const String &label,
                    uint16_t bg, const String &id) {
    _spr.fillRoundRect(x, y, w, h, 6, bg);
    _spr.drawRoundRect(x, y, w, h, 6, C_BORDER);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextColor(textOn(bg), bg);

    _spr.drawString(fitText(label, w - 8, 2), x + w / 2, y + h / 2);
    _spr.setTextDatum(TL_DATUM);
    addHit(x, y, w, h, id);
}

void Screen::drawTiles() {
    JsonArray items = _screen["items"].as<JsonArray>();
    const int count = items.size();
    if (count == 0) return;

    const int cols = count <= 4 ? 2 : 3;
    const int rows = (count + cols - 1) / cols;
    const int16_t gap = 8;
    const int16_t w = (W - gap * (cols + 1)) / cols;
    const int16_t h = min<int16_t>((BODY_H - gap * (rows + 1)) / rows, 92);

    const uint8_t font = gridFont(items, w - 12);
    int index = 0;
    for (JsonObject item : items) {
        const int row = index / cols, col = index % cols;
        tile(gap + col * (w + gap), BODY_Y + gap + row * (h + gap), w, h,
             String(item["label"] | ""), String(item["sub"] | ""),
             parseColor(item["color"] | "", C_PRIMARY), String(item["id"] | ""), font);
        index++;
    }
}

void Screen::scrollBy(int16_t deltaYPx) {
    if (_kind != "list" || _listTotal <= _pageSize) return;

    // Zeilen laufen mit dem Finger mit: nach oben ziehen (deltaY negativ)
    // blaettert vorwaerts durch die Liste.
    _scrollAccumPx -= deltaYPx;
    const int16_t rowH = 44;
    const int prevScroll = _scroll;
    while (_scrollAccumPx >= rowH) { _scrollAccumPx -= rowH; _scroll++; }
    while (_scrollAccumPx <= -rowH) { _scrollAccumPx += rowH; _scroll--; }

    const int maxScroll = max(0, _listTotal - _pageSize);
    if (_scroll < 0) { _scroll = 0; _scrollAccumPx = 0; }
    if (_scroll > maxScroll) { _scroll = maxScroll; _scrollAccumPx = 0; }

    if (_scroll != prevScroll) redraw();
}

void Screen::scrollByRows(int rows) {
    // Die Taster springen ganze Zeilen - anders als das Ziehen, das dem Finger
    // pixelweise folgt.
    scrollBy((int16_t)(-rows * 44));
}

void Screen::drawList() {
    JsonArray items = _screen["items"].as<JsonArray>();
    const int total = items.size();
    const int16_t rowH = 44;

    // Steuerzeile ueber der Liste (Sortierung, Suche). Die sass frueher in der
    // Fussleiste; im Vorgaengerprojekt stand sie ueber der Inventarliste, und
    // dort gehoert sie auch hin - sie betrifft die Liste, nicht den Bildschirm.
    int16_t listY = BODY_Y;
    JsonArray controls = _screen["meta"]["controls"].as<JsonArray>();
    if (!controls.isNull() && controls.size() > 0) {
        const int count = controls.size();
        const int16_t w = (W - 8 * (count + 1)) / count;
        int index = 0;
        for (JsonObject control : controls) {
            button(8 + index * (w + 8), listY, w, 34, String(control["label"] | ""),
                   C_SURFACE2, String(control["id"] | ""));
            index++;
        }
        listY += 40;
    }

    const int16_t listH = H - listY;
    _pageSize = listH / rowH;
    _listTotal = total;

    if (_scroll > max(0, total - _pageSize)) _scroll = max(0, total - _pageSize);

    for (int i = 0; i < _pageSize && (_scroll + i) < total; i++) {
        JsonObject item = items[_scroll + i];
        const int16_t y = listY + i * rowH;

        // Gruppenueberschrift: nicht antippbar, eigener Zeilenstil. Zaehlt als
        // gewoehnliche Zeile fuer die Seitenberechnung - das haelt das Blaettern
        // einfach und vorhersehbar, kostet dafuer etwas Platz.
        if (item["header"] | false) {
            _spr.setTextFont(2);
            _spr.setTextColor(C_MUTED, C_BG);
            _spr.drawString(String(item["label"] | ""), 10, y + rowH / 2 - 8);
            continue;
        }

        const uint16_t color = parseColor(item["color"] | "", C_PRIMARY);

        _spr.fillRoundRect(8, y + 2, W - 16 - (total > _pageSize ? 34 : 0), rowH - 6, 6, C_SURFACE);
        _spr.fillRoundRect(8, y + 2, 5, rowH - 6, 3, color);

        // Produktnamen sind haeufig laenger als die Zeile - abschneiden statt
        // ueber den Rand und die Bildlaufleiste hinauszuschreiben.
        const int16_t textW = W - 16 - (total > _pageSize ? 34 : 0) - 22;
        // Zwei Zeilen in 38 Pixel Zeilenhoehe: der Name in Stufe 4 ist 21
        // Pixel hoch, die Unterzeile in Stufe 2 noch einmal 16 - zusammen mehr
        // als die Zeile hergibt, und die Unterzeile lief in den Namen.
        // Die Unterzeile steht deshalb in der kleinen Stufe.
        const char *sub = item["sub"] | "";
        _spr.setTextColor(C_TEXT, C_SURFACE);
        _spr.drawString(fitText(String(item["label"] | ""), textW, 4),
                        22, y + (sub[0] ? 3 : 9));
        if (sub[0]) {
            _spr.setTextColor(C_MUTED, C_SURFACE);
            _spr.drawString(fitText(sub, textW, 1), 22, y + 26);
        }
        addHit(8, y, W - 16, rowH - 4, String(item["id"] | ""));
    }

    // Bildlaufleiste als zwei grosse Flaechen - fuer Finger, nicht fuer Maeuse.
    if (total > _pageSize) {
        button(W - 32, listY, 26, listH / 2 - 3, "^", C_SURFACE, "__up");
        button(W - 32, listY + listH / 2 + 3, 26, listH / 2 - 3, "v", C_SURFACE, "__down");
    }
}

String Screen::shiftDate(const String &iso, int days, int months) const {
    struct tm tm {};
    if (iso.length() == 10) {
        tm.tm_year = iso.substring(0, 4).toInt() - 1900;
        tm.tm_mon  = iso.substring(5, 7).toInt() - 1;
        tm.tm_mday = iso.substring(8, 10).toInt();
    } else {
        const time_t now = time(nullptr);
        localtime_r(&now, &tm);
    }
    tm.tm_mday += days;
    tm.tm_mon  += months;
    tm.tm_hour = 12;   // Mittag: schuetzt vor Sommerzeit-Sprüngen um Mitternacht
    const time_t stamp = mktime(&tm);
    struct tm out {};
    localtime_r(&stamp, &out);

    char buf[11];
    strftime(buf, sizeof(buf), "%Y-%m-%d", &out);
    return String(buf);
}

void Screen::drawDate() {
    // Grosse Datumsanzeige
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextFont(6);
    _spr.setTextColor(C_TEXT, C_BG);
    String shown = "--.--.----";
    if (_dateValue.length() == 10) {
        shown = _dateValue.substring(8, 10) + "." + _dateValue.substring(5, 7) + "." +
                _dateValue.substring(0, 4);
    }
    _spr.drawString(shown, W / 2, BODY_Y + 26);
    _spr.setTextDatum(TL_DATUM);

    // Schrittweisen: Tag und Monat
    const int16_t y = BODY_Y + 56;
    button(10, y, 68, 34, "-1 Mon", C_SURFACE, "__m-");
    button(84, y, 60, 34, "-1 Tag", C_SURFACE, "__d-");
    button(W - 144, y, 60, 34, "+1 Tag", C_SURFACE, "__d+");
    button(W - 78, y, 68, 34, "+1 Mon", C_SURFACE, "__m+");

    // Voreinstellungen des Servers
    JsonArray presets = _screen["meta"]["presets"].as<JsonArray>();
    int index = 0;
    const int16_t py = y + 42;
    const int16_t pw = (W - 20 - 5 * 6) / 6;
    for (JsonObject preset : presets) {
        if (index >= 6) break;
        button(10 + index * (pw + 6), py, pw, 32, String(preset["label"] | ""),
               C_PRIMARY, String(preset["id"] | ""));
        index++;
    }
}

void Screen::drawNumber() {
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextFont(7);
    _spr.setTextColor(C_TEXT, C_BG);
    char buf[16];
    if (_numberValue == (long)_numberValue) snprintf(buf, sizeof(buf), "%ld", (long)_numberValue);
    else snprintf(buf, sizeof(buf), "%.1f", _numberValue);
    _spr.drawString(buf, W / 2, BODY_Y + 40);

    _spr.setTextFont(2);
    _spr.setTextColor(C_MUTED, C_BG);
    _spr.drawString(String(_screen["meta"]["unit"] | ""), W / 2, BODY_Y + 78);
    _spr.setTextDatum(TL_DATUM);

    const float step = _screen["meta"]["step"] | 1.0f;
    const int16_t y = BODY_Y + 94;
    button(14, y, 100, 52, "-" + String(step * 10, step < 1 ? 1 : 0), C_SURFACE2, "__n--");
    button(122, y, 100, 52, "-" + String(step, step < 1 ? 1 : 0), C_SURFACE2, "__n-");
    button(W - 222, y, 100, 52, "+" + String(step, step < 1 ? 1 : 0), C_SURFACE2, "__n+");
    button(W - 114, y, 100, 52, "+" + String(step * 10, step < 1 ? 1 : 0), C_SURFACE2, "__n++");

    // Bestaetigen steht jetzt im Bildschirm statt in der Fussleiste - und
    // darf entsprechend gross ausfallen.
    button(14, H - 62, W - 28, 52, "Übernehmen", C_OK, "ok");
}

// Bildschirmtastatur: drei Reihen Buchstaben (oder im Ziffernmodus Ziffern und
// Satzzeichen) plus eine Bedienreihe mit Umschalt/Modus, Leerzeichen und OK.
// Reihen sind unterschiedlich breit wie bei einer echten Tastatur - das ist
// bewusst kein einheitliches Raster, sondern an app/DisplayCanvas'
// Textbreite orientiert.
static const char KB_ROW1[] = "qwertzuiop";                // 10 Zeichen, QWERTZ
static const char KB_ROW2[] = "asdfghjkl";                  // 9 Zeichen
static const char KB_ROW3[] = "yxcvbnm";                    // 7 Zeichen + Ruecktaste
static const char KB_NUM1[] = "1234567890";
static const char KB_NUM2[] = "-_/:;()&@";
static const char KB_NUM3[] = ".,?!'\"";

char Screen::keyboardCharAt(uint8_t row, uint8_t col) const {
    const char *src = nullptr;
    if (_kbNumeric) {
        src = row == 0 ? KB_NUM1 : row == 1 ? KB_NUM2 : KB_NUM3;
    } else {
        src = row == 0 ? KB_ROW1 : row == 1 ? KB_ROW2 : KB_ROW3;
    }
    const char c = src[col];
    if (c == 0) return 0;
    if (!_kbNumeric && _kbShift) return (char)toupper(c);
    return c;
}

void Screen::drawKeyboard() {
    // Eingabezeile
    const int16_t inputH = 30;
    _spr.fillRoundRect(8, BODY_Y, W - 16, inputH, 5, C_SURFACE);
    _spr.setTextFont(4);
    _spr.setTextColor(_textValue.isEmpty() ? C_MUTED : C_TEXT, C_SURFACE);
    _spr.drawString(_textValue.isEmpty() ? "..." : (_textValue + "_"), 14, BODY_Y + 4);

    const int16_t gap = 3;

    // Tastenhoehe aus dem vorhandenen Platz statt fest 36 Pixel. Die vier
    // Reihen endeten sonst rund 60 Pixel ueber dem unteren Rand - Platz, der
    // bei einer Tastatur, die mit dem Finger bedient wird, nirgends besser
    // aufgehoben ist als in der Tastengroesse.
    const int16_t keysY = BODY_Y + inputH + 6;
    const int16_t rowH = (H - 6 - keysY - 3 * gap) / 4;

    // Zeichenreihe zeichnen: `count` Tasten plus optional eine breitere
    // Sondertaste (Ruecktaste) im letzten Slot dieser Zeile.
    auto drawCharRow = [&](uint8_t row, int count, int16_t y, bool withBackspace) {
        const int slots = count + (withBackspace ? 1 : 0);
        const int16_t w = (W - 12 - (slots - 1) * gap) / slots;
        int16_t x = 6;
        for (int col = 0; col < count; col++) {
            const char c = keyboardCharAt(row, col);
            char label[2] = {c, 0};
            button(x, y, w, rowH, label, C_SURFACE, "K" + String(c));
            x += w + gap;
        }
        if (withBackspace) button(x, y, w, rowH, "<-", C_DANGER, "__kbback");
    };

    int16_t y = keysY;
    drawCharRow(0, _kbNumeric ? strlen(KB_NUM1) : strlen(KB_ROW1), y, false);
    y += rowH + gap;
    drawCharRow(1, _kbNumeric ? strlen(KB_NUM2) : strlen(KB_ROW2), y, false);
    y += rowH + gap;
    drawCharRow(2, _kbNumeric ? strlen(KB_NUM3) : strlen(KB_ROW3), y, true);
    y += rowH + gap;

    // Bedienreihe: ein einzelner Knopf durchlaeuft klein -> GROSS -> 123 -> klein,
    // damit fuer Umschalten und Ziffernmodus keine zwei separaten Tasten den
    // knappen Platz der Bedienreihe teilen muessen. Der Text zeigt das Ziel des
    // naechsten Tipps, nicht den aktuellen Zustand.
    const char *modeLabel = _kbNumeric ? "abc" : (_kbShift ? "123" : "ABC");
    const int16_t modeW = 70, okW = 90;
    const int16_t spaceW = W - 12 - modeW - okW - 2 * gap;
    button(6, y, modeW, rowH, modeLabel, C_SURFACE, "__kbmode");
    button(6 + modeW + gap, y, spaceW, rowH, "LEERTASTE", C_SURFACE, "K ");
    button(6 + modeW + gap + spaceW + gap, y, okW, rowH, "OK", C_OK, "ok");
}

void Screen::drawMessage() {
    JsonArray lines = _screen["lines"].as<JsonArray>();
    int16_t y = BODY_Y + 6;
    _spr.setTextFont(4);
    _spr.setTextColor(C_TEXT, C_BG);
    for (JsonVariant line : lines) {
        _spr.drawString(String(line.as<const char *>()), 12, y);
        y += 26;
        if (y > BODY_Y + BODY_H - 24) break;
    }

    // Bei message-Bildschirmen sind items grosse Bestaetigungskacheln.
    JsonArray items = _screen["items"].as<JsonArray>();
    if (!items.isNull() && items.size() > 0) {
        const int count = items.size();
        const int16_t w = (W - 10 * (count + 1)) / count;
        int index = 0;
        for (JsonObject item : items) {
            tile(10 + index * (w + 10), BODY_Y + BODY_H - 58, w, 52,
                 String(item["label"] | ""), String(item["sub"] | ""),
                 parseColor(item["color"] | "", C_PRIMARY), String(item["id"] | ""));
            index++;
        }
    }
}

// Startbildschirm - Portierung von draw_panel_store() aus dem
// Vorgaengerprojekt (display.cpp): Kennzahlenreihe mit farbigem Streifen,
// WLAN/BLE-Pillen + Etikettenrolle, darunter das 2x2-Kachelraster.
void Screen::drawHome() {
    int16_t y = BODY_Y;

    JsonArray stats = _screen["meta"]["stats"].as<JsonArray>();
    const int statCount = stats.size();
    if (statCount > 0) {
        const int16_t gap = 4;
        const int16_t h = 64;
        const int16_t w = (W - gap * (statCount + 1)) / statCount;
        int index = 0;
        for (JsonObject s : stats) {
            const uint16_t color = parseColor(s["color"] | "", C_PRIMARY);
            const int16_t x = gap + index * (w + gap);
            _spr.fillRoundRect(x, y, w, h, 10, C_SURFACE);
            _spr.drawRoundRect(x, y, w, h, 10, color);
            _spr.fillRect(x + 1, y + 1, 4, h - 2, color);
            _spr.setTextColor(color, C_SURFACE);
            _spr.setTextFont(4);
            _spr.drawString(String((long)(s["value"] | 0)), x + 10, y + 8);
            _spr.setTextColor(C_MUTED, C_SURFACE);
            _spr.setTextFont(2);
            _spr.drawString(String(s["label"] | ""), x + 10, y + 38);
            // Wie im Vorgaengerprojekt: Produkte- und Ablaufend-Karten sind
            // Abkuerzungen in die jeweilige Ansicht.
            if (index == 0) addHit(x, y, w, h, "inventory");
            else if (index == 1 || index == 2) addHit(x, y, w, h, "expiring");
            index++;
        }
        y += h + 8;
    }

    // WLAN/BLE-Pillen, Breite nach Textinhalt (wie draw_pill im
    // Vorgaengerprojekt) - reine Anzeige, das Detail steht im System-Panel.
    const bool wifiOk = _screen["meta"]["wifi"] | true;
    const bool bleOk  = _screen["meta"]["ble"]  | false;
    _spr.setTextFont(2);
    auto pill = [&](int16_t px, const String &label, uint16_t bg) -> int16_t {
        const int16_t tw = _spr.textWidth(label) + 16;
        _spr.fillRoundRect(px, y, tw, 22, 11, bg);
        _spr.setTextDatum(MC_DATUM);
        _spr.setTextColor(C_TEXT, bg);
        _spr.drawString(label, px + tw / 2, y + 11);
        _spr.setTextDatum(TL_DATUM);
        return px + tw + 6;
    };
    const int16_t afterWifi = pill(4, wifiOk ? "WLAN OK" : "WLAN FEHLT", wifiOk ? C_OK : C_DANGER);
    pill(afterWifi, bleOk ? "BLE OK" : "BLE ---", bleOk ? C_OK : C_SURFACE2);
    button(W - 120, y - 2, 116, 26, "Neue Rolle", C_SURFACE2, "new_roll");
    y += 30;

    // Kachelraster mit den verbleibenden Bildschirmaktionen.
    JsonArray items = _screen["items"].as<JsonArray>();
    const int count = items.size();
    if (count == 0) return;
    const int cols = 2;
    const int rows = (count + cols - 1) / cols;
    const int16_t gap = 4;
    const int16_t tw = (W - gap * (cols + 1)) / cols;
    const int16_t th = max<int16_t>(40, (BODY_Y + BODY_H - y - gap * (rows + 1)) / rows);
    const uint8_t font = gridFont(items, tw - 12);
    int index = 0;
    for (JsonObject item : items) {
        const int row = index / cols, col = index % cols;
        tile(gap + col * (tw + gap), y + gap + row * (th + gap), tw, th,
             String(item["label"] | ""), String(item["sub"] | ""),
             parseColor(item["color"] | "", C_PRIMARY), String(item["id"] | ""), font);
        index++;
    }
}

// System-Panel: bis zu vier umrandete Statuskarten (Netzwerk, BLE-Scanner,
// Geraet, System) mit optionalem Knopf am unteren Kartenrand.
void Screen::drawCards() {
    JsonArray cards = _screen["meta"]["cards"].as<JsonArray>();
    const int count = cards.size();
    if (count == 0) return;

    const int cols = 2;
    const int rows = (count + cols - 1) / cols;
    const int16_t gap = 8;
    const int16_t w = (W - gap * (cols + 1)) / cols;
    const int16_t h = (BODY_H - gap * (rows + 1)) / rows;

    int index = 0;
    for (JsonObject card : cards) {
        const int row = index / cols, col = index % cols;
        const int16_t x = gap + col * (w + gap);
        const int16_t y = BODY_Y + gap + row * (h + gap);
        const uint16_t titleColor = parseColor(card["title_color"] | "", C_PRIMARY);

        _spr.drawRoundRect(x, y, w, h, 8, titleColor);

        _spr.setTextFont(2);
        _spr.setTextColor(titleColor, C_BG);
        _spr.drawString(fitText(String(card["title"] | ""), w - 20, 2), x + 10, y + 6);

        // Die grosse Statuszeile ist optional - Karten ohne Schlagzeile (z.B.
        // "System") gewinnen den Platz fuer eine Zeile mehr. Der Titel selbst
        // ist 16 Pixel hoch; bei y+20 schrieb die erste Zeile hinein.
        int16_t ly = y + 26;
        const char *statusText = card["status"] | "";
        if (statusText[0]) {
            _spr.setTextFont(4);
            _spr.setTextColor(parseColor(card["status_color"] | "", C_TEXT), C_BG);
            _spr.drawString(statusText, x + 10, ly);
            ly += 26;
        }

        JsonObject btn = card["button"];
        const int16_t bottomLimit = btn.isNull() ? (y + h - 6) : (y + h - 40);

        _spr.setTextFont(2);
        _spr.setTextColor(C_MUTED, C_BG);
        JsonArray lines = card["lines"].as<JsonArray>();
        for (JsonVariant line : lines) {
            if (ly + 13 > bottomLimit) break;
            // Einpassen statt einfach schreiben: eine Zeile wie "noch keine
            // Angaben vom Geraet" ist breiter als die Karte und lief bisher
            // ueber den Rand in die Nachbarkarte.
            _spr.drawString(fitText(String(line.as<const char *>()), w - 20, 2), x + 10, ly);
            _spr.setTextFont(2);
            ly += 14;
        }

        if (!btn.isNull()) {
            button(x + 8, y + h - 38, w - 16, 32, String(btn["label"] | ""),
                   parseColor(btn["color"] | "", C_SURFACE), String(btn["id"] | ""));
        }
        index++;
    }
}

// Welche Ziffern an der aktuellen Stelle moeglich sind - 1:1 aus dem
// Vorgaengerprojekt (showDateEntry). Unmoegliche Tasten werden ausgegraut und
// nehmen keine Beruehrung an; damit kann gar kein unsinniges Datum entstehen.
uint16_t Screen::datePadValidDigits() const {
    switch (_dateDigits.length()) {
        case 0:                                   // Zehner des Tages: 0-3
            return 0x000F;
        case 1:                                   // Einer des Tages
            if (_dateDigits[0] == '0') return 0x03FE;   // 1-9, kein Tag 00
            if (_dateDigits[0] == '3') return 0x0003;   // nur 30 und 31
            return 0x03FF;
        case 2:                                   // Zehner des Monats: 0-1
            return 0x0003;
        case 3:                                   // Einer des Monats
            if (_dateDigits[2] == '0') return 0x03FE;   // 1-9, kein Monat 00
            return 0x0007;                              // 10, 11, 12
        default:                                  // Jahr: alles erlaubt
            return 0x03FF;
    }
}

// MHD-Eingabe, nachgebaut nach showDateEntry() aus dem Vorgaengerprojekt:
// links Produktangaben und die grosse Datumsanzeige, rechts ein Ziffernblock.
// Nach der sechsten Ziffer wird von selbst uebernommen - ohne Bestaetigen.
void Screen::drawDatePad() {
    static constexpr int16_t DIV_X = 240;
    _spr.fillRect(DIV_X, BODY_Y, 1, H - BODY_Y, C_BORDER);

    // ---- linke Haelfte: Produkt und Datum -------------------------------
    JsonArray lines = _screen["lines"].as<JsonArray>();
    int16_t ly = BODY_Y + 8;
    int index = 0;
    for (JsonVariant line : lines) {
        if (index > 1) break;
        _spr.setTextColor(index == 0 ? C_TEXT : C_MUTED, C_BG);
        _spr.drawString(fitText(String(line.as<const char *>()), DIV_X - 20, 2), 10, ly);
        ly += 20;
        index++;
    }

    _spr.fillRect(10, BODY_Y + 52, DIV_X - 20, 1, C_BORDER);
    _spr.setTextFont(2);
    _spr.setTextColor(C_MUTED, C_BG);
    _spr.drawString("MHD Eingabe:", 10, BODY_Y + 58);

    const int16_t boxX = 10, boxY = BODY_Y + 76, boxW = DIV_X - 20, boxH = 76;
    _spr.fillRoundRect(boxX, boxY, boxW, boxH, 10, C_SURFACE);
    _spr.drawRoundRect(boxX, boxY, boxW, boxH, 10, C_PRIMARY);

    // Platzhalter TT.MM.JJ, von links mit den getippten Ziffern gefuellt.
    static const char PLACEHOLDER[6] = {'T', 'T', 'M', 'M', 'J', 'J'};
    String shown;
    for (int i = 0; i < 6; i++) {
        shown += (i < (int)_dateDigits.length()) ? _dateDigits[i] : PLACEHOLDER[i];
        if (i == 1 || i == 3) shown += '.';
    }
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextColor(C_PRIMARY, C_SURFACE);
    _spr.drawString(fitText(shown, boxW - 12, 6), boxX + boxW / 2, boxY + boxH / 2);
    _spr.setTextDatum(TL_DATUM);

    // Knoepfe unter der Anzeige - was dort steht, bestimmt der Server.
    JsonArray items = _screen["items"].as<JsonArray>();
    const int count = items.size();
    if (count > 0) {
        const int16_t bh = 40, gap = 6;
        int16_t by = H - 6 - count * bh - (count - 1) * gap;
        for (JsonObject item : items) {
            button(10, by, DIV_X - 20, bh, String(item["label"] | ""),
                   parseColor(item["color"] | "", C_SURFACE2), String(item["id"] | ""));
            by += bh + gap;
        }
    }

    // ---- rechte Haelfte: Ziffernblock -----------------------------------
    const int16_t npX = DIV_X + 1;
    const int16_t npW = W - npX;
    const int16_t btnW = npW / 3;
    const int16_t btnH = (H - BODY_Y) / 4;
    const uint16_t valid = datePadValidDigits();

    auto key = [&](int16_t x, int16_t y, int16_t w, const String &label,
                   bool enabled, const String &id, uint16_t bg) {
        _spr.fillRect(x, y, w, btnH, bg);
        _spr.fillRect(x, y, w, 1, C_BORDER);
        _spr.fillRect(x, y, 1, btnH, C_BORDER);
        _spr.setTextDatum(MC_DATUM);
        _spr.setTextColor(enabled ? C_TEXT : C_MUTED, bg);
        _spr.drawString(fitText(label, w - 8, 6), x + w / 2, y + btnH / 2);
        _spr.setTextDatum(TL_DATUM);
        if (enabled) addHit(x, y, w, btnH, id);
    };

    for (int i = 0; i < 9; i++) {
        const int digit = i + 1;
        const bool enabled = (valid >> digit) & 1;
        key(npX + (i % 3) * btnW, BODY_Y + (i / 3) * btnH, btnW, String(digit),
            enabled, "__dp" + String(digit), enabled ? C_SURFACE2 : C_SURFACE);
    }

    const int16_t lastY = BODY_Y + 3 * btnH;
    const bool zeroOk = valid & 1;
    key(npX, lastY, btnW * 2, "0", zeroOk, "__dp0", zeroOk ? C_SURFACE2 : C_SURFACE);
    key(npX + btnW * 2, lastY, btnW, "<-", true, "__dpback", C_DANGER);
}

void Screen::drawToast() {
    const uint16_t bg = _toastLevel == "error"   ? C_DANGER
                      : _toastLevel == "warn"    ? C_WARN
                      : _toastLevel == "success" ? C_OK
                                                 : C_PRIMARY;
    const int16_t h = 40;
    _spr.fillRoundRect(20, H - h - 10, W - 40, h, 8, bg);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextFont(4);
    _spr.setTextColor(TFT_WHITE, bg);
    _spr.drawString(_toastText, W / 2, H - h / 2 - 10);
    _spr.setTextDatum(TL_DATUM);
}

// ---------------------------------------------------------------------------
// Beruehrung
// ---------------------------------------------------------------------------
Action Screen::handleTouch(int16_t x, int16_t y) {
    Action action;
    for (const Hit &hit : _hits) {
        if (x < hit.x || x > hit.x + hit.w || y < hit.y || y > hit.y + hit.h) continue;

        const String &id = hit.id;

        // Lokal behandelte Elemente veraendern nur die Anzeige und erzeugen
        // keinen Netzverkehr. Erst "OK" schickt den Wert an den Server.
        if (id == "__up") { _scroll = max(0, _scroll - _pageSize); redraw(); return action; }
        if (id == "__down") { _scroll += _pageSize; redraw(); return action; }

        if (id == "__dpback") {
            if (_dateDigits.length()) _dateDigits.remove(_dateDigits.length() - 1);
            redraw();
            return action;
        }
        if (id.startsWith("__dp")) {
            if (_dateDigits.length() < 6) _dateDigits += id[4];
            if (_dateDigits.length() < 6) {
                redraw();
                return action;
            }
            // Sechste Ziffer: uebernehmen, ohne dass noch bestaetigt werden
            // muss - so war es im Vorgaengerprojekt und es spart bei jedem
            // Artikel einen Tastendruck.
            const String iso = "20" + _dateDigits.substring(4, 6) + "-" +
                               _dateDigits.substring(2, 4) + "-" +
                               _dateDigits.substring(0, 2);
            action.type  = ActionType::Input;
            action.value = iso;
            action.id    = "ok";
            return action;
        }

        if (id == "__d-") { _dateValue = shiftDate(_dateValue, -1, 0); redraw(); return action; }
        if (id == "__d+") { _dateValue = shiftDate(_dateValue, 1, 0); redraw(); return action; }
        if (id == "__m-") { _dateValue = shiftDate(_dateValue, 0, -1); redraw(); return action; }
        if (id == "__m+") { _dateValue = shiftDate(_dateValue, 0, 1); redraw(); return action; }

        if (id.startsWith("__n")) {
            const float step = _screen["meta"]["step"] | 1.0f;
            const float lo = _screen["meta"]["min"] | 0.0f;
            const float hi = _screen["meta"]["max"] | 9999.0f;
            if (id == "__n-")  _numberValue -= step;
            if (id == "__n+")  _numberValue += step;
            if (id == "__n--") _numberValue -= step * 10;
            if (id == "__n++") _numberValue += step * 10;
            _numberValue = constrain(_numberValue, lo, hi);
            redraw();
            return action;
        }

        if (_kind == "keyboard") {
            const int16_t maxLen = _screen["meta"]["max_len"] | 40;

            if (id == "__kbback") {
                if (_textValue.length()) _textValue.remove(_textValue.length() - 1);
                redraw();
                return action;
            }
            if (id == "__kbmode") {
                // klein -> GROSS -> Ziffern -> klein, siehe drawKeyboard().
                if (_kbNumeric)          { _kbNumeric = false; _kbShift = false; }
                else if (!_kbShift)      { _kbShift = true; }
                else                     { _kbShift = false; _kbNumeric = true; }
                redraw();
                return action;
            }
            if (id.startsWith("K") && id.length() == 2) {
                if (_textValue.length() < (size_t)maxLen) _textValue += id[1];
                redraw();
                return action;
            }
        }

        // "OK" auf einem Eingabebildschirm schickt zuerst den Wert, damit der
        // Server ihn kennt, bevor er den Tipp auswertet.
        if (id == "ok" && (_kind == "date" || _kind == "number" || _kind == "keyboard")) {
            action.type = ActionType::Input;
            action.value = _kind == "date" ? _dateValue
                          : _kind == "number" ? String(_numberValue, 2)
                          : _textValue;
            action.id = id;
            return action;
        }

        action.type = ActionType::Tap;
        action.id = id;
        return action;
    }
    return action;
}