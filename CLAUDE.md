# Projektdokumentation für Claude

Lebensmittel-Inventarsystem, Version 2. Nachfolger von
`ESP32_Lebensmittel_BT_Scanner`, komplett neu aufgebaut.

**Die eine Regel, aus der sich alles andere ergibt:** der Docker-Container hält
Daten und Ablauf, der ESP32 ist ein Terminal. Kommt eine neue Funktion dazu,
gehört sie zuerst in den Server. Die Firmware wird nur angefasst, wenn neue
*Hardware* angesprochen werden muss.

---

## Aufbau

```
server/app/
  main.py        FastAPI, Web-Auslieferung, optionaler Passwortschutz
  config.py      Einstellungen aus der Umgebung (kein Config-File!)
  models.py      ORM: inventory, events, products, templates, categories,
                 locations, shopping_list, devices, print_jobs, settings
  api/           inventory · catalog · labels · system · firmware
  device/        protocol · hub · workflow · routes
  services/      inventory · labels · categories · openfoodfacts · notify ·
                 scheduler · dates · settings_store · seed · importer · firmware
server/web/      index.html · mobile.html · js/ · css/ (kein Build-Schritt)
                 js/vendor/  ZXing – Barcodes aus der Handykamera, siehe
                             das README dort
firmware/scripts/ gen_gfx_font.py  · preview_screens.py (Bildschirme ansehen,
                  ohne zu flashen – Zweitschrift von Screen.cpp!)
firmware/src/    main.cpp · core/ · scanner/ · ui/ · printer/
```

## Server

```bash
python3 -m venv .venv && .venv/bin/pip install -r server/requirements-dev.txt
cd server && DEVICE_TOKEN=dev ../.venv/bin/uvicorn app.main:app --reload
cd server && ../.venv/bin/python -m pytest tests -q
../.venv/bin/ruff check server/app server/tests
```

### Regeln

* **Bestandsänderungen laufen ausschließlich über `services/inventory.py`.**
  Nie direkt `session.add(InventoryItem(...))` in einer Route – sonst fehlen
  Etikettennummer, Event-Log oder Druckauftrag. Genau diese Duplizierung war im
  ersten Projekt (`App::finishStorageWorkflow()` vs. HTTP-Route) eine stete
  Fehlerquelle.
* **Datumsangaben sind intern immer ISO** (`YYYY-MM-DD`). Umgewandelt wird nur
  in `services/dates.py`; die Schemas rufen das über einen Validator auf.
* **`inventory` ist Zustand, `events` ist Protokoll.** Der Bestand wird nie aus
  Events rekonstruiert.
* Nach einer schreibenden Operation `hub.notify_ui("<bereich>")` aufrufen, damit
  offene Browser sich aktualisieren.
* Sicherheitsrelevantes (Token, Passwort, Datenbank) kommt aus der Umgebung;
  nur Bedienbares (Helligkeit, Papierbreite) gehört in `settings_store`.
* Neue Einstellungsschlüssel brauchen einen Eintrag in
  `settings_store.DEFAULTS` – `PATCH /api/settings/{key}` lehnt Unbekanntes ab.

### Geräte-Workflow

`device/workflow.py` ist der Nachfolger von `App.cpp`. Ein Bildschirm besteht
aus Name (Konstante oben in der Datei), einer `_screen_*`-Funktion und einer
`_tap_*`-Funktion; `render()` und `on_tap()` verteilen darauf.

Der Zustand (`DeviceSession`) liegt im Arbeitsspeicher und wird beim Trennen
verworfen – nach einem Reconnect steht das Gerät auf dem Startbildschirm.

Die Firmware kennt diese Darstellungsarten: `tiles`, `list`, `date`, `number`,
`message`, `keyboard`, `home`, `cards`, `datepad`. Wer eine weitere braucht,
muss die Firmware anfassen – vorher prüfen, ob sich das Ziel mit den
vorhandenen erreichen lässt.

## Firmware

```bash
cd firmware && pio run                 # bauen
cd firmware && pio run --target upload # flashen
```

### Regeln

* **Keine Fachlogik.** Kein Etikettenlayout, keine Produktabfrage, keine
  Bestandsentscheidung. Kommt die Versuchung auf, gehört die Sache in den
  Server.
* **Ein Loop, keine eigenen Tasks.** Version 1 hatte vier davon, jeder mit
  Schutzflags, die bei einem fehlgeschlagenen `xTaskCreate` hängenblieben.
* **Nichts blockieren.** Kein `delay()` außer den 5 ms am Loop-Ende, keine
  synchronen HTTP-Aufrufe, höchstens ein Etikett pro Durchlauf.
