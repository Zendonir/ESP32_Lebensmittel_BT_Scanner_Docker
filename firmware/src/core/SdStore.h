#pragma once

#include <Arduino.h>

// Sichert die auf dem Geraet eingegebenen Zugangsdaten (WLAN, Server, Token)
// zusaetzlich auf der SD-Karte.
//
// Das ist kein Ersatz fuer das NVS, sondern eine Vorbelegung: steht beim
// allerersten Start - oder nach einem Werksreset - noch nichts im NVS, aber
// eine gueltige Datei auf der Karte, wird sie automatisch uebernommen, ohne
// durch das WLAN-Einrichtungsportal zu muessen. Praktisch beim Tausch eines
// defekten Geraets: Karte umstecken, fertig.
//
// Beide Methoden sind bewusst fehlertolerant - eine fehlende oder volle Karte
// darf niemals den Aufrufer scheitern lassen, nur der normale Betrieb ohne
// SD-Karte muss weiterhin funktionieren.
class SdStore {
public:
    void saveSettings();   // still; Fehler landen nur im Log
    bool loadSettings();   // true = gueltige Datei gefunden und uebernommen

    // Einmaligen Mount-Versuch erzwingen, auch wenn loadSettings() nie
    // aufgerufen wird (z.B. weil im NVS schon Zugangsdaten stehen). Nur so
    // weiss mounted() beim System-Panel Bescheid.
    void probe() { ensureMounted(); }

    // Fuers System-Panel: ob beim Start eine Karte gefunden wurde. Kein neuer
    // Mount-Versuch - ein Wechsel im laufenden Betrieb wird nicht erkannt,
    // wie im Vorgaengerprojekt auch.
    bool mounted() const { return _mounted; }

private:
    void ensureMounted();

    bool _mounted = false;
    bool _mountTried = false;
};

extern SdStore sdStore;
