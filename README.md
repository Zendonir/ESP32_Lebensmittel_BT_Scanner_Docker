# Lebensmittel-Scanner 2.0

Haushalts-Inventarsystem mit BLE-Barcodescanner, Etikettendruck und
MHD-Überwachung. Der Docker-Container ist der zentrale Punkt: er hält alle
Daten, kennt den kompletten Ablauf und liefert das Web-Interface aus. Der ESP32
ist nur noch ein Terminal – BLE-Scanner, Anzeige und Drucker.

Nachfolger von [ESP32_Lebensmittel_BT_Scanner](https://github.com/Zendonir/ESP32_Lebensmittel_BT_Scanner),
komplett neu aufgebaut. Warum und was sich dadurch ändert, steht in
[docs/MIGRATION.md](docs/MIGRATION.md).

---

## Architektur

```
   ┌──────────────────────┐             ┌────────────────────────────────┐
   │  BLE-Barcodescanner  │──HID/BLE──▶ │       ESP32-S3 Terminal        │
   └──────────────────────┘             │  Scanner · Anzeige · Drucker   │
                                        └───────────────┬────────────────┘
   ┌──────────────────────┐                             │ WebSocket (JSON)
   │  ESC/POS-Drucker     │◀────UART────────────────────┤
   └──────────────────────┘                             ▼
                                        ┌────────────────────────────────┐
   ┌──────────────────────┐    HTTP     │        Docker-Container        │
   │  Browser / Handy     │◀───────────▶│  Daten · Ablauf · Web · API    │
   └──────────────────────┘             │  SQLite oder PostgreSQL        │
                                        └───────────────┬────────────────┘
                                                        │
                              OpenFoodFacts · ntfy · Telegram · MQTT
```

Die Aufgabenteilung in einem Satz: **das Terminal meldet Ereignisse und zeigt
an, was der Server ihm schickt.** Es gibt auf dem ESP32 keine Datenbank, keinen
Webserver, keine Produktabfrage und keinen Zustandsautomaten mehr.

---

## Was das System kann

**Einlagern** – Barcode scannen, Produktdaten kommen aus OpenFoodFacts oder dem
Cache, MHD und Etikettenzahl am Touchscreen bestätigen, Etikett wird gedruckt.
Für Produkte ohne Barcode (Eingekochtes, Aufschnitt, Restessen) gibt es
Vorlagen mit Kategorie, Marke, Sorte, Füllmenge und Standard-Haltbarkeit.

**Auslagern** – das eigene Etikett scannen. Ein zweiter Scan innerhalb von 48
Stunden bucht wieder ein, falls man sich vertan hat. Alternativ über
Auslagern-Modus per Produkt-Barcode (ältestes MHD zuerst), über die
Ablaufend-Liste am Gerät, per Wischgeste am Handy oder im Web-Interface.

**Überwachen** – täglicher MHD-Bericht per ntfy oder Telegram, Bestandszahlen
per MQTT für Home Assistant, Übersichtsseite mit Statistik.

**Verwalten** – Inventar, Vorlagen, Kategorien, Lagerorte, Produkt-Cache,
Einkaufsliste, Etikettenrolle und Druckwarteschlange im Browser. Mobile PWA mit
Kamera-Scan über die BarcodeDetector-API.

---

## Schnellstart

### 1. Server

```bash
git clone https://github.com/Zendonir/ESP32_Lebensmittel_BT_Scanner_Docker.git
cd ESP32_Lebensmittel_BT_Scanner_Docker

cp .env.example .env
# DEVICE_TOKEN auf einen langen Zufallswert setzen:
sed -i "s/^DEVICE_TOKEN=.*/DEVICE_TOKEN=$(openssl rand -hex 24)/" .env

docker compose up -d
```

Web-Interface: `http://<server>:8080` · Mobil: `http://<server>:8080/mobile`
· API-Dokumentation: `http://<server>:8080/api/docs`

Beim ersten Start werden Kategorien, Lagerorte und ein paar Beispielvorlagen
angelegt. Die Daten liegen im Volume `scanner-data` (`/data/lebensmittel.db`).

Auf einem NAS statt am Terminal? Für **TrueNAS SCALE** liegen fertige Custom-App-YAMLs
unter [`deploy/truenas/`](deploy/truenas/), die Schritte dazu in
[docs/TRUENAS.md](docs/TRUENAS.md).

### 2. Terminal

Am schnellsten ohne Entwicklungsumgebung: Board anstecken und im Browser
(Chrome/Edge) auf
**[zendonir.github.io/ESP32_Lebensmittel_BT_Scanner_Docker](https://zendonir.github.io/ESP32_Lebensmittel_BT_Scanner_Docker/)**
flashen. Fertige `.bin`-Dateien für `esptool.py` oder das Espressif-Tool
liegen bei jedem [Release](https://github.com/Zendonir/ESP32_Lebensmittel_BT_Scanner_Docker/releases).

Unterstützt werden **Waveshare ESP32-S3-Touch-LCD-3.5** (ST7796/FT6336) und
**3.5B** (AXS15231B/QSPI). Im Browser-Installer muss die auf der Platine
aufgedruckte Variante gewählt werden; die Binärdateien sind nicht austauschbar.

Zum Weiterentwickeln in VS Code: **Datei → Arbeitsbereich aus Datei öffnen…** →
`lebensmittel-scanner.code-workspace`, dann der Upload-Pfeil in der
PlatformIO-Leiste. Wichtig ist die Arbeitsbereichsdatei – öffnet man das
Repo-Wurzelverzeichnis, findet PlatformIO die `platformio.ini` unter
`firmware/` nicht. Einzelheiten und Fehlerbilder: [docs/FLASHING.md](docs/FLASHING.md).

Auf der Kommandozeile:

```bash
cd firmware
pio run -e terminal-35 --target upload    # 3.5
pio run -e terminal-35b --target upload   # 3.5B
```

Beim ersten Start – oder wenn beim Einschalten **BOOT** gedrückt wird – öffnet
das Gerät den Access Point `Lebensmittel-Terminal` (Passwort `12345678`). Dort
werden WLAN, Server-Adresse und das `DEVICE_TOKEN` aus der `.env` eingetragen.
Danach startet es neu und meldet sich am Server an; im Web-Interface taucht es
unter **Terminals** auf.

### 3. Ohne Hardware ausprobieren

Unter **Terminals → Scan simulieren** lässt sich ein beliebiger Code in den
Ablauf eines Geräts schicken. Der komplette Weg – Produktabfrage,
MHD-Bildschirm, Etikettenvergabe, Druckauftrag – lässt sich so ohne
BLE-Scanner durchspielen.

---

## Konfiguration

Alles über Umgebungsvariablen in der `.env` (Vorlage: `.env.example`):

| Variable | Bedeutung | Standard |
|---|---|---|
| `DEVICE_TOKEN` | **Pflicht.** Anmeldung der Terminals | `change-me` |
| `UI_PASSWORD` | Passwortschutz Web-Interface (leer = offen im LAN) | – |
| `DATABASE_URL` | SQLite oder `postgresql+asyncpg://…` | SQLite in `/data` |
| `HOUSEHOLD` | Haushaltsname auf dem Etikett | `Standard` |
| `LABEL_PREFIX` | Präfix der Etikettennummern | `LEB` |
| `EXPIRY_WARN_DAYS` | Vorwarnzeit im MHD-Bericht | `3` |
| `EXPIRY_CHECK_HOUR` | Uhrzeit des Berichts | `8` |
| `RESTORE_WINDOW_HOURS` | Fenster fürs Zurückbuchen per Re-Scan | `48` |
| `NTFY_URL` / `NTFY_TOPIC` | ntfy-Benachrichtigung | – |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram-Benachrichtigung | – |
| `MQTT_HOST` / `MQTT_PORT` / … | MQTT für Home Assistant | – |
| `OPENFOODFACTS_ENABLED` | Produktabfrage online | `true` |
| `TZ` | Zeitzone | `Europe/Berlin` |

Laufzeit-Einstellungen (Helligkeit, Signalton, Druckerbreite, QR/Code128,
Vorwarnzeit der Oberfläche) stehen unter **System → Einstellungen** und liegen
in der Datenbank – ein Neustart des Containers ändert daran nichts.

---

## Projektstruktur

```
server/
  app/
    main.py              FastAPI-Anwendung, Web-Auslieferung
    config.py            Einstellungen aus der Umgebung
    models.py            ORM-Modelle
    schemas.py           Ein-/Ausgabeschemas
    api/                 REST: inventory, catalog, labels, system
    device/
      protocol.py        Nachrichtenformat Terminal <-> Server
      hub.py             Verbindungsverwaltung
      workflow.py        Der Ablauf (früher App.cpp)
      routes.py          WebSocket-Endpunkte
    services/            Bestand, Etiketten, OpenFoodFacts, Benachrichtigung,
                         Zeitplan, Datumsformate, Erstbefüllung
  web/                   Web-Interface (kein Build-Schritt)
  tests/                 Ende-zu-Ende-Tests ohne Hardware
  Dockerfile

firmware/
  src/
    main.cpp             ein Loop, keine eigenen Tasks
    core/                Settings (NVS), Net (WLAN, Portal, WebSocket)
    scanner/BLEScanner   NimBLE HID + Batteriedienst
    ui/                  Screen (Renderer), Touch, Buzzer
    printer/Printer      ESC/POS-Blockausführung
  include/config.h       Pinbelegung und Zeitkonstanten
  platformio.ini
```

---

## Entwicklung

```bash
# Server lokal
python3 -m venv .venv && .venv/bin/pip install -r server/requirements-dev.txt
cd server && DEVICE_TOKEN=dev DATABASE_URL=sqlite+aiosqlite:///./dev.db \
  ../.venv/bin/uvicorn app.main:app --reload --port 8080

# Tests (kein ESP32 nötig)
cd server && ../.venv/bin/python -m pytest tests -q

# Linter
../.venv/bin/ruff check server/app server/tests

# Firmware (beide Boardvarianten)
cd firmware && pio run
```

Weitere Unterlagen:
[Architektur](docs/ARCHITECTURE.md) ·
[Geräteprotokoll](docs/PROTOCOL.md) ·
[Migration aus Version 1](docs/MIGRATION.md) ·
[Installation auf TrueNAS](docs/TRUENAS.md) ·
[Firmware flashen](docs/FLASHING.md)