* `esp_task_wdt_reset()` steht als **erste** Zeile im Loop – danach darf jeder
  Zweig früh zurückkehren.
* **Task-Stacks niemals in PSRAM.** PSRAM hängt am selben Cache-Controller wie
  der Flash; bei abgeschaltetem Cache (NVS-Schreibvorgang) löst das
  `assert esp_task_stack_is_sane_cache_disabled()` aus.
* `-DUSE_HSPI_PORT=1` ist Pflicht, keine Optimierung. Ohne das dereferenziert
  TFT_eSPI auf dem ESP32-S3 eine ungültige SPI-Registerbasis → Panic mit
  `EXCVADDR=0x10`.
* Die BLE-Kopplung ist eine Zustandsmaschine (`BLEScanner::State`) und muss
  es bleiben: **kein blockierender Aufruf im Loop.** `getResults()` und ein
  synchrones `connect()` haben das Gerät sekundenlang stillstehen lassen –
  inklusive Touch. Es gibt für beides eine asynchrone Variante.
* Der Zustand wird auf **jedem** Ausstiegspfad zurückgesetzt, nicht nur in
  `onDisconnect` – sonst bleibt der Scanner für immer im Zustand „verbinde…".
* Ein bekannter Scanner wird **nicht gesucht**, sondern über einen gerichteten
  Verbindungswunsch ohne Zeitgrenze erwartet (`AutoConnect`). Den hält der
  Controller offen; die Verbindung steht in dem Moment, in dem der Scanner
  eingeschaltet wird. Suchläufe kosten Funkzeit, die neben WLAN im selben Band
  fehlt.
* Es bleibt immer **genau eine** Kopplung gespeichert. Sonst zeigt
  `getBondedAddress(0)` nach einem Scannerwechsel womöglich auf das alte Gerät.
* Töne laufen über den **ES8311-Codec**, nicht über einen Piezo – das Board
  hat keinen. Dazu gehören die Spannungsschienen des AXP2101, die I2S-Takte
  und die Endstufe am Expander (EXIO7). Auf der 3.5B ist die Belegung
  ungeklärt (GPIO12 liegt dort am Display), deshalb `AUDIO_ENABLED 0`.
* Umlaute auf dem Drucker gehen über `toCp1252()`; der Drucker kennt kein UTF-8.

## Bekannte Fallstricke

| Problem | Lösung |
|---|---|
| SPI-Panic `EXCVADDR=0x10` | `-DUSE_HSPI_PORT=1` |
| Scanner koppelt nicht mehr | Zustand auf jedem Ausstiegspfad zurücksetzen, Abbruch in `loop()` |
| Gerät friert ein, Touch tot | Blockierender Aufruf im Loop (BLE-Suche, TCP-Aufbau, `uart.flush()`) |
| Kryptische Zeichen auf dem Etikett | `toCp1252()` benutzen |
| Doppelte Etikettennummern | nur `services/labels.next_labels()` verwenden (Lock!) |
| MHD wird falsch angezeigt | ISO speichern, `to_display()` erst beim Ausgeben |
| Änderung erscheint nicht im Browser | `hub.notify_ui(...)` vergessen |
| JSON-Liste in der Datenbank ändert sich nicht | neue Liste zuweisen, nicht `append()` – SQLAlchemy erkennt In-Place-Änderungen nicht |
| Druckauftrag verschwindet | nie direkt senden, immer über `print_jobs` |
| Terminal zeigt „Kein Server" | Token in `.env` und im WLAN-Portal vergleichen |
| Etikett läuft auf das nächste über | Totbereich in den Einstellungen eintragen; der Druckkopf erreicht den Anfang nicht |
| Strichcode steht quer zur Schrift | `ESC V` dreht nur Zeichen – gedreht geht nur der QR-Code |
| Folgeetiketten wandern | Ein Etikett muss **genau** eine Teilung Papier verbrauchen, siehe `total_dots()` |
| Umlaute tanzen in der Zeile | Hinting staucht Zeichen mit Aufsatz; `gen_gfx_font.py` zieht die Grundlinie nach |
| Kamera-Scan geht am iPhone nicht | Safari hat kein `BarcodeDetector` (ZXing springt ein) **und** braucht HTTPS |

## Entwicklungsregeln

* Arbeitszweig ist `claude/lebensmittelscanner-redesign-vto9sg`.
* Commits auf Deutsch: präziser Betreff, Begründung im Rumpf.
* Tests laufen ohne Hardware – neue Fachlogik bekommt einen Test in
  `server/tests/`.
* Kein `--no-verify`, kein Force-Push.
