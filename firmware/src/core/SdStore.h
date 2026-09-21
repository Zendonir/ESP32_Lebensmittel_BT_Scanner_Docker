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

    // Fuers System-Panel: ob zuletzt eine Karte gefunden wurde. Fragt selbst
    // nicht nach - das tun saveSettings() und loadSettings().
    bool mounted() const { return _mounted; }

private:
    void ensureMounted();

    bool _mounted = false;

    // Naechster erlaubter Mount-Versuch. Ein gelungener Mount wird behalten;
    // ein misslungener darf spaeter wiederholt werden.
    //
    // Vorher gab es genau einen Versuch beim Start, danach nie wieder. Damit
    // lief ausgerechnet die Wiederherstellung ins Leere, fuer die es die
    // Karte gibt: wer vor einem Terminal steht, das den Server nicht findet,
    // steckt die Karte *jetzt* hinein - und Net::trackConnectionHealth fragt
    // nach 90 Sekunden Ausfall genau danach. Gesehen wurde sie nie.
    uint32_t _nextTryMs = 0;
    bool     _everTried = false;
};

extern SdStore sdStore;
