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

Version 1 speicherte den Bestand als Event-Strom in der MySQL-Tabelle
`inventory_events`. Der aktuelle Stand lässt sich daraus mit dem damaligen View
`current_inventory` ziehen und über die REST-API einspielen:

```bash
mysql -N -B -e "SELECT label_barcode,barcode,name,brand,category,expiry_date,
                       added_date,quantity,household
                FROM Lebensmittel_Scanner.current_inventory" \
| while IFS=$'\t' read -r label barcode name brand cat mhd added qty haushalt; do
    curl -s -X POST http://localhost:8080/api/labels \
      -H 'Content-Type: application/json' \
      -d "$(jq -n --arg n "$name" --arg b "$brand" --arg c "$cat" \
                  --arg bc "$barcode" --arg e "$mhd" --argjson q "${qty:-1}" \
            '{name:$n, brand:$b, category:$c, barcode:$bc, expiry_date:$e,
              quantity:$q, count:1, print:false}')" > /dev/null
  done
```

Zwei Hinweise dazu:

* **Die Etikettennummern ändern sich.** Der Server vergibt neue, fortlaufende
  Nummern; die alten Aufkleber im Schrank passen danach nicht mehr. Wer den
  Bestand übernehmen will, druckt entweder neu (`"print": true`) oder trägt die
  alten Nummern direkt in die Datenbank ein. Sauberer ist, den Zähler einmalig
  über den vorhandenen Höchstwert zu setzen und die alten Zeilen per SQL zu
  importieren.
* **MHD-Werte lagen gemischt als `YYYY-MM-DD` und `DD.MM.YYYY` vor.** Die API
  normalisiert beides beim Import (`app/services/dates.py`), der Import muss
  also nichts umrechnen.

Vorlagen, Kategorien und Lagerorte lagen in Version 1 als JSON auf LittleFS.
Sie sind schneller im Web-Interface neu angelegt als exportiert – und die
Erstbefüllung bringt einen brauchbaren Satz bereits mit.
