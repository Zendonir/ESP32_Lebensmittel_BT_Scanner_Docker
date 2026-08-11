# Architektur

## Leitgedanke

Ein Mikrocontroller ist gut in Echtzeit-Peripherie und schlecht in
Datenhaltung. Ein Container ist gut in Datenhaltung und kann keine UART
bedienen. Version 2 teilt genau entlang dieser Linie:

| | Terminal (ESP32-S3) | Server (Docker) |
|---|---|---|
| BLE-HID-Scanner | ✔ | |
| Touchscreen zeichnen | ✔ | |
| ESC/POS-UART | ✔ | |
| Was auf dem Bildschirm steht | | ✔ |
| Bestand, Vorlagen, Produkte | | ✔ |
| Etikettennummern und -layout | | ✔ |
| OpenFoodFacts, ntfy, Telegram, MQTT | | ✔ |
| Web-Interface | | ✔ |

Die Grenze ist ein WebSocket mit fünf Bildschirmarten
([docs/PROTOCOL.md](PROTOCOL.md)).

---

## Server

### Schichten

```
main.py            FastAPI, Web-Auslieferung, optionaler Passwortschutz
 ├── api/          REST für Browser und Skripte
 ├── device/       WebSocket, Verbindungsverwaltung, Ablauflogik
 └── services/     Fachlogik – von beiden Seiten genutzt
      └── models.py / db.py
```

**Die Fachlogik liegt genau einmal vor.** `services/inventory.add_items()` wird
sowohl vom Geräte-Workflow als auch von `POST /api/labels` aufgerufen –
Etikettenvergabe, Event-Log und Druckauftrag entstehen dadurch immer gleich. Im
ersten Projekt existierten dafür zwei Implementierungen
(`App::finishStorageWorkflow()` und die HTTP-Route), die regelmäßig
auseinanderliefen.

### Datenmodell

`inventory` ist eine Zustandstabelle mit einer Zeile je physischem Etikett.
`events` ist ein reines Protokoll. Das ist der wichtigste Unterschied zum
Vorgänger: dort musste der aktuelle Bestand über einen View aus dem Event-Strom
rekonstruiert werden, was bei jedem Sync-Aussetzer eine andere Antwort gab.

Weitere Tabellen: `products` (Stammsatz und OpenFoodFacts-Cache), `templates`,
`categories`, `locations`, `shopping_list`, `devices`, `print_jobs`, `settings`.

Voreinstellung ist SQLite mit WAL – für einen Haushalt mit ein paar tausend
Zeilen völlig ausreichend und ohne zweiten Container zu sichern. PostgreSQL ist
über `DATABASE_URL` einsetzbar, ohne dass sich am Code etwas ändert.

### Nebenläufigkeit

* Jede Gerätenachricht bekommt eine eigene Datenbanksitzung und ein eigenes
  `try/except`. Ein Fehler in einem Scan beendet weder die Verbindung noch den
  Dienst.
* Der Etikettenzähler läuft unter einem `asyncio.Lock` – zwei gleichzeitig
  gescannte Artikel bekommen nie dieselbe Nummer.
* Gleichzeitige OpenFoodFacts-Abfragen desselben Barcodes teilen sich einen
  Task.
* Pro Gerät sichert ein Lock den Sendeweg, damit sich Web-Request und
  Workflow-Task keine Frames verschränken.
* Ein einzelner Scheduler-Task prüft minütlich, ob MHD-Bericht, MQTT-Update
  oder Aufräumen fällig sind. Er fängt jede Ausnahme ab – ein stillschweigend
  gestorbener Scheduler fällt monatelang niemandem auf.

### Zustand je Gerät

`workflow.DeviceSession` hält den Bedienzustand im Arbeitsspeicher und wird beim
Trennen verworfen. Nach einem Reconnect steht das Gerät immer auf dem
Startbildschirm. Ein halb ausgefüllter Entwurf ist weniger wert als ein
eindeutiger Zustand – und alles, was zählt, steht ohnehin in der Datenbank.

---

## Terminal

### Ein Loop, keine eigenen Tasks

`main.cpp` macht der Reihe nach: Watchdog füttern, Netz bedienen, BLE bedienen,
Ton, Anzeige, Scan abholen, Berührung auswerten, höchstens ein Etikett drucken,
Telemetrie. Nebenläufig sind nur die Stacks von NimBLE und WLAN.

Version 1 hatte zusätzlich einen Touch-Task auf Core 0, einen Sync-Worker, einen
Produktabruf-Task und einen BLE-Connect-Task – jeder mit eigenen Schutzflags,
die bei einem fehlgeschlagenen `xTaskCreate` hängenblieben. Diese Tasks gibt es
nicht mehr, weil es die Arbeit nicht mehr gibt.

### Der Watchdog als letzte Instanz

30 Sekunden, `trigger_panic`, gefüttert als Erstes im Loop – danach darf jeder
Zweig früh zurückkehren. Die Reset-Ursache des vorherigen Laufs wird beim Start
protokolliert. Anders als vorher ist der Watchdog aber kein Betriebsmittel
mehr: es gibt keine blockierende Netzwerkarbeit, die ihn regelmäßig an den Rand
bringt.

### Speicher

* Der Zeichen-Sprite (480×320×16 bit ≈ 300 KB) liegt in PSRAM.
* **Task-Stacks bleiben im internen SRAM.** PSRAM hängt am selben
  Cache-Controller wie der Flash; während eines NVS-Schreibvorgangs ist der
  Cache abgeschaltet und ein Stack in PSRAM unerreichbar
  (`assert esp_task_stack_is_sane_cache_disabled()`).
* Im NVS stehen nur WLAN-Zugang, Server, Token und Anzeigename. Kein
  Dateisystem, kein JSON, kein Produktcache. Dieselben Werte werden zusätzlich
  still auf eine eingelegte SD-Karte gespiegelt (`core/SdStore`) - beim
  allerersten Start (leeres NVS) liest das Gerät sie von dort, statt das
  Einrichtungsportal zu verlangen. Kein Ersatz für das NVS, nur eine
  Vorbelegung fürs Tauschen defekter Geräte.

### Partitionen

Ohne Web-Dateien und ohne Nutzdaten auf dem Gerät entfallen die
`spiffs`- und `userdata`-Partitionen. Der Platz geht an die App-Slots
(2×7,5 MB), damit ein OTA nie an der Größe scheitert – und es gibt nur noch
**ein** Abbild zu flashen statt zweier.

---

## Ausfallverhalten

| Fall | Verhalten |
|---|---|
| Server weg | Terminal zeigt „Kein Server", verbindet alle 3 s neu; ein Scan wartet gepuffert |
| WLAN weg | Banner „Kein WLAN", expliziter Neuversuch alle 20 s |
| Terminal aus | Server läuft weiter, Web-Interface uneingeschränkt nutzbar |
| Drucker aus | Auftrag bleibt `queued` und wird nach dem Reconnect erneut gesendet |
| BLE-Scanner leer | Akkuwarnung einmalig bei <10 %, Anzeige im Web-Interface |
| Fehler in der Ablauflogik | HTTP-500 im Container, Terminal bekommt „Serverfehler" und bedient sich weiter |
| Container-Neustart | Terminals verbinden selbsttätig neu, offene Druckaufträge laufen weiter |
