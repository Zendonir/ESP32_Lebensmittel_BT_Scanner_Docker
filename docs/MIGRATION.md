# Von Version 1 zu Version 2

Diese Seite hält fest, **warum** neu angefangen wurde, was aus dem ersten
Projekt übernommen wurde und wie man bestehende Daten mitnimmt.

## Die Analyse des ersten Projekts

Der erste Scanner lief – aber alles lief auf dem ESP32. Die
Firmware-Verzeichnisse umfassten rund eine halbe Million Zeichen Quelltext,
darunter:

| Datei | Größe | Aufgabe |
|---|---|---|
| `src/web/WebInterface.cpp` | 131 KB | 75 HTTP-Routen |
| `src/display.cpp` | 121 KB | Anzeige |
| `data/app.js` | 121 KB | Web-Oberfläche auf LittleFS |
| `src/core/App.cpp` | 111 KB | Zustandsautomat |
| `data/mobile.html` | 68 KB | PWA auf LittleFS |

Dazu kamen ein MySQL-Client, MQTT, Telegram, ntfy, OpenFoodFacts über HTTPS,
eine Sync-Warteschlange, SD-Kartenlogging und OTA für zwei Partitionen.

Die Fallstrick-Tabelle in der damaligen `CLAUDE.md` ist die ehrlichste
Beschreibung des Problems – jeder Eintrag ist ein Symptom derselben Ursache,
nämlich zu viel Arbeit auf einem Mikrocontroller:

* „UI friert sekundenweise ein" → blockierender MySQL-/HTTPS-Aufruf im Loop
* „Gerät hängt dauerhaft" → Watchdog musste einen Neustart erzwingen
* „Scanner koppelt nicht mehr" → hängendes `connecting`-Flag
* „Web-UI hängt beim Etikettendruck" → UART belegt den AsyncTCP-Task
* „WDT-Absturz bei SD-Log" → jedes Log-Schreiben öffnete die SD-Karte
* PSRAM-Task-Stacks lösen `esp_task_stack_is_sane_cache_disabled()` aus

Diese Fehler sind einzeln behebbar – und sie wurden behoben. Sie kommen aber
strukturell immer wieder, solange Datenhaltung, Netzwerk, Oberfläche und
Echtzeitperipherie sich einen 240-MHz-Kern und 512 KB SRAM teilen.

## Was Version 2 anders macht

| Thema | Version 1 | Version 2 |
|---|---|---|
| Daten | JSON auf LittleFS + MySQL per Sync | Eine Datenbank im Container |
| Bestand | aus Event-Strom rekonstruiert (`current_inventory`-View) | eigene Tabelle, Events nur zur Nachvollziehbarkeit |
| Ablauf | `App.cpp` auf dem Gerät | `workflow.py` im Server |
| Web | Async-Webserver auf dem ESP32 | FastAPI im Container |
| Produktabfrage | HTTPS vom ESP32 | Server, mit Cache in der Datenbank |
| Etikettennummer | Zähler im NVS (Factory-Reset = Kollisionen) | Zähler in der Datenbank |
| Etikettenlayout | im C++-Code | Server rendert Blöcke, Firmware führt aus |
| Druckwarteschlange | RAM, nach Reset verloren | Tabelle, überlebt Neustarts |
| Benachrichtigungen | Gerät (TLS, MQTT-Reconnect) | Server |
| OTA | Firmware **und** LittleFS-Abbild | nur Firmware |
| Neustart des Geräts | Datenverlust möglich | kostet die Reconnect-Zeit |
| Test ohne Hardware | nicht möglich | vollständig (`pytest`, Scan-Simulation) |

Ein Fehler in der Ablauflogik führt jetzt zu einem HTTP-500 im Container – nicht
mehr zu einem Watchdog-Reset des Geräts in der Küche.

## Was übernommen wurde

Nicht alles war neu zu erfinden. Bewusst übernommen:

* **Die Pinbelegung** des Waveshare-Boards inklusive der Erkenntnis, dass
  `-DUSE_HSPI_PORT=1` Pflicht ist (sonst Panic mit `EXCVADDR=0x10`).
* **Der BLE-Scanner-Treiber** samt seiner Härtungen: `connecting` wird auf
  jedem Ausstiegspfad zurückgesetzt, Verbindungsversuche brechen nach 30 s ab.
* **Das Zurückbuchen per Re-Scan** – im Alltag bewährt, hier mit
  Datenbank-Fenster statt RAM-Puffer.
* **Die Trennung Scan- und Vorlagen-Workflow**: ohne Einheit ist jedes Etikett
  ein Artikel, mit Einheit ist die Menge die Füllmenge.
