#include "Screen.h"

#include "config.h"

Screen screen;

// Layoutraster fuer 480x320 im Querformat.
static constexpr int16_t W = UI_WIDTH;
static constexpr int16_t H = UI_HEIGHT;
static constexpr int16_t STATUS_H = 26;
static constexpr int16_t TITLE_H = 40;
static constexpr int16_t FOOTER_H = 46;
static constexpr int16_t BODY_Y = STATUS_H + TITLE_H;
static constexpr int16_t BODY_H = H - BODY_Y - FOOTER_H;

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

    drawStatusBar();
    drawTitle();

    if (_kind == "tiles") drawTiles();
    else if (_kind == "list") drawList();
    else if (_kind == "date") drawDate();
    else if (_kind == "number") drawNumber();
    else if (_kind == "keyboard") drawKeyboard();
    else if (_kind == "home") drawHome();
    else if (_kind == "cards") drawCards();
    else drawMessage();

    drawFooter();
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
    const bool removeMode = status["mode"] == "remove";
    const char *location = status["location"] | "";

    _spr.setTextColor(removeMode ? C_DANGER : C_OK, C_SURFACE);
    _spr.drawString(removeMode ? "AUSLAGERN" : "EINLAGERN", 8, 5);
    addHit(0, 0, 108, STATUS_H, "mode");

    // Lagerort als antippbare Pille rechts - oeffnet die Ortsauswahl, wie im
    // Vorgaengerprojekt das Badge oben rechts.
    const int16_t pillH = STATUS_H - 6;
    const int16_t pillW = 150;
    const int16_t pillX = W - 84 - pillW;
    const int16_t pillY = 3;
    _spr.fillRoundRect(pillX, pillY, pillW, pillH, pillH / 2, C_PRIMARY);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextColor(TFT_WHITE, C_PRIMARY);
    _spr.drawString(location[0] ? location : "kein Ort", pillX + pillW / 2, pillY + pillH / 2 + 1);
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
    _spr.setTextFont(4);
    _spr.setTextColor(C_TEXT, C_BG);
    _spr.drawString(String(_screen["title"] | ""), 10, y + 2);

    const char *subtitle = _screen["subtitle"] | "";
    if (subtitle[0]) {
        _spr.setTextFont(2);
        _spr.setTextColor(C_MUTED, C_BG);
        _spr.drawString(subtitle, 10, y + 26);
    }
}

void Screen::tile(int16_t x, int16_t y, int16_t w, int16_t h, const String &label,
                  const String &sub, uint16_t color, const String &id) {
    _spr.fillRoundRect(x, y, w, h, 8, color);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextFont(4);
    _spr.setTextColor(TFT_WHITE, color);
    _spr.drawString(label, x + w / 2, y + h / 2 - (sub.isEmpty() ? 0 : 10));
    if (!sub.isEmpty()) {
        _spr.setTextFont(2);
        _spr.drawString(sub, x + w / 2, y + h / 2 + 14);
    }
    _spr.setTextDatum(TL_DATUM);
    addHit(x, y, w, h, id);
}

