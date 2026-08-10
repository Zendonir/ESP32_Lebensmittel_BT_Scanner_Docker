#pragma once

// Wird vom Pre-Build-Skript scripts/version.py gesetzt.
#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "dev"
#endif

// Exakt eines der beiden PlatformIO-Ziele muss die Boardvariante setzen.
#if !defined(BOARD_WAVESHARE_35) && !defined(BOARD_WAVESHARE_35B)
#error "Boardvariante fehlt: terminal-35 oder terminal-35b bauen"
#endif

// ---- Display (ST7796, SPI) -------------------------------------------------
// LCD_CS haengt nicht an einem direkten GPIO, LCD_RST am TCA9554-Expander.
// Beide bleiben deshalb auf -1.
#define LCD_MOSI 1
#define LCD_MISO 2
#define LCD_DC   3
#define LCD_CLK  5
#define LCD_BL   6

#if defined(BOARD_WAVESHARE_35B)
// 3.5B: AXS15231B ueber QSPI (Herstellerbeispiel 08_gfx_helloworld).
#define LCD_QSPI_CS  12
#define LCD_QSPI_CLK 5
#define LCD_QSPI_D0  1
#define LCD_QSPI_D1  2
#define LCD_QSPI_D2  3
#define LCD_QSPI_D3  4
#endif
#define LCD_RST -1

#define PANEL_WIDTH   320  // native Aufloesung (Hochformat)
#define PANEL_HEIGHT  480
#define UI_WIDTH      480  // Querformat, so wird gezeichnet
#define UI_HEIGHT     320

// ---- Touch (FT6336, I2C) ---------------------------------------------------
// Der Touch-Interrupt laeuft ueber EXIO2 des Expanders, wird also gepollt.
#define TOUCH_SDA  8
#define TOUCH_SCL  7
#if defined(BOARD_WAVESHARE_35B)
#define TOUCH_ADDR 0x3B
#else
#define TOUCH_ADDR 0x38
#endif
#define I2C_FREQ   400000

// ---- Drucker (ESC/POS ueber UART) ------------------------------------------
#define PRINTER_TX   44
#define PRINTER_RX   43
#define PRINTER_BAUD 9600

// ---- Signalton -------------------------------------------------------------
// Standardmaessig AUS (-1). Das Board hat keinen Piezo; die Toene wuerden ueber
// den ES8311-Codec laufen, was I2S und die Verstaerkerfreigabe am Expander
// braucht - dafuer ist die Quittung zu wenig wert.
//
// ACHTUNG bei der Wahl eines eigenen Pins: auf dem N16R8-Modul (octal PSRAM,
// board_build.arduino.memory_type = qio_opi) sind **GPIO 33-37 vom PSRAM
// belegt**. Wer dort etwas anschliesst, zerstoert die PSRAM-Anbindung; das
// aeussert sich nicht als Pin-Fehler, sondern als
//   assert failed: block_locate_free ... (block_size(block) >= *size)
// beim naechsten groesseren malloc - also als scheinbar zusammenhangloser
// Absturz. Die Bezeichnung "FREE_GPIO_1..3" fuer 35/36/37 im Vorgaengerprojekt
// war irrefuehrend; benutzt wurden sie dort nie.
//
// Wirklich frei sind auf diesem Board z.B. GPIO 17, 18 oder 21 - vor dem
// Anschluss trotzdem den Schaltplan pruefen.
#define BUZZER_PIN -1

// ---- Bedienung -------------------------------------------------------------
#define BOOT_BTN 0

// ============================================================================
// Netzwerk
// =====================================================…5599 tokens truncated…ger"  ? C_DANGER
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

        // "OK" auf einem Eingabebildschirm schickt zuerst den Wert, damit der
        // Server ihn kennt, bevor er den Tipp auswertet.
        if (id == "ok" && (_kind == "date" || _kind == "number")) {
            action.type = ActionType::Input;
            action.value = _kind == "date" ? _dateValue : String(_numberValue, 2);
            action.id = id;
            return action;
        }

        action.type = ActionType::Tap;
        action.id = id;
        return action;
    }
    return action;
}