* **Die Wischgeste** der mobilen Ansicht, inklusive des Stacking-Kontexts, ohne
  den der Löschen-Knopf durchscheint.
* **Der Watchdog** – jetzt nur noch als letzte Instanz, nicht als
  Betriebsmittel.

## Daten aus Version 1 übernehmen

Es gibt eine eingebaute Importfunktion (`POST /api/import/v1`,
`app/services/importer.py`) mit drei erkannten Dateiformen - je nachdem,
woher die alten Daten kommen.

### Quelle A: die MySQL-Datenbank (`.csv`)

```bash
mysql -B -e "SELECT label_barcode,barcode,name,brand,category,expiry_date,
                    added_date,quantity,household
             FROM Lebensmittel_Scanner.current_inventory" \
  > export.csv
```

(`-B` liefert Tab-getrennte Ausgabe mit Kopfzeile - der Importer erkennt
Komma, Semikolon und Tabulator automatisch.) Deckt nur den **aktiven**
Bestand ab - der View kennt keinen Verlauf.

### Quelle B: die SD-Karte (`.json` / `.zip`)

Version 1 sicherte den internen Speicher täglich nach `/scanner_backup/` auf
der SD-Karte (`BackupManager::doBackup()`). Darin von Interesse:

| Datei | Inhalt |
|---|---|
| `inventory.json` | aktiver Bestand |
| `removed_items.json` | **Auslager-Verlauf** - steckt nicht in der MySQL-CSV |

Die SD-Karte aus dem Gerät nehmen, am Rechner einlesen und entweder
`inventory.json`/`removed_items.json` einzeln hochladen, oder gleich den
ganzen Ordner `scanner_backup` als ZIP:

```bash
cd /pfad/zur/sd-karte
zip -r backup.zip scanner_backup/
```

`categories.json`, `locations.json`, `custom_products.json` und die
Einstellungsdateien im selben Ordner werden beim ZIP-Import stillschweigend
übergangen - siehe Begründung unten.

### Einspielen

Im Web-Interface unter **System → Bestand aus Version 1 übernehmen** die Datei
hochladen (`.csv`, `.json` oder `.zip` - automatisch erkannt). „Nur prüfen"
ist voreingestellt und zeigt, wie viele Zeilen erkannt würden, ohne etwas zu
schreiben - danach den Haken entfernen und erneut hochladen.

Per Kommandozeile:

```bash
curl -X POST "http://localhost:8080/api/import/v1?dry_run=true" \
  -F "file=@export.csv"          # oder inventory.json / backup.zip
```

### Was dabei passiert

* **Etikettennummern bleiben erhalten.** Anders als beim Anlegen über
  `/api/labels` vergibt der Import keine neuen Nummern, sondern übernimmt
  `label_barcode` (CSV) bzw. `labelBarcode` (SD-JSON) unverändert - die
  Aufkleber im Schrank passen danach weiterhin. Der interne Zähler wird
  anschließend über den höchsten importierten Wert gesetzt, damit neu
  angelegte Etiketten nicht kollidieren.
* **`removed_items.json` landet als ausgelagert.** Jeder Eintrag bekommt
  `status="removed"` und, falls vorhanden, das ursprüngliche `removedAt`
  (Unix-Zeitstempel) als Zeitpunkt - der Verlauf bleibt damit nachvollziehbar
  statt nur als "irgendwann vor der Migration" zu erscheinen.
* **Doppelter Import ist gefahrlos.** Eine Etikettennummer, die schon in der
  Datenbank steht, wird übersprungen statt dupliziert - bricht der Import ab,
  einfach dieselbe Datei (oder dasselbe ZIP) erneut hochladen.
* **MHD-Werte** waren in Version 1 gemischt als `YYYY-MM-DD` und
  `DD.MM.YYYY` gespeichert; beide Formen werden normalisiert
  (`app/services/dates.py`).
* **Zeilen ohne Etikettennummer** (z.B. unvollständige alte Exporte) bekommen
  eine aus Name, Barcode und Eingangsdatum abgeleitete Ersatznummer
  (`IMPORT########`) - stabil genug, um auch hier Duplikate zu erkennen.
* **Nicht übernommen** werden Vorlagen, Kategorien und Lagerorte - die legt
  man im Web-Interface schneller neu an, als sie aus der alten Struktur
  eindeutig zuzuordnen. Die Erstbefüllung (`services/seed.py`) liefert dafür
  ohnehin einen brauchbaren Ausgangssatz.
