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
    { "t": "text", "v": "Kirschmarmelade", "align": 1, "bold": true, "large": true, "h": 48 },
    { "t": "sep", "h": 24 },
    { "t": "row", "k": "MHD", "v": "24.12.2026", "underline": true, "h": 24 },
    { "t": "row", "k": "Menge", "v": "500 g", "h": 24 },
    { "t": "code128", "v": "LEB000042", "height": 30, "h": 30 },
    { "t": "feed", "dots": 42 }
  ]
}
```

Blockarten: `text` (`align` 0/1/2, `bold`, `large`), `row` (zweispaltig,
`underline`), `sep`, `qr`, `code128`, `feed`, `form`, `back`.

`form` schließt ein Etikett mit `GS FF` ab, statt den ausgerechneten Rest
vorzuschieben: der Drucker sucht die Trennlücke mit seinem eigenen Sensor. Bei
gestanzten Etiketten ist das der robustere Abschluss, weil die Registrierung
dann bei jedem Etikett neu stimmt und ein Rest von ein paar Punkten sich nicht
über die Rolle aufsummieren kann. Drucker ohne Lücken- oder Markensensor
kennen den Befehl nicht, deshalb schickt ihn der Server nur auf ausdrückliche
Einstellung (`printer.label_end`). Der Block trägt weiterhin `dots`, damit
`total_dots()` und die Vorschau eine volle Teilung sehen.

### `h` – die Höhe ist verbindlich

Jeder Block außer `feed` und `back` trägt in `h` die Höhe in Punkten, mit der
der Server gerechnet hat. Die Firmware stellt den Zeilenabstand vor jeder
Zeile ausdrücklich darauf ein (`ESC 3 n`), statt den Standardabstand des
Druckers zu nehmen.

Das ist keine Feinheit, sondern die Bedingung dafür, dass das Konzept
überhaupt trägt. Vorher rechnete der Server mit 24 Punkten je Zeile
(`LINE_DOTS`, exakt die Schrifthöhe von Font A) und sagte es dem Drucker nie;
`ESC @` stellt dort den Standard ein – laut Spezifikation 1/6 Zoll, bei
203 dpi also rund 34 Punkte. Beim klassischen Zuschnitt auf 50×30 mm ergab das
314 statt 240 Punkten: **9,2 mm Überlauf je Etikett**, nach drei Stück ein
ganzes Etikett Versatz. Totbereich, Rückzug und `SAFETY_DOTS` waren allesamt
Gegenmittel gegen ein Symptom, dessen Ursache diese Rechnung selbst war.

Zwei Blöcke rechnen nicht über den Zeilenabstand:

* `code128` – der Abstand wird auf 0 gesetzt, `GS k` schiebt genau seine
  Strichcodehöhe vor. `h` ist deshalb gleich `height`.
* `qr` – `h` ist der **reservierte** Platz, ein Vielfaches von 8 (`ESC *`
  druckt immer acht Punktzeilen auf einmal). Die Firmware leitet daraus ihre
  Modulgröße ab, zeichnet den Code quadratisch mit zwei Modulen Ruhezone und
  füllt auf `h` auf. Vorher steckten zwei Modulzeilen in einem Durchgang –
  senkrecht also fest 4 Punkte je Modul, waagerecht aber `scale`: quadratisch
  war der Code nur bei Skalierung 4, bei „mittel" 3:4 gestaucht, bei „klein"
  2:4. Eine Ruhezone wurde gar nicht gedruckt.

Fehlt `h`, fällt die Firmware auf die Schrifthöhe zurück – ein älterer Server
bleibt damit bedienbar.

Der Renderer meldet im Auftrag außerdem `overflow`: die Punkte, die über die
bedruckbare Höhe hinausgehen. 0 heißt, das Etikett endet genau an der
Perforation. Alles andere gehört in die Oberfläche, statt still aufs
Folgeetikett zu laufen.

Ablauf und Fehlerbehandlung:

1. Der Auftrag steht als Zeile in `print_jobs` (`queued`).
2. Beim Senden wird er `sent`, `attempts` steigt.
3. `print_result` mit `ok: true` setzt ihn auf `done`, sonst zurück auf
   `queued`. Nach fünf Versuchen `failed`. Ein erfolgreicher Auftrag zieht
   sofort den nächsten nach, bis nichts mehr offen ist.
4. Es liegen nie mehr als `PRINT_BATCH` (4) Aufträge gleichzeitig beim Gerät.
   Die Warteschlange der Firmware fasst acht; wer sie überfüllt, sammelt für
   jeden abgelehnten Auftrag einen Versuch ein und schiebt ihn ohne Not
   Richtung `failed`.
5. Ein `sent` wird **nicht** erneut gesendet – er liegt im Drucker, und ein
   zweites Senden heißt ein zweites Etikett. Ausnahme ist der Reconnect: dort
   ist die Warteschlange der Firmware leer, und erneutes Senden ist die
   einzige Rettung für das Etikett.
6. Bleibt zu einem `sent` länger als `PRINT_STALE_SECONDS` (60) die Rückmeldung
   aus, gilt er als verloren und geht zurück in die Schlange. Ein Etikett
   braucht unter zwei Sekunden; länger heißt, das Gerät war zwischendurch weg,
   ohne dass der Server es gemerkt hat.
7. Ist die Warteschlange der Firmware trotzdem voll, meldet sie sofort
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
