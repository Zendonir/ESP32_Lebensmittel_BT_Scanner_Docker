# Installation auf TrueNAS SCALE

Gilt für TrueNAS SCALE 24.10 (Electric Eel) und neuer – ab dieser Version
laufen Apps unter Docker, und eine Custom App ist schlicht eine
Compose-Datei. Fertige YAMLs liegen unter
[`deploy/truenas/`](../deploy/truenas/).

---

## 1. Dataset anlegen

**Datasets → Add Dataset**, z. B. `apps/lebensmittel` im Pool `tank`.

Darin ein Unterverzeichnis für die Daten, und beides dem Apps-Benutzer geben
(UID/GID **568**). In der TrueNAS-Shell:

```bash
mkdir -p /mnt/tank/apps/lebensmittel/data
chown -R 568:568 /mnt/tank/apps/lebensmittel
```

Dieser Schritt ist nicht optional. Fehlt er, startet der Container zwar,
kann aber die Datenbank nicht anlegen und beendet sich beim ersten Zugriff.

## 2. Token erzeugen

```bash
openssl rand -hex 24
```

Der Wert wandert an zwei Stellen: in die YAML als `DEVICE_TOKEN` und später
ins WLAN-Portal des ESP32-Terminals. Stimmen beide nicht überein, weist der
Server die Geräteverbindung mit Code 4401 ab und das Terminal zeigt dauerhaft
„Kein Server".

## 3. App anlegen

**Apps → Discover Apps → Custom App → Install via YAML**

Den Inhalt von [`deploy/truenas/docker-compose.yaml`](../deploy/truenas/docker-compose.yaml)
einfügen und drei Dinge anpassen:

| Stelle | Was |
|---|---|
| `DEVICE_TOKEN` | das eben erzeugte Token |
| `volumes:` | Pfad auf den eigenen Pool (`/mnt/<pool>/apps/lebensmittel/data`) |
| `ports:` | nur falls 8080 auf dem NAS schon belegt ist, z. B. `8095:8080` |

Speichern, starten. Nach etwa 20 Sekunden meldet der Healthcheck „healthy".

Erreichbar ist die Oberfläche dann unter `http://<nas-ip>:8080`, mobil unter
`http://<nas-ip>:8080/mobile`.

## 4. Terminal verbinden

Beim ersten Start – oder wenn beim Einschalten **BOOT** gedrückt wird – öffnet
der ESP32 den Access Point `Lebensmittel-Terminal` (Passwort `12345678`). Dort
eintragen:

* WLAN-Name und -Passwort
* **Server:** die IP des NAS (kein `http://` davor, kein Pfad)
* **Port:** derselbe wie links in `ports:`
* **Token:** das `DEVICE_TOKEN` aus der YAML

Nach dem Neustart taucht das Gerät im Web-Interface unter **Terminals** auf.

---

## Sicherung

Alles, was zählt, liegt im Dataset aus Schritt 1: Datenbank, Produktcache,
Etikettenzähler, Einstellungen. Ein periodischer **Snapshot Task** auf
`apps/lebensmittel` sichert damit den kompletten Zustand.

Zusätzlich gibt es unter **System → Sicherung laden** einen JSON-Export aller
Nutzdaten – praktisch für einen Umzug auf andere Hardware, wo ein
ZFS-Snapshot nicht passt.

## Aktualisieren

Die App zieht `:latest`. Über **Apps → lebensmittel-scanner → Update** bzw.
in der Shell:

```bash
docker compose -f <pfad-zur-yaml> pull
docker compose -f <pfad-zur-yaml> up -d
```

Das Datenbankschema legt der Server beim Start selbst an bzw. erweitert es;
ein Downgrade auf eine ältere Version ist dagegen nicht vorgesehen – vorher
einen Snapshot anlegen.

---

## Wenn etwas nicht läuft

| Symptom | Ursache |
|---|---|
| Container startet und beendet sich sofort | Dataset gehört nicht 568:568 (Schritt 1) |
| App bleibt „Deploying" | Port bereits belegt – anderen Host-Port wählen |
| Terminal zeigt „Kein Server" | Token oder Server-IP im WLAN-Portal falsch |
| Terminal zeigt „Kein WLAN" | WLAN-Zugang im Portal falsch, oder 5-GHz-Netz (der ESP32 kann nur 2,4 GHz) |
| Web-Interface leer, keine Daten | Browser-Konsole prüfen; meist blockt ein Reverse Proxy den WebSocket `/ws/ui` |
| Keine MHD-Benachrichtigungen | `NTFY_*` bzw. `TELEGRAM_*` gesetzt? Test unter **System → Benachrichtigung testen** |

**Logs ansehen:** Apps → lebensmittel-scanner → Logs, oder
`docker logs -f lebensmittel-scanner`.

**Healthcheck von Hand:**

```bash
curl http://<nas-ip>:8080/api/health
```

---

## Reverse Proxy

Läuft davor ein Nginx Proxy Manager oder Traefik, müssen **WebSockets
durchgereicht** werden – sonst funktionieren weder die Terminals (`/ws/device`)
noch die Live-Aktualisierung im Browser (`/ws/ui`).

Nginx:

```nginx
location / {
    proxy_pass http://<nas-ip>:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host       $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Die Geräteverbindung ist im Leerlauf minutenlang still.
    proxy_read_timeout 3600s;
}
```

Beim Nginx Proxy Manager reicht der Schalter **Websockets Support**.

Läuft der Zugang über HTTPS, im WLAN-Portal des Terminals zusätzlich
**HTTPS / WSS** auswählen und den Port des Proxys eintragen (in der Regel 443).

## PostgreSQL statt SQLite

Nur bei konkretem Bedarf – für einen Haushalt ist SQLite die bessere Wahl:
eine Datei, mit dem Dataset-Snapshot gesichert, kein zweiter Container. Wer
trotzdem Postgres will, nimmt
[`docker-compose.postgres.yaml`](../deploy/truenas/docker-compose.postgres.yaml)
und legt vorher zusätzlich `…/postgres` an (ebenfalls `chown 568:568`).