void Screen::button(int16_t x, int16_t y, int16_t w, int16_t h, const String &label,
                    uint16_t bg, const String &id) {
    _spr.fillRoundRect(x, y, w, h, 6, bg);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextFont(2);
    _spr.setTextColor(TFT_WHITE, bg);
    _spr.drawString(label, x + w / 2, y + h / 2);
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

    int index = 0;
    for (JsonObject item : items) {
        const int row = index / cols, col = index % cols;
        tile(gap + col * (w + gap), BODY_Y + gap + row * (h + gap), w, h,
             String(item["label"] | ""), String(item["sub"] | ""),
             parseColor(item["color"] | "", C_PRIMARY), String(item["id"] | ""));
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

void Screen::drawList() {
    JsonArray items = _screen["items"].as<JsonArray>();
    const int total = items.size();
    const int16_t rowH = 44;
    _pageSize = BODY_H / rowH;
    _listTotal = total;

    if (_scroll > max(0, total - _pageSize)) _scroll = max(0, total - _pageSize);

    for (int i = 0; i < _pageSize && (_scroll + i) < total; i++) {
        JsonObject item = items[_scroll + i];
        const int16_t y = BODY_Y + i * rowH;

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

        _spr.setTextFont(4);
        _spr.setTextColor(C_TEXT, C_SURFACE);
        _spr.drawString(String(item["label"] | ""), 22, y + 5);

        const char *sub = item["sub"] | "";
        if (sub[0]) {
            _spr.setTextFont(2);
            _spr.setTextColor(C_MUTED, C_SURFACE);
            _spr.drawString(sub, 22, y + 24);
        }
        addHit(8, y, W - 16, rowH - 4, String(item["id"] | ""));
    }

    // Bildlaufleiste als zwei grosse Flaechen - fuer Finger, nicht fuer Maeuse.
    if (total > _pageSize) {
        button(W - 32, BODY_Y, 26, BODY_H / 2 - 3, "^", C_SURFACE, "__up");
        button(W - 32, BODY_Y + BODY_H / 2 + 3, 26, BODY_H / 2 - 3, "v", C_SURFACE, "__down");
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
    button(14, y, 78, 40, "-" + String(step * 10, step < 1 ? 1 : 0), C_SURFACE, "__n--");
    button(100, y, 78, 40, "-" + String(step, step < 1 ? 1 : 0), C_SURFACE, "__n-");
    button(W - 178, y, 78, 40, "+" + String(step, step < 1 ? 1 : 0), C_SURFACE, "__n+");
    button(W - 92, y, 78, 40, "+" + String(step * 10, step < 1 ? 1 : 0), C_SURFACE, "__n++");
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
    _spr.fillRoundRect(8, BODY_Y, W - 16, 26, 5, C_SURFACE);
    _spr.setTextFont(4);
    _spr.setTextColor(_textValue.isEmpty() ? C_MUTED : C_TEXT, C_SURFACE);
    _spr.drawString(_textValue.isEmpty() ? "..." : (_textValue + "_"), 14, BODY_Y + 2);

    const int16_t gap = 3;
    const int16_t rowH = 36;

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

    int16_t y = BODY_Y + 32;
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
    int index = 0;
    for (JsonObject item : items) {
        const int row = index / cols, col = index % cols;
        tile(gap + col * (tw + gap), y + gap + row * (th + gap), tw, th,
             String(item["label"] | ""), String(item["sub"] | ""),
             parseColor(item["color"] | "", C_PRIMARY), String(item["id"] | ""));
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
        _spr.drawString(String(card["title"] | ""), x + 10, y + 6);

        // Die grosse Statuszeile ist optional - Karten ohne Schlagzeile (z.B.
        // "System") gewinnen den Platz fuer eine Zeile mehr.
        int16_t ly = y + 20;
        const char *statusText = card["status"] | "";
        if (statusText[0]) {
            _spr.setTextFont(4);
            _spr.setTextColor(parseColor(card["status_color"] | "", C_TEXT), C_BG);
            _spr.drawString(statusText, x + 10, ly);
            ly += 26;
        }

        JsonObject btn = card["button"];
        const int16_t bottomLimit = btn.isNull() ? (y + h - 6) : (y + h - 28);

        _spr.setTextFont(2);
        _spr.setTextColor(C_MUTED, C_BG);
        JsonArray lines = card["lines"].as<JsonArray>();
        for (JsonVariant line : lines) {
            if (ly + 13 > bottomLimit) break;
            _spr.drawString(String(line.as<const char *>()), x + 10, ly);
            ly += 14;
        }

        if (!btn.isNull()) {
            button(x + 8, y + h - 26, w - 16, 20, String(btn["label"] | ""),
                   parseColor(btn["color"] | "", C_SURFACE), String(btn["id"] | ""));
        }
        index++;
    }
}

void Screen::drawFooter() {
    JsonArray buttons = _screen["buttons"].as<JsonArray>();
    if (buttons.isNull() || buttons.size() == 0) return;

    const int count = buttons.size();
    const int16_t y = H - FOOTER_H + 5;
    const int16_t w = (W - 10 * (count + 1)) / count;
    int index = 0;
    for (JsonObject item : buttons) {
        const String style = String(item["style"] | "ghost");
        const uint16_t bg = style == "primary" ? C_PRIMARY
                          : style == "danger"  ? C_DANGER
                                               : C_SURFACE;
        button(10 + index * (w + 10), y, w, FOOTER_H - 12, String(item["label"] | ""),
               bg, String(item["id"] | ""));
        index++;
    }
}

void Screen::drawToast() {
    const uint16_t bg = _toastLevel == "error"   ? C_DANGER
                      : _toastLevel == "warn"    ? C_WARN
                      : _toastLevel == "success" ? C_OK
                                                 : C_PRIMARY;
    const int16_t h = 40;
    _spr.fillRoundRect(20, H - FOOTER_H - h - 6, W - 40, h, 8, bg);
    _spr.setTextDatum(MC_DATUM);
    _spr.setTextFont(4);
    _spr.setTextColor(TFT_WHITE, bg);
    _spr.drawString(_toastText, W / 2, H - FOOTER_H - h / 2 - 6);
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