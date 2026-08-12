#pragma once

#include <ArduinoJson.h>
#include <Arduino.h>
#include <vector>

#include "DisplayCanvas.h"

// Allgemeiner Renderer fuer die Bildschirmbeschreibungen des Servers.
//
// Die Firmware kennt keine Bildschirme mehr, nur noch Darstellungsarten:
// Kacheln, Liste, Datum, Zahl, Meldung. Was darauf steht, entscheidet der
// Server. Damit ist ein Umbau der Bedienung ein Server-Deploy - im
// Vorgaengerprojekt war dafuer jedes Mal ein OTA-Flash noetig.

enum class ActionType { None, Tap, Input };

struct Action {
    ActionType type = ActionType::None;
    String     id;       // bei Tap: Element- oder Knopf-Kennung
    String     value;    // bei Input: der eingestellte Wert
};

struct Hit {
    int16_t x, y, w, h;
    String  id;
    bool    isInput = false;   // liefert value statt id
};

class Screen {
public:
    void begin();

    // Neue Bildschirmbeschreibung uebernehmen und zeichnen.
    void apply(JsonDocument &doc);

    // Ueberlagerungen, die den Bildschirminhalt nicht ersetzen.
    void toast(const String &text, const String &level);
    void setBanner(const String &text);   // Dauerhinweis, z.B. "kein Server"
    void showBoot(const String &line1, const String &line2 = "");
    void setBrightness(uint8_t percent);

    // Muss regelmaessig laufen: blendet Meldungen aus, zeichnet die Uhr.
    void loop();

    // Beruehrung auswerten. Liefert die auszuloesende Aktion.
    Action handleTouch(int16_t x, int16_t y);

    // Kann der aktuelle Bildschirm ueberhaupt scrollen? Steuert bei Touch, ob
    // eine anhaltende Vertikalbewegung als Scroll-Drag statt als Swipe zaehlt.
    bool scrollable() const { return _kind == "list" && _listTotal > _pageSize; }

    // Um die uebergebenen Pixel weiterscrollen (Finger nach oben = Liste
    // laeuft weiter). Rechnet intern in ganze Zeilen um und zeichnet nur bei
    // tatsaechlicher Verschiebung neu.
    void scrollBy(int16_t deltaYPx);

    // Ganze Zeilen weiterspringen - fuer die Scrolltaster.
    void scrollByRows(int rows);

    int screenId() const { return _screenId; }
    bool ready() const { return _ready; }

private:
    void redraw();
    void drawStatusBar();
    void drawTitle();
    int16_t bodyTop() const;
    void drawTiles();
    void drawList();
    void drawDate();
    void drawNumber();
    void drawMessage();
    void drawKeyboard();
    void drawHome();
    void drawCards();
    void drawDatePad();

    // Welche Ziffern an der aktuellen Stelle ueberhaupt moeglich sind.
    // Bit N gesetzt = Ziffer N erlaubt.
    uint16_t datePadValidDigits() const;
    void drawToast();
    void commit();

    // Zeichen, das eine Taste bei aktuellem Umschalt-/Zifferzustand einfuegt.
    char keyboardCharAt(uint8_t row, uint8_t col) const;

    // Setzt die passende Schriftgroesse und liefert den ggf. gekuerzten Text.
    String fitText(const String &text, int16_t maxWidth, uint8_t font);

    void addHit(int16_t x, int16_t y, int16_t w, int16_t h, const String &id, bool isInput = false);
    void tile(int16_t x, int16_t y, int16_t w, int16_t h, const String &label,
              const String &sub, uint16_t color, const String &id, uint8_t font = 4);
    uint8_t gridFont(JsonArray items, int16_t maxTextW);
    void button(int16_t x, int16_t y, int16_t w, int16_t h, const String &label,
                uint16_t bg, const String &id);

    static uint16_t parseColor(const char *hex, uint16_t fallback);
    String  shiftDate(const String &iso, int days, int months) const;

    DisplayCanvas _spr;

    JsonDocument _screen;      // aktuelle Beschreibung
    std::vector<Hit> _hits;

    int      _screenId = 0;
    String   _kind     = "message";
    bool     _ready    = false;

    // Zustand der Eingabeelemente
    String   _dateValue;
    String   _dateDigits;      // bis zu 6 getippte Ziffern (TTMMJJ)
    float    _numberValue = 1;
    String   _textValue;
    bool     _kbShift   = false;   // Grossbuchstaben
    bool     _kbNumeric = false;   // Ziffern/Symbole statt Buchstaben

    // Liste
    int      _scroll        = 0;
    int      _pageSize      = 4;
    int      _listTotal     = 0;
    int16_t  _scrollAccumPx = 0;   // Restweg unterhalb einer ganzen Zeile

    String   _toastText;
    String   _toastLevel;
    uint32_t _toastUntil = 0;
    String   _banner;
    int16_t  _bodyY = 66;   // Oberkante des Inhalts, siehe bodyTop()
    uint8_t  _brightness = 80;
};

extern Screen screen;