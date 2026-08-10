# Geräteprotokoll

Terminal und Server sprechen über **einen** WebSocket. Alles ist JSON, eine
Nachricht pro Frame, Feld `t` nennt die Art.

```
ws://<server>:8080/ws/device?id=<geräte-id>&token=<DEVICE_TOKEN>
```

`id` ist die MAC-Adresse ohne Trenner. Stimmt das Token nicht, schließt der
Server mit Code `4401`. Verbindet sich dieselbe `id` erneut, wird die alte
Verbindung mit Code `4000` ersetzt – ein halboffener Socket nach einem
Geräte-Reset blockiert damit nichts.

Der Server sendet alle 20 s ein `ping`; bleibt 90 s lang jede Nachricht aus,
gilt die Verbindung als tot. Umgekehrt hält die Firmware den WebSocket-Heartbeat
der Bibliothek aktiv und verbindet nach 3 s neu.

---

## Terminal → Server

| `t` | Felder | Bedeutung |
|---|---|---|
| `hello` | `device_id`, `name`, `firmware`, `ip`, `has_printer` | Anmeldung; aktualisiert den Gerätedatensatz |
| `scan` | `code`, `source` (`ble`/`manual`) | Ein Barcode wurde gelesen |
| `tap` | `screen`, `item` | Kachel, Listenzeile oder Fußleistenknopf berührt |
| `input` | `screen`, `value` | Wert eines Datums- oder Zahlenbildschirms |
| `back` | `screen` | Einen Schritt zurück |
| `telemetry` | `heap`, `min_heap`, `psram`, `rssi`, `uptime`, `scanner:{connected,battery,name}` | Alle 30 s |
| `print_result` | `job`, `ok`, `error` | Rückmeldung zu einem Druckauftrag |
| `pong` | – | Antwort auf `ping` |

Reihenfolge bei Eingabebildschirmen: erst `input`, dann `tap` mit `item: "ok"`.
Sonst speichert der Server den vorherigen Wert.

## Server → Terminal

| `t` | Felder | Bedeutung |
|---|---|---|
| `screen` | siehe unten | Vollständige Bildschirmbeschreibung |
| `toast` | `text`, `level` (`info`/`success`/`warn`/`error`) | Kurzmeldung über dem Inhalt |
| `beep` | `pattern` (`ok`/`error`/`warn`/`scan`/`print`) | Signalton |
| `print` | `job`, `chars`, `blocks` | Druckauftrag |
| `config` | `brightness`, `beep`, `idle_seconds` | Anzeigeeinstellungen |
| `ping` | – | Lebenszeichen |
| `reboot` | – | Neustart |

---

## Bildschirmbeschreibung

```jsonc
{
  "t": "screen",
  "id": 42,                     // fortlaufend, geht in tap/input zurück
  "kind": "tiles",              // tiles | list | date | number | message
  "title": "12 Artikel",
  "subtitle": "Barcode scannen zum Einlagern",
  "lines": ["freier Text", "…"],
  "items": [                    // Kacheln bzw. Listenzeilen
    { "id": "cat:Getraenke", "label": "Getränke", "sub": "12", "color": "#1e88e5" }
  ],
  "buttons": [                  // feste Fußleiste
    { "id": "back", "label": "Zurueck", "style": "ghost" },
    { "id": "ok",   "label": "OK",      "style": "primary" }
  ],
  "value": "2026-12-24",        // Startwert für date/number
  "meta": {                     // Grenzen und Schnellwahl
    "min": 1, "max": 20, "step": 1, "unit": "Stk",
    "presets": [{ "id": "p:7", "label": "+1 W" }]
  },
  "status": {                   // Kopfzeile
    "location": "Kuehlschrank", "mode": "store",
    "total": 12, "expiring": 3, "battery": 87, "scanner": true
  }
}
```

Die Firmware kennt nur diese fünf Darstellungsarten. Welche Bildschirme es
gibt, welche Kacheln darauf liegen und was ein Tipp bewirkt, entscheidet
ausschließlich `app/device/workflow.py`. **Eine neue Bedienführung ist damit ein
Server-Deploy, kein OTA-Flash.**

Kennungen mit `__`-Präfix (`__d+`, `__n-`, `__up`, …) behandelt die Firmware
selbst: sie verändern nur den angezeigten Wert und erzeugen keinen Netzverkehr.
Erst „OK" schickt das Ergebnis.

---

## Druckauftrag

Der Server rendert das Etikett vollständig; die Firmware setzt Blöcke in
ESC/POS-Bytes um und kennt kein Layout.

```jsonc
{
  "t": "print", "job": 17, "chars": 32,
  "blocks": [
    { "t": "text", "v": "Haushalt Mueller", "align": 1 },
    { "t": "text", "v": "Kirschmarmelade", "align": 1, "bold": true, "large": true },
    { "t": "sep" },
    { "t": "row", "k": "MHD", "v": "24.12.2026", "underline": true },
    { "t": "row", "k": "Menge", "v": "500 g" },
    { "t": "code128", "v": "LEB000042" },
    { "t": "qr", "v": "LEB000042" },
    { "t": "feed", "dots": 86 }
  ]
}
```

Blockarten: `text` (`align` 0/1/2, `bold`, `large`), `row` (zweispaltig,
`underline`), `sep`, `qr`, `code128`, `feed`.

Ablauf und Fehlerbehandlung:

1. Der Auftrag steht als Zeile in `print_jobs` (`queued`).
2. Beim Senden wird er `sent`, `attempts` steigt.
3. `print_result` mit `ok: true` setzt ihn auf `done`, sonst zurück auf
   `queued`. Nach fünf Versuchen `failed`.
4. Nach jedem Reconnect sendet der Server offene Aufträge erneut – ein Etikett,
   das während eines Neustarts entstanden ist, geht nicht verloren.
5. Ist die Warteschlange der Firmware voll (8 Aufträge), meldet sie sofort
   `ok: false`, statt den Auftrag stillschweigend zu verwerfen.

Gedruckt wird ausschließlich im Hauptloop, höchstens ein Etikett pro Durchlauf.
Ein Etikett belegt die UART mehrere hundert Millisekunden – im ersten Projekt
stand währenddessen der gesamte Webserver des Geräts still.

---

## Browser-Verbindung

`ws://<server>:8080/ws/ui` ist einseitig: der Server meldet nur, *was* sich
geändert hat (`inventory`, `catalog`, `devices`, `shopping`, `settings`), und die
Seite lädt die betroffene Ansicht neu. Es werden bewusst keine Daten über diesen
Kanal geschickt – so kann im Browser kein halbaktueller Zustand hängenbleiben.
