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
  api/           inventory · catalog · labels · system · firmware · haushalte
  device/        protocol · hub · workflow · routes
  services/      inventory · labels · calibration · categories · openfoodfacts ·
                 notify · scheduler · dates · settings_store · seed · importer ·
                 firmware · updates · deploy · zeichen · hochformat · haushalte ·
                 druckertest
  fonts/         DejaVu Sans fuer das Hochformat-Bild (im Repo, damit Test und
                 Container gleich umbrechen)
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
* **Haushalte:** Die Web-Seite eines Haushalts liegt unter `/h/<id>/` und
  schickt `X-Haushalt` mit (`api.js`). Neue Listen/Zahlen filtern über
  `haushalte.aus_anfrage()` – ohne Kopf (Verwaltung, `/?alle=1`) gilt alles.
  Terminals tragen ihren Haushalt in `Device.household` / `DeviceSession.household`.
  Entnimmt ein Haushalt bei einem anderen, steht das beim Besitzer im Event
  (`household` = Besitzer, `payload.fremd` = Entnehmer).
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
  synchronen HTTP-Aufrufe. Druckaufträge gehen Block für Block hinaus, und
  nur so viel, wie gerade in den Sendepuffer passt (`Printer::process`) – ein
  Hochformat-Etikett braucht bei 9600 Baud rund zehn Sekunden.
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
* Aufgebaut wird **schnell** (15–30 ms, Latenz 0), und die Verschlüsselung
  startet schon in `onConnect` (`secureConnection(true)`). Erst nach dem
  Abonnieren wird auf sparsam (30–50 ms, Latenz 4) umgestellt. Mit den
  sparsamen Werten von Anfang an durfte der Scanner sich je GATT-Abfrage bis
  zu 250 ms Zeit lassen – das waren Sekunden bis „bereit“.
