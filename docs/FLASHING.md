# Firmware installieren und bauen

Zielgerät: **Waveshare ESP32-S3-Touch-LCD-3.5** (N16R8, 16 MB Flash, PSRAM).

Es gibt zwei Wege. Wer nur ein Gerät in Betrieb nehmen will, braucht die
Entwicklungsumgebung nicht.

---

## Weg A: Ohne Entwicklungsumgebung

GitHub baut die Firmware und stellt sie fertig bereit – es muss nichts
übersetzt werden.

### Im Browser

**https://zendonir.github.io/ESP32_Lebensmittel_BT_Scanner_Docker/**

Board per USB-Datenkabel anschließen, auf *Terminal flashen* klicken, Port
auswählen, fertig. Voraussetzung ist **Chrome, Edge oder Opera am Rechner** –
Firefox, Safari und Mobilbrowser können kein WebSerial.

*Erase device* nur beim ersten Mal ankreuzen. Ohne Löschen bleiben WLAN-Zugang,
Server-Adresse und Token erhalten – praktisch beim Aktualisieren.

### Mit einem eigenen Werkzeug

Unter [Releases](https://github.com/Zendonir/ESP32_Lebensmittel_BT_Scanner_Docker/releases)
liegen die fertigen Dateien. Die zusammengefasste genügt:

```bash
esptool.py --chip esp32s3 write_flash 0x0 firmware.factory.bin
```

Für das *Espressif Flash Download Tool* einzeln:

| Datei | Offset |
|---|---|
| `bootloader.bin` | `0x0` |
| `partitions.bin` | `0x8000` |
| `firmware.bin` | `0x10000` |

`firmware.bin` allein ist außerdem das Abbild für ein OTA-Update.
Prüfsummen stehen in `SHA256SUMS.txt`.

### Ein Release erzeugen

Die Dateien entstehen, sobald ein Tag geschoben wird:

```bash
git tag v2.0.0
git push origin v2.0.0
```

Der Workflow `firmware-release.yml` baut, hängt die Dateien an ein
GitHub-Release und veröffentlicht die Installer-Seite. Für einen Testlauf ohne
Tag: Actions → *Firmware veröffentlichen* → *Run workflow* (baut und
aktualisiert die Seite, legt aber kein Release an).

> Einmalig nötig: **Settings → Pages → Source** auf *GitHub Actions* stellen.
> Sonst schlägt der Pages-Auftrag fehl.

---

## Weg B: Mit VS Code

Nötig, sobald am Quelltext etwas geändert werden soll.

## 1. Einmalige Einrichtung

**PlatformIO IDE** in VS Code installieren: Erweiterungen (`Strg+Shift+X`) →
`platformio.platformio-ide` → Installieren → VS Code neu starten. Beim ersten
Start lädt PlatformIO seine eigene Python-Umgebung nach, das dauert ein paar
Minuten.

> Die Arduino-Erweiterung von Microsoft **nicht** parallel installieren – beide
> beanspruchen die serielle Schnittstelle und die IntelliSense-Konfiguration.

**Linux:** der Benutzer braucht Zugriff auf die serielle Schnittstelle,
sonst scheitert der Upload mit `Permission denied: '/dev/ttyACM0'`:

```bash
sudo usermod -aG dialout $USER      # Debian/Ubuntu; Arch/Fedora: uucp
# danach ab- und wieder anmelden
```

**Windows/macOS:** der ESP32-S3 dieses Boards meldet sich über natives USB
(CDC) und braucht in der Regel keinen Treiber. Erscheint kein Port, ist es
meist das USB-Kabel – viele Kabel führen nur Strom. Ein Datenkabel verwenden.

## 2. Projekt öffnen

Repository holen und **die Arbeitsbereichsdatei** öffnen:

```bash
git clone https://github.com/Zendonir/ESP32_Lebensmittel_BT_Scanner_Docker.git
```

In VS Code: **Datei → Arbeitsbereich aus Datei öffnen…** →
`lebensmittel-scanner.code-workspace`

Das ist der entscheidende Punkt: PlatformIO sucht die `platformio.ini` immer
direkt in einem **Wurzelordner** des Arbeitsbereichs. Wer stattdessen das
Repo-Wurzelverzeichnis öffnet, bekommt keine PlatformIO-Leiste und keinen
Upload-Knopf, weil die Datei unter `firmware/` liegt. Die Arbeitsbereichsdatei
hängt `firmware` als eigenen Wurzelordner ein und löst genau das.

Alternativ, wenn nur die Firmware interessiert: einfach **den Ordner
`firmware` direkt öffnen**.

Beim ersten Öffnen lädt PlatformIO Toolchain und Bibliotheken (rund 600 MB,
einmalig). Der Fortschritt steht im Terminal.

## 3. Bauen und flashen

Unten in der Statusleiste (oder über das PlatformIO-Symbol links):

| Symbol | Bedeutung | Tastenkürzel |
|---|---|---|
| ✓ | Build – nur übersetzen | `Strg+Alt+B` |
| → | Upload – übersetzen und flashen | `Strg+Alt+U` |
| 🔌 | Serial Monitor (115200 Baud) | `Strg+Alt+S` |
| 🗑 | Clean | – |

Oder im Terminal:

```bash
cd firmware
pio run                      # bauen
pio run --target upload      # flashen
pio device monitor           # Ausgabe mitlesen
```

Ein sauberer Durchlauf endet mit `[SUCCESS]` und rund 20 % Flash-Belegung.

**Anders als im ersten Projekt gibt es nur ein Abbild.** Es entfällt das
separate `uploadfs` für die Web-Dateien – die liegen jetzt im Server.

## 4. Wenn der Upload nicht greift

Findet PlatformIO das Gerät nicht oder bricht der Upload ab, das Board von
Hand in den Download-Modus bringen:

1. **BOOT** gedrückt halten
2. kurz **RESET** drücken
3. **BOOT** loslassen
4. Upload erneut starten

Nach dem Flashen einmal **RESET** drücken.

> **Achtung, eine Eigenheit dieser Firmware:** BOOT beim *normalen* Start
> gedrückt zu halten erzwingt das WLAN-Einrichtungsportal. Wer nach dem Upload
> die Taste noch hält, landet unter Umständen dort statt im Betrieb – einfach
> loslassen und RESET drücken.

Wird der Port nicht automatisch erkannt, lässt er sich in `platformio.ini`
festnageln:

```ini
upload_port = /dev/ttyACM0     ; Linux
; upload_port = COM5           ; Windows
; upload_port = /dev/cu.usbmodem101   ; macOS
monitor_port = ${env.upload_port}
```

Verfügbare Ports auflisten: `pio device list`

## 5. Erste Inbetriebnahme

Nach dem ersten Flashen ist noch nichts konfiguriert; das Gerät öffnet den
Access Point **`Lebensmittel-Terminal`** (Passwort `12345678`). Mit Handy oder
Laptop verbinden, `http://192.168.4.1` aufrufen und eintragen:

* WLAN-Name und -Passwort (**2,4 GHz** – der ESP32 kann kein 5 GHz)
* Server: IP des Docker-Hosts, ohne `http://` und ohne Pfad
* Port: derselbe wie in der Compose-Datei (Standard 8080)
* Token: das `DEVICE_TOKEN` aus der `.env` bzw. der TrueNAS-YAML

Speichern → das Gerät startet neu und meldet sich am Server. Im
Web-Interface taucht es unter **Terminals** auf.

Später lässt sich das Portal jederzeit wieder erzwingen: **BOOT** halten und
das Gerät einschalten.

---

## Häufige Fehler

| Meldung / Symptom | Ursache |
|---|---|
| Keine PlatformIO-Leiste, kein Upload-Knopf | Repo-Wurzel geöffnet statt Arbeitsbereichsdatei oder `firmware/` |
| `Permission denied: '/dev/ttyACM0'` | Benutzer nicht in der Gruppe `dialout` |
| Kein Port sichtbar | Ladekabel ohne Datenleitungen, oder Board nicht im Download-Modus |
| `A fatal error occurred: Failed to connect` | BOOT/RESET-Ablauf aus Abschnitt 4 |
| Port belegt / `Resource busy` | Serial Monitor läuft noch – erst schließen |
| Display bleibt schwarz | Nach dem Upload einmal RESET; bleibt es dunkel, im Monitor nach `EXCVADDR` suchen |
| Gerät hängt im Einrichtungsportal | BOOT war beim Start gedrückt |
| Terminal zeigt „Kein Server" | Token oder Server-IP im Portal falsch |
| Terminal zeigt „Kein WLAN" | falsches WLAN-Passwort oder 5-GHz-Netz |

**Absturz analysieren:** Der Serial Monitor ist mit
`monitor_filters = esp32_exception_decoder` konfiguriert – ein Backtrace wird
automatisch in Dateinamen und Zeilennummern übersetzt. Beim Start meldet die
Firmware außerdem die Ursache des vorherigen Neustarts (Panic, Task-WDT,
Brownout …).
