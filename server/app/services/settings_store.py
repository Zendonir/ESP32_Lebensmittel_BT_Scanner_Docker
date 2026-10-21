"""Persistente Laufzeit-Einstellungen (Key/Value in der Datenbank)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting

DEFAULTS: dict[str, Any] = {
    "label_counter": 0,
    "roll_size": 0,
    "roll_used": 0,
    "ui": {
        "theme": "auto",
        "expiring_days": 7,
        "sort": "expiry",
    },
    "device_ui": {
        "brightness": 80,
        "beep": True,
        "idle_seconds": 60,
        # So lange bleibt der Handscanner nach dem letzten Barcode verbunden.
        # 0 = das Terminal trennt nie (leert den Akku des Scanners schneller).
        "scanner_minutes": 10,
        # So lange gilt der gewaehlte Lagerort nach dem letzten Erfassen
        # (Einlagern oder Ort waehlen). Danach fragt der naechste Scan neu.
        # 0 = gilt, bis man ihn von Hand aendert.
        "location_minutes": 30,
    },
    # KI-Vorschlaege fuer neue Produkte (services/ki.py). Wirkt nur, wenn
    # ANTHROPIC_API_KEY in der Umgebung steht - der Schluessel gehoert nicht
    # hierher.
    "ki": {"aktiv": True},
    # Handy: nach einem Produkt-Barcode gleich die MHD-Kamera oeffnen
    # (mit "Ueberspringen"), statt erst auf "MHD scannen" tippen zu muessen.
    "mobil": {"mhd_nach_scan": False},
    "printer": {
        "enabled": True,
        "baud": 9600,
        "paper_chars": 32,
        # "post_feed_dots", "qr" und "code128" standen hier und in der
        # Oberflaeche, wurden gespeichert - und von niemandem gelesen. Der
        # Nachschub wird seit der Umstellung auf Etikettenmasse aus der
        # Teilung gerechnet (services/labels.render_label), und welcher Code
        # gedruckt wird, entscheidet "label_code". Wer an den drei Reglern
        # drehte, um das Wandern der Etiketten loszuwerden, drehte ins Leere.
        "household": "",
        # Etikettenmasse in Millimetern. Der Drucker rechnet in Punkten
        # (203 dpi = 8 je mm); daraus ergibt sich, wie viel ueberhaupt
        # draufpasst - siehe services/labels.py.
        "label_width_mm": 50,
        "label_height_mm": 30,
        "label_layout": "klassisch",
        # Welche Etikettenkante in Papierrichtung laeuft. Sie begrenzt, wie
        # viel Inhalt auf ein Etikett passt. Gibt die Rolle vor, nicht der
        # Geschmack - deshalb getrennt vom Drehen des Textes.
        "label_feed_edge": "hoehe",    # hoehe | breite
        "label_rotate": False,         # Text um 90 Grad drehen
        "label_orientation": "quer",   # Altbestand, wird nur noch gelesen
        # Welcher Code aufs Etikett kommt und wie gross. Auf einem 30-mm-
        # Etikett ist das die groesste Stellschraube: ein QR ist quadratisch
        # und kostet so viel Hoehe wie Breite, ein Strichcode ist flach.
        # Strichcode als Vorgabe: er ist flach und kostet nur ein Drittel
        # der Hoehe eines QR-Codes.
        "label_code": "code128",       # auto | qr | code128
        "label_code_size": "mittel",   # klein | mittel | gross
        # Streifen am Etikettenanfang, den der Druckkopf nicht erreicht.
        # Bestimmt, wie viel vom Etikett wirklich bedruckbar ist - ohne diese
        # Angabe rechnet der Server mit der vollen Hoehe und der letzte Block
        # rutscht aufs naechste Etikett.
        "label_dead_zone_mm": 0,
        # Wie ein Etikett endet.
        #
        # "feed" schiebt den ausgerechneten Rest vor - dann haengt alles an
        # der Rechnung. "formfeed" laesst den Drucker selbst bis zur naechsten
        # Trennluecke fahren (`GS FF`); die Registrierung stimmt dann bei
        # jedem Etikett neu und ein Rest kann sich nicht aufsummieren. Das ist
        # bei gestanzten Etiketten der bessere Weg - aber nur, wenn der
        # Drucker einen Luecken- oder Markensensor hat. Deshalb nicht die
        # Vorgabe: wer ihn nicht hat, bekaeme je nach Modell einen vollen
        # Seitenvorschub oder gar nichts.
        "label_end": "feed",           # feed | formfeed
        # Rueckzug vor dem Druck, in Punkten. Holt den Totbereich zwischen
        # Druckkopf und Abrisskante zurueck. 0 = aus, weil nicht jeder
        # ESC/POS-Drucker rueckwaerts fahren kann.
        "backfeed_dots": 0,
        # Mit welchem Befehl der Drucker zurueckfaehrt: "escj" (ESC j, Epson)
        # oder "esck" (ESC K, viele namenlose Drucker). Herausfinden ueber die
        # Testknoepfe unter Etiketten -> Einrichten.
        "backfeed_cmd": "escj",
        # Die Aufteilung "eigen": Liste von {"feld", "stil", "unterstrichen"}.
        # Leer = Vorschlag aus labels.EIGEN_STANDARD.
        "label_custom": [],
        # "teilung": Etikett + Luecke als eine Teilung, Totbereich gerechnet.
        # "einfach": Rueckzug -> Etikett in label_height_mm -> label_gap_mm leer.
        "label_sequence": "teilung",
        "label_gap_mm": 0,
        # Ablauf "einfach": Abstand der Rolle von Etikettanfang zu
        # Etikettanfang (Etikett + Luecke). Eingetragen ergibt sich der
        # Vorschub danach daraus (labels.ablauf_einfach); 0 = unbekannt.
        "label_pitch_mm": 0,
        # Vorschub in Schritten zu so vielen Punkten (0 = am Stueck).
        "feed_step_dots": 0,
        # Wie vorgeschoben wird: "escj" (ESC J, schnell), "leerzeilen" oder
        # "bild" (gedruckt, im langsameren Drucktempo - gegen Durchdrehen).
        "feed_mode": "escj",
        # Nur fuer Layouts, die als Bild gedruckt werden (Hochformat).
        #
        # Welcher Rasterbefehl auf einem namenlosen Drucker masshaltig
        # druckt, laesst sich nicht vorher wissen - der Messstreifen druckt
        # dafuer drei Quadrate, eines je Modus. "gsv0" ist der Befehl, den
        # die meisten Thermodrucker fuer Bilder verstehen, deshalb die
        # Vorgabe.
        "raster_mode": "gsv0",         # gsv0 | esc33 | esc0
        # Wie das Hochformat-Bild auf dem Papier liegt. Steht das Etikett
        # beim Lesen auf dem Kopf, die andere Richtung waehlen.
        "label_portrait_turn": "links",  # links | rechts
    },
    # Brother-Etikettendrucker am WLAN-Printserver (services/brother.py).
    # Ist er aktiv, druckt er die Etiketten der genannten Haushalte statt
    # des Druckers am Terminal.
    "brother": {
        "aktiv": False,
        # Adresse des Printservers, z. B. http://192.168.1.50 - aus dem
        # Container heraus loest "brother-td2020.local" (mDNS) meist nicht auf.
        "url": "",
        # Leer = alle Haushalte, sonst Namen mit Komma getrennt.
        "haushalt": "",
        # Etikett quer zum Druckkopf und in Laufrichtung, in mm. 0 = aus der
        # Treiberdatei (Profil) bzw. 51 x 26.
        "breite_mm": 0,
        "laenge_mm": 0,
        # Wie das Etikett gelesen wird: keine | 180 | links | rechts.
        "drehung": "keine",
        # Verschiebung quer zum Druckkopf (+ = zur Seite der hoeheren Punkte).
        "versatz_mm": 0,
        # Zeilen gespiegelt senden - falls der Drucker sie von rechts erwartet.
        "spiegeln": False,
        # Aus einer Druckdatei des Originaltreibers gelesen (brother.analysieren).
        # Leer = Standardprofil.
        "profil": {},
    },
}


# Werte, die fuer diesen Drucker feststehen (eingemessen, siehe CLAUDE.md).
# Sie stehen nicht mehr in der Oberflaeche und gelten unabhaengig davon, was
# gespeichert ist - eine alte Einstellung (Teilung, ESC j, Abstand 32 mm)
# soll nicht unsichtbar weiterwirken. Etikett ist immer die eigene
# Aufteilung, der Ablauf immer Rueckzug -> Etikett in Hoehe -> Nachlauf,
# den Nachlauf stellt man von Hand ein (kein Etikettenabstand).
FEST: dict[str, dict[str, Any]] = {
    "printer": {
        "baud": 9600,
        "paper_chars": 32,
        "label_layout": "eigen",
        "label_feed_edge": "hoehe",
        "label_sequence": "einfach",
        "label_pitch_mm": 0,
        "label_dead_zone_mm": 0,
        "label_end": "feed",
        "backfeed_cmd": "esck",
        "raster_mode": "esc33",
    },
}


def _mit_vorgaben(key: str, wert: Any) -> Any:
    """Gespeicherten Bereich mit den Vorgaben auffuellen.

    Gespeichert ist ein Bereich so, wie er beim letzten Aendern war. Kommt
    danach ein neuer Schluessel dazu (etwa `scanner_minutes`), fehlte er in
    jeder bestehenden Installation: die Oberflaeche zeigte ein leeres Feld,
    und ans Terminal ging er nie - bis jemand zufaellig etwas im selben
    Bereich aenderte. Jetzt gilt fuer alles, was nicht gespeichert ist, die
    Vorgabe. Immer als Kopie, damit niemand DEFAULTS veraendert.
    """
    vorgabe = DEFAULTS.get(key)
    if isinstance(vorgabe, dict) and isinstance(wert, dict):
        return {**vorgabe, **wert, **FEST.get(key, {})}
    if isinstance(wert, dict):
        return dict(wert)
    if isinstance(wert, list):
        return list(wert)
    return wert


async def get(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return _mit_vorgaben(key, DEFAULTS.get(key, default))
    try:
        return _mit_vorgaben(key, json.loads(row.value))
    except (ValueError, TypeError):
        return row.value


async def put(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    encoded = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(Setting(key=key, value=encoded))
    else:
        row.value = encoded
    await session.flush()


async def merge(session: AsyncSession, key: str, patch: dict) -> dict:
    """Teilweise Aktualisierung eines Dict-Settings."""
    current = await get(session, key, {})
    if not isinstance(current, dict):
        current = {}
    merged = {**DEFAULTS.get(key, {}), **current, **patch, **FEST.get(key, {})}
    await put(session, key, merged)
    return merged


async def all_settings(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars().all()
    out = {key: _mit_vorgaben(key, wert) for key, wert in DEFAULTS.items()}
    for row in rows:
        try:
            out[row.key] = _mit_vorgaben(row.key, json.loads(row.value))
        except (ValueError, TypeError):
            out[row.key] = row.value
    return out