* Wie lange der Scanner nach dem letzten Barcode verbunden bleibt, kommt vom
  Server (`device_ui.scanner_minutes` in der config-Nachricht, 0 = nie
  trennen); `BLE_IDLE_TIMEOUT_MS` ist nur der Startwert.
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
| Etikett soll um 90° gedreht (hochkant) gedruckt werden | Layout `hochformat`: der Server zeichnet ein Bild (`services/hochformat.py`), dreht es und schickt es als gepackte Rasterstreifen. `ESC V` dreht nur Zeichen, die Zeile nicht – und `rotate` stand nie in der Druckauftrag-Nachricht, der alte Schalter "Text drehen" wirkte also gar nicht |
| Hochformat druckt leer oder verzerrt | Bildmodus (`printer.raster_mode`) aus dem Messstreifen, Punkt 2: das der drei Quadrate A/B/C, das maßhaltig ist. Leere Etiketten mit Meldung "Firmware kann kein Hochformat": Terminal meldet `raster2` nicht in `caps` |
| Druckaufträge stehen nach einem Reconnect ewig auf `queued`, Versuche 0 | Die alte Verbindung wird oft erst *nach* dem hello der neuen abgebaut. Aufräumen nur, wenn `hub.unregister()` True liefert, und `drop_session(id, sess)` nur mit der eigenen Sitzung – sonst verschwinden die Fähigkeiten der neuen und das Gerät gilt als offline |
| Hochformat-Auftrag wird nach Firmware-Update verworfen | Fähigkeiten gelten je Verbindung (`DeviceSession.caps` aus hello), nicht aus der Datenbank – vor dem hello bleiben solche Aufträge liegen, statt an veralteten Angaben zu scheitern |
| Rasterblock druckt Müll oder gar nicht | Beim Rasterblock ist `h` die Zeilenzahl der Daten – `render_label` darf sie nicht überschreiben. Bei `ESC * 33` ein Vielfaches von 24, sonst wird das letzte Band zu lang |
| Große Nachricht kommt nie an | WebSockets nimmt höchstens 15 KB, die Firmware 14 KB (`MAX_MESSAGE_BYTES`). Bilddaten deshalb PackBits-gepackt (`z: "rle"`) |
| Drucker langsam, Baudrate hochsetzen | `printer.baud` (Etiketten → Einrichten, Schritt 3) muss zur Einstellung **des Druckers** passen, sonst Zeichensalat. Sie reist mit jedem Auftrag (`"baud"` im print), die Firmware stellt vor dem Auftrag um, erst wenn die Leitung leer ist (`uart_wait_tx_done` mit 0 Ticks). Nicht 9600 geht nur an Terminals mit `baud` in `caps` |
| Rückzug wirkt nicht, obwohl der Drucker es kann | Befehl prüfen: viele namenlose Drucker kennen statt `ESC j` nur `ESC K` (`printer.backfeed_cmd`). ESC K geht als `raw`-Block mit `h` = −Punkte hinaus und braucht die Fähigkeit `raw` |
| `write()` blockiert trotz Prüfung | `availableForWrite()` meldet bei *vollem* Sendepuffer den Platz im 128-Byte-FIFO statt 0. Deshalb immer mindestens `MIN_FREE` (256) verlangen |
| Folgeetiketten wandern | Ein Etikett muss **genau** eine Teilung Papier verbrauchen, siehe `total_dots()`. Damit das keine Hoffnung bleibt, traegt jeder Block seine Hoehe in `h`, und die Firmware setzt `ESC 3` ausdruecklich darauf - `ESC @` stellt sonst den Standardabstand des Druckers ein (~34 statt 24 Punkte) |
| QR-Code wird nicht gelesen | Module muessen quadratisch sein und eine Ruhezone haben; die Reservierung ist ein Vielfaches von 8, weil `ESC *` in Baendern druckt |
| Einstellung wirkt nicht | Erst pruefen, ob sie ueberhaupt gelesen wird - `post_feed_dots`, `printer.qr` und `printer.code128` standen jahrelang in der Oberflaeche, ohne dass sie jemand auswertete |
| Etikett laeuft trotzdem ueber | `render_label()` meldet es in `overflow`; kleinerer Code oder kuerzerer Zuschnitt |
| Neuer Blocktyp im Druckprotokoll | Lieber ein Merkmal an einem vorhandenen Block als ein neuer Typ - eine aeltere Firmware laesst unbekannte Typen stillschweigend fallen (siehe `feed` mit `form`) |
| Versatz summiert sich ueber die Rolle | Bei gestanzten Etiketten `label_end: "formfeed"` - der Drucker sucht die Luecke mit seinem Sensor (`GS FF`) und registriert bei jedem Etikett neu, statt unserer Rechnung zu vertrauen |
| Welche Fassung laeuft hier? | System-Panel, "Fassung" - aus `APP_VERSION`/`APP_COMMIT`/`APP_BUILT`, gesetzt beim Bau des Abbilds. Version **nirgends** ein zweites Mal eintragen, sonst widersprechen sich die Angaben |
| Update-Knopf meldet Fehler, obwohl es lief | Watchtower beendet genau den Container, der die Anfrage gestellt hat - die Antwort kann nicht ankommen. Ein Abriss gilt in `deploy.trigger()` deshalb als Erfolg |
| Abbild-Tags | `:latest` folgt den **Releases**, nicht jedem Commit - wer den Kopf von main will, nimmt `:main`. Releases legt `release.yml` selbst an, sobald CI auf main gruen ist (Patch-Stufe; groessere Spruenge von Hand ueber *Run workflow*) |
| Release-Tag loest image.yml/firmware-release.yml nicht aus | Absicht von GitHub: ein mit `GITHUB_TOKEN` gesetzter Tag stoesst keine Ablaeufe an. `release.yml` ruft beide deshalb selbst auf (`workflow_call`) und reicht Nummer und Commit als Eingabe durch |
| Terminal bietet fuer immer "Update" an | Die Firmware muss unter demselben Namen laufen, unter dem der Server sie aus dem Release ablegt: `FIRMWARE_VERSION` = Release-Tag, gesetzt im Release-Bau, gelesen von `scripts/version.py` |
| Der Server soll sich selbst aktualisieren | Er laedt und startet **keinen** Code aus dem Netz. `services/deploy.py` schickt nur eine Anfrage an einen Dienst, der den Container ersetzen darf; das Abbild bleibt das, was in der Registry steht |
| GitHub-Vergleich meldet immer "aktuell" | `/compare/base...head` liefert `ahead_by` fuer das, was head voraus ist - `behind_by` ist die Gegenrichtung und bleibt dabei 0 |
| Messstreifen faellt in die Etikettenluecken | Ein Test je Etikett, jedes Stueck auf genau eine Teilung aufgefuellt (`calibration.build(stack_mm=…)`). Die Zeilenstrecke richtet sich nach der Etikettenhoehe, der Sollwert steht im Druck und in `anleitung()` |
| Namenloser Drucker, kein Datenblatt | Nicht raten: `POST /api/labels/calibrate` druckt einen Messstreifen, der Zeilenabstand, Rastergeometrie, Rueckzug und Lueckensensor mit dem Lineal ablesbar macht (`services/calibration.py`) |
| Umlaute tanzen in der Zeile | Hinting staucht Zeichen mit Aufsatz; `gen_gfx_font.py` zieht die Grundlinie nach |
| Kamera-Scan geht am iPhone nicht | Safari hat kein `BarcodeDetector` (ZXing springt ein) **und** braucht HTTPS |
| Terminal steht nach einem Netzausfall dauerhaft in der Einrichtung | Das selbsttaetig geoeffnete Portal schliesst sich wieder (`Net::stopPortal`); von Hand geoeffnet bleibt es offen |
| Barcode falsch, mit Zeichen davor | `_buffer` wird auf jedem Trennpfad geleert - ein abgebrochener Code klebte sonst vorn am naechsten |
| Scan geht spurlos verloren | Uebergabe an den Hauptloop nur unter dem Mutex, und `_buffer` bleibt stehen, wenn sie misslingt |
| Rueckwaerts unerklaerliche Abstuerze im Heap | Kein `String` ohne Mutex zwischen NimBLE-Task und Hauptloop anfassen |
| Touch tot, Rest laeuft | `Touch::poll()` sucht den Controller alle 2 s erneut - beim Start wie im Betrieb |
| SD-Karte wird nicht erkannt, wenn sie spaeter kommt | `SdStore::ensureMounted()` versucht es weiter, solange keine sitzt |
| Etikett kommt doppelt aus dem Drucker | Ein Auftrag in `sent` wird nur beim Reconnect erneut geschickt, sonst nie |
| Druckauftrag steht ewig auf `sent` | Nach `PRINT_STALE_SECONDS` zurueck in die Schlange |
| Test scheitert mit `Permission denied: '/data'` | `Settings` friert beim **ersten** Import von `app.config` ein. Umgebungsvariablen fuer Tests gehoeren deshalb in `tests/conftest.py` - pytest laedt die vor jedem Testmodul. In einer Testdatei gesetzt gilt es nur, solange die zufaellig die alphabetisch erste ist |
| SQLite-PRAGMA wirkt nicht | `foreign_keys`/`synchronous` gelten je Verbindung - gehoeren in den `connect`-Listener in `db.py`, nicht in `init_db()` |
| Nach einem Update fehlen halbe Masken, ohne Fehlermeldung | Der Browser hat das neue `index.html`, aber noch das alte `app.js`. Einstellungsfelder werden deshalb ueber `fillSettings()` gefuellt, das nur ueber vorhandene `[data-setting]`-Elemente laeuft - ein entferntes Feld darf nie den Rest mitreissen |
| Browser holt die neue Datei trotzdem nicht | `no-cache` wirkt nur auf kuenftige Antworten; wer die alte Datei schon hat, fragt gar nicht erst. Die Kennung steht deshalb im **Pfad** (`/static/v/<kennung>/…`) und nicht als `?v=` - `app.js` importiert `./api.js` relativ, ein Abfrageteil an `app.js` liesse `api.js` alt |
| Firmware-Bau in der Werkstatt bricht mitten drin ab (`Failed to install Python dependencies into penv`) | Kein Fehler am Code. Die Plattform sucht ihren eigenen Kern als `platformio`, installiert ist er als `pioarduino-core` - sie laedt ihn deshalb einmal pro Bauumgebung neu, und einer der Ladevorgaenge faellt um. Gebaut wird darum mit `PLATFORMIO_OFFLINE=1`; ans Netz darf nur `pio pkg install` davor |
| Etikett soll „Rückzug → Länge → Vorschub“ drucken | `printer.label_sequence: "einfach"`: Rückzug, dann der Inhalt auf genau `label_height_mm` aufgefüllt, dann `label_gap_mm` leer. Totbereich wird dabei nicht gerechnet. Standard bleibt `"teilung"` |
| Walze dreht zwischen Etiketten durch, Papier bleibt stehen | Erst die Rechnung prüfen: im Ablauf „einfach“ muss Höhe + Vorschub danach − Rückzug **genau** der Etikettenabstand sein (`labels.ablauf_einfach`), sonst wandert jedes Etikett, bis der Rückzug eine Etikettkante unter die Walze zieht – das rattert. Mit `printer.label_pitch_mm` rechnet sich der Vorschub danach selbst aus. Dreht es dann noch durch: `printer.feed_mode` „leerzeilen“/„bild“ druckt den Vorschub im langsameren Drucktempo, `feed_step_dots` zerlegt ESC J. Summe und Teilung bleiben in jedem Fall gleich; `form`-Vorschübe werden nie umgewandelt |
| Feld mit `hidden` ist trotzdem zu sehen | `label.field { display: flex }` schlug das Attribut. Deshalb steht ganz oben in `app.css` `[hidden] { display: none !important; }` – nicht entfernen |
| Eigenes Etikett zusammenstellen | Aufteilung `eigen`: `printer.label_custom` ist eine Liste `{feld, stil, unterstrichen}` (Felder/Stile in `labels.EIGEN_FELDER`/`EIGEN_STILE`). Leere Werte fallen weg, `_fit()` kuerzt von hinten, MHD und Code bleiben |
| Scanner ist „da“, aber erst Sekunden später benutzbar | Verbindungsparameter beim Aufbau schnell, Verschlüsselung sofort in `onConnect` – siehe Firmware-Regeln. Wie lange die Kopplung gedauert hat, meldet die Telemetrie (`scanner.setup_ms`) |
| Scanner trennt „von selbst“ | Trenngrund steht unter Terminals (`scanner.reason`, lesbar über `services/scanner.py`). 0x13 = der Scanner legt selbst auf – dessen Energiesparen, einstellbar nur am Scanner; 0x16 = das Terminal (Verbindungsdauer) |
| Neuer Einstellungsschlüssel fehlt in bestehenden Installationen | `settings_store.get()`/`all_settings()` füllen gespeicherte Bereiche mit `DEFAULTS` auf (`_mit_vorgaben`). Ohne das zeigte die Oberfläche ein leeres Feld und ans Terminal ging der Wert nie |
| Neue Einstellung in der Oberflaeche | Feld mit `data-setting="bereich.schluessel"` (und `data-art="int"`/`"float"` fuer Zahlen) - es speichert sich beim Aendern selbst. Kein eigener Speichern-Knopf; `test_oberflaeche.py` prueft, dass der Schluessel in `settings_store.DEFAULTS` steht |
| Kategorie/Lagerort umbenannt, Artikel fallen aus dem Filter | Namen stehen an Artikeln als Text. Umbenennen nur ueber `inventory.rename_category()`/`rename_location()`, die Artikel, Vorlagen, Produkte und Terminals nachziehen |
| `?` statt Anfuehrungszeichen oder Strich auf Etikett/Terminal | Beide kennen nur Latin-1. Text zur Hardware laeuft durch `services/zeichen.py` (in `render_label` und `protocol.screen`/`toast`) - neue Wege zur Hardware ebenso |
| Druckereinstellung in der Oberflaeche geaendert, wirkt nicht | Steht sie in `settings_store.FEST`? Baudrate 9600, 32 Zeichen, Aufteilung `eigen`, Ablauf `einfach` ohne Etikettenabstand, ESC K, Bildmodus B gelten fest und schlagen Gespeichertes. Einstellbar sind nur Breite, Etikettenhoehe, Code, Zeilen, Rueckzug, Nachlauf (`label_gap_mm`) und Vorschub als |
| Lagerort verfaellt / Scan fragt nach dem Ort | Gezaehlt wird ab dem letzten **Erfassen** (Einlagern oder Ort waehlen, `DeviceSession.erfasst()`), nicht ab dem letzten Antippen; Dauer `device_ui.location_minutes`. Gefragt wird erst, wenn eingelagert werden soll (`ort_erfragen`, Ton `ort`); der Scan wartet in `sess.pending` und laeuft nach der Wahl weiter |
| Browser zeigt sein eigenes Passwortfenster / Handy fragt bei jedem Start | Anmeldung laeuft ueber `/login` und das Cookie aus `services/anmeldung.py` (HMAC aus UI_PASSWORD + DEVICE_TOKEN, ein Jahr). Die API antwortet mit 401 **ohne** `WWW-Authenticate`, Seiten leiten per 303 auf `/login` um (`require_ui_page`) |
| Etikett druckt beim falschen Haushalt / gar nicht | Ziel bestimmt `workflow.druckziel()`: gewaehltes Terminal, sonst das eine verbundene des Haushalts, sonst fest am ersten Terminal des Haushalts (`PrintJob.device_pk`, druckt beim Reconnect). Mehrere verbundene: `POST /api/labels` antwortet 409 mit Auswahl. `flush_print_queue` holt nur fest zugeordnete und offene Auftraege des eigenen Haushalts (`PrintJob.household`) |
| `/api/health` 401 / Container "unhealthy" | Der Healthcheck kennt kein Passwort - `system.public_router` liegt ohne Anmeldung und sagt dann nur `{"status": "ok"}` |
| Live-Anzeige ueber den Reverse Proxy tot, Log zeigt `GET /ws/ui … 404` | Der Proxy reicht den WebSocket-Upgrade nicht durch (Nginx Proxy Manager: "Websockets Support") |
| Ein haengender Browser friert das Terminal ein | Jeder Sendevorgang hat eine Zeitgrenze (`hub.SEND_TIMEOUT_S`) |

## Entwicklungsregeln

* Arbeitszweig ist `claude/lebensmittelscanner-redesign-vto9sg`.
* Commits auf Deutsch: präziser Betreff, Begründung im Rumpf.
* Tests laufen ohne Hardware – neue Fachlogik bekommt einen Test in
  `server/tests/`.
* Kein `--no-verify`, kein Force-Push.
