"""Die Ablauflogik des Geraets - vollstaendig serverseitig.

Das ist der Kern des Neuanfangs: `App.cpp` (110 KB Zustandsautomat auf dem
ESP32, der bei jedem Fehler einen Neustart des Geraets erzwang) wird hier zu
einem gewoehnlichen Python-Objekt. Faellt es um, faengt FastAPI die Ausnahme,
das Geraet bekommt eine Fehlermeldung und steht danach wieder auf dem
Startbildschirm - statt in einen Watchdog-Reset zu laufen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    Category,
    Device,
    InventoryItem,
    Location,
    OfflineScan,
    PrintJob,
    Template,
    utcnow,
)
from ..services import inventory as inv
from ..services import categories as cat
from ..services import brother, firmware, haltbarkeit, ki, labels, mengen, openfoodfacts, settings_store
from ..services import scanner as scanner_info
from ..services.dates import days_left, shift_iso, to_display, to_iso_date
from . import protocol as proto
from .hub import hub

log = logging.getLogger(__name__)

# Bildschirmnamen
HOME = "home"
UNKNOWN = "unknown"
ENTER_DATE = "date"
ENTER_QTY = "qty"
RESULT = "result"
LOCATIONS = "locations"
EXPIRING = "expiring"
TMPL_CATEGORY = "tmpl_category"
TMPL_PRODUCT = "tmpl_product"
TMPL_BRAND = "tmpl_brand"
TMPL_SORTE = "tmpl_sorte"
TMPL_AMOUNT = "tmpl_amount"
CONFIRM_REMOVE = "confirm_remove"
UNKNOWN_NAME = "unknown_name"
SUBCATEGORY = "subcategory"
INVENTORY = "inventory"
SYSTEM = "system"
INV_SEARCH = "inv_search"
ROLL_NEW = "roll_new"
NACHTRAG = "nachtrag"          # offline gescannte Produkte nachtragen

def _after_product(sess: "DeviceSession") -> str:
    """Naechster Bildschirm, sobald das Produkt feststeht.

    Hat die Kategorie Unterkategorien und ist noch keine gesetzt, wird sie
    zuerst erfragt - sie gehoert auf das Etikett ("Filet - Schwein") und laesst
    sich hinterher nur muehsam nachtragen.

    Nicht bei Vorlagen: dort entscheidet der Haken "Sorte abfragen" an der
    Vorlage (use_sorten), und der ist schon abgearbeitet. Vorher fragte jede
    Fleisch-Vorlage ohne Sortenliste trotzdem nach - auch "Aufschnitt".

    Bei Vorlagen wird auch kein MHD gefragt: das ergibt sich aus der
    Haltbarkeit der Vorlage fuer die Lagerart des Orts (`_vorlage_mhd`).
    """
    if sess.draft.template_id:
        return ENTER_QTY
    if cat.subcategories_for(sess.draft.category) and not sess.draft.subcategory:
        return SUBCATEGORY
    return ENTER_DATE


# Sortiermodi der Inventarliste, zyklisch per Tap auf den Spaltenkopf -
# genau wie im Vorgaengerprojekt (App.cpp, INV_SORT).
INV_SORT_MODES = ("mhd", "name", "location")


@dataclass
class Draft:
    """Der Artikel, der gerade eingelagert wird."""

    name: str = ""
    barcode: str = ""
    brand: str = ""
    category: str = ""
    subcategory: str = ""
    expiry_date: str = ""
    quantity: float = 1.0
    unit: str = ""
    count: int = 1
    template_id: int | None = None


@dataclass
class DeviceSession:
    """Fluechtiger UI-Zustand je Geraet.

    Bewusst nicht persistiert: nach einem Reconnect landet das Geraet immer auf
    dem Startbildschirm. Ein halb ausgefuellter Entwurf ist weniger wert als
    ein eindeutiger Zustand.
    """

    device_id: str
    stack: list[str] = field(default_factory=lambda: [HOME])
    draft: Draft = field(default_factory=Draft)
    location: str = ""
    screen_id: int = 0
    last_result: list[str] = field(default_factory=list)
    # Letzte Bedienung irgendeiner Art.
    last_action: datetime = field(default_factory=utcnow)
    # Letztes Erfassen: Einlagern oder Lagerort waehlen. Daran haengt, ob der
    # Ort noch gilt - Antippen allein haelt ihn nicht am Leben.
    last_erfasst: datetime = field(default_factory=utcnow)
    # Was nach der Ortswahl weiterlaufen soll: "scan:<code>" oder "store".
    pending: str = ""
    # Gerade nachgetragener Offline-Scan (OfflineScan.id) - erledigt erst,
    # wenn er wirklich eingelagert ist; Abbrechen laesst ihn stehen.
    offline_id: int | None = None
    scanner: dict = field(default_factory=dict)
    online_since: datetime = field(default_factory=utcnow)
    inv_sort: str = "mhd"
    inv_search: str = ""
    roll_size_draft: str = ""
    # Was die Firmware dieser Verbindung kann, aus ihrem hello(). None heisst:
    # noch nicht gemeldet. Die Datenbank kennt nur den Stand der *letzten*
    # Verbindung - direkt nach einem Firmware-Update waere der veraltet.
    caps: set[str] | None = None
    # Haushalt des Terminals (Device.household, sonst der erste Haushalt).
    # Was hier eingelagert wird, gehoert ihm; Listen zeigen nur seins.
    household: str = ""
    # Drucker und USB-Scanner (Device.drucker/usb_scanner), siehe Terminals.
    drucker: str = "seriell"
    usb_scanner: bool = False

    @property
    def current(self) -> str:
        return self.stack[-1] if self.stack else HOME

    def push(self, name: str) -> None:
        self.stack.append(name)

    def pop(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
        else:
            self.stack = [HOME]

    def reset(self) -> None:
        self.stack = [HOME]
        self.draft = Draft()
        self.pending = ""
        self.offline_id = None

    def touch(self) -> None:
        self.last_action = utcnow()

    def erfasst(self) -> None:
        self.last_erfasst = utcnow()

    def erfasst_vor(self) -> float:
        return _seconds_since(self.last_erfasst)

    def idle_seconds(self) -> float:
        # Ueber _seconds_since, damit ein von aussen gesetzter Zeitstempel
        # ohne Zeitzone hier nicht mit einem TypeError endet.
        return _seconds_since(self.last_action)


# Nach dieser Ruhezeit gilt der Lagerort als nicht mehr gueltig. Wer nach
# einer halben Stunde wiederkommt, steht in aller Regel vor einem anderen
# Schrank als vorher - und ein stillschweigend uebernommener Ort ist der
# Fehler, den man erst merkt, wenn man das Etikett am falschen Regal sucht.
#
# Gezaehlt wird ab dem letzten *Erfassen* (Einlagern oder Ort waehlen), die
# Dauer steht in `device_ui.location_minutes` (0 = nie). Dies ist nur die
# Vorgabe, falls die Einstellung fehlt.
LOCATION_IDLE_SECONDS = 30 * 60


async def ort_gilt_sekunden(session: AsyncSession) -> float:
    cfg = await settings_store.get(session, "device_ui", {}) or {}
    try:
        minuten = float(cfg.get("location_minutes", LOCATION_IDLE_SECONDS / 60))
    except (TypeError, ValueError):
        minuten = LOCATION_IDLE_SECONDS / 60
    return minuten * 60 if minuten > 0 else float("inf")


def _seconds_since(stamp: datetime | None) -> float:
    """Alter eines Zeitstempels in Sekunden, robust gegen fehlende Zeitzone.

    SQLite gibt trotz `DateTime(timezone=True)` naive Zeitstempel zurueck.
    Ein naiver von einem bewussten abgezogen ist ein TypeError - und der faellt
    ausgerechnet dann an, wenn ein Geraet nach langer Pause zurueckkommt.
    Ohne Zeitstempel gilt das Geraet als lange weg.
    """
    if stamp is None:
        return float("inf")
    now = utcnow()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


_sessions: dict[str, DeviceSession] = {}


def session_for(device_id: str) -> DeviceSession:
    sess = _sessions.get(device_id)
    if sess is None:
        sess = DeviceSession(device_id=device_id)
        _sessions[device_id] = sess
    return sess


def _eigener_haushalt(sess: DeviceSession) -> tuple:
    """Filter fuer Listen am Terminal: nur der eigene Haushalt."""
    return (InventoryItem.household == sess.household,) if sess.household else ()


def druckt(device_id: str) -> bool:
    """Hat dieses Terminal einen Drucker? Ohne Sitzung: ja (wie bisher)."""
    sess = _sessions.get(device_id)
    return sess is None or sess.drucker != "keiner"


def drucker_online() -> list[str]:
    """Verbundene Terminals mit Drucker - fuer die Verwaltung ohne Haushalt."""
    return [d for d in hub.online_ids() if druckt(d)]


def terminals_fuer(haushalt: str | None) -> list[str]:
    """Verbundene Terminals dieses Haushalts, an denen gedruckt werden kann."""
    if not haushalt:
        return []
    return [d for d in drucker_online()
            if (s := _sessions.get(d)) is not None and s.household == haushalt]


def terminal_fuer(haushalt: str | None) -> str | None:
    """Ein verbundenes Terminal dieses Haushalts - zum Drucken aus dem Web."""
    liste = terminals_fuer(haushalt)
    return liste[0] if liste else None


async def geraet_pk(session: AsyncSession, device_id: str | None) -> int | None:
    if not device_id:
        return None
    return (await session.execute(
        select(Device.id).where(Device.device_id == device_id))).scalar_one_or_none()


async def druckziel(session: AsyncSession, haushalt: str,
                    device_id: str | None = None) -> tuple[str | None, int | None]:
    """Wo ein Etikett aus Web oder Handy gedruckt wird: (online-Terminal, fest an).

    Ausdruecklich gewaehlt gilt das gewaehlte. Sonst das eine verbundene
    Terminal des Haushalts. Ist keines verbunden, wartet der Auftrag fest am
    ersten Terminal dieses Haushalts - er druckt, sobald es wieder da ist,
    und nicht beim Nachbarn, der zufaellig online ist. Kennt der Haushalt gar
    kein Terminal, bleibt er offen (jedes Terminal darf ihn drucken).
    Bei mehreren verbundenen entscheidet der Aufrufer (Nachfrage).
    """
    if device_id:
        return (device_id if device_id in hub.online_ids() else None,
                await geraet_pk(session, device_id))
    online = terminals_fuer(haushalt)
    if len(online) == 1:
        return online[0], await geraet_pk(session, online[0])
    if not online and haushalt:
        pk = (await session.execute(
            select(Device.id).where(Device.household == haushalt, Device.drucker != "keiner")
            .order_by(Device.id)
        )).scalars().first()
        return None, pk
    return None, None


def haushalt_umbenannt(alt: str, neu: str) -> None:
    for sess in _sessions.values():
        if sess.household == alt:
            sess.household = neu


def drop_session(device_id: str, sess: DeviceSession | None = None) -> None:
    """Sitzung verwerfen - mit `sess` nur, wenn es noch genau diese ist.

    Ohne diese Pruefung loeschte eine alte Verbindung, die erst nach dem
    Reconnect abgebaut wurde, die Sitzung der neuen samt ihren Faehigkeiten
    aus dem hello. Danach galt das hello als ausstehend, und jeder Auftrag,
    der eine Faehigkeit braucht, blieb fuer immer auf "queued".
    """
    if sess is None or _sessions.get(device_id) is sess:
        _sessions.pop(device_id, None)


def new_session(device_id: str) -> DeviceSession:
    """Frische Sitzung fuer eine neue Verbindung - nie die der alten erben."""
    sess = DeviceSession(device_id=device_id)
    _sessions[device_id] = sess
    return sess


def rename_location(old: str, new: str) -> None:
    """Laufende Sitzungen auf einen umbenannten Lagerort umhaengen."""
    for sess in _sessions.values():
        if sess.location == old:
            sess.location = new


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------
async def _status_bar(session: AsyncSession, sess: DeviceSession) -> dict:
    counts = await inv.stats(session)
    return {
        "location": sess.location,
        "total": counts["total"],
        "expiring": counts["expiring"] + counts["expired"],
        "battery": sess.scanner.get("battery", -1),
        "scanner": bool(sess.scanner.get("connected")),
    }


async def render(session: AsyncSession, sess: DeviceSession) -> dict:
    """Aktuellen Bildschirm der Sitzung als Protokollnachricht bauen."""
    sess.screen_id += 1
    sid = sess.screen_id
    status = await _status_bar(session, sess)
    name = sess.current

    if name == HOME:
        return await _screen_home(session, sess, sid, status)
    if name == LOCATIONS:
        return await _screen_locations(session, sess, sid, status)
    if name == EXPIRING:
        return await _screen_expiring(session, sess, sid, status)
    if name == UNKNOWN:
        return _screen_unknown(sess, sid, status)
    if name == UNKNOWN_NAME:
        return _screen_unknown_name(sess, sid, status)
    if name == ENTER_DATE:
        return _screen_date(sess, sid, status)
    if name == ENTER_QTY:
        return _screen_qty(sess, sid, status)
    if name == RESULT:
        return _screen_result(sess, sid, status)
    if name == TMPL_CATEGORY:
        return await _screen_tmpl_category(session, sess, sid, status)
    if name == TMPL_PRODUCT:
        return await _screen_tmpl_product(session, sess, sid, status)
    if name == TMPL_BRAND:
        return await _screen_tmpl_brand(session, sess, sid, status)
    if name == TMPL_SORTE:
        return await _screen_tmpl_sorte(session, sess, sid, status)
    if name == TMPL_AMOUNT:
        return _screen_tmpl_amount(sess, sid, status)
    if name == INVENTORY:
        return await _screen_inventory(session, sess, sid, status)
    if name == INV_SEARCH:
        return _screen_inv_search(sess, sid, status)
    if name == SYSTEM:
        return await _screen_system(session, sess, sid, status)
    if name == ROLL_NEW:
        return await _screen_roll_new(session, sess, sid, status)
    if name == NACHTRAG:
        return await _screen_nachtrag(session, sess, sid, status)
    if name == SUBCATEGORY:
        return _screen_subcategory(sess, sid, status)

    sess.reset()
    return await _screen_home(session, sess, sid, status)


async def _screen_home(session, sess, sid, status) -> dict:
    counts = await inv.stats(session)
    roll = await labels.roll_state(session)
    # Neuere Firmware ("home3"): drei Spalten, "Neue Rolle" als sechste
    # Kachel - zwei Reihen gleich grosser Kacheln statt fuenf in zwei Spalten
    # mit einer allein in der letzten Reihe und einem winzigen Knopf daneben.
    drei = "home3" in (sess.caps or set())
    items = [
        {"id": "templates", "label": "Kategorie", "sub": "ohne Barcode", "color": "#4c9eff"},
        {"id": "manual_entry", "label": "Manuell" if drei else "Manuelle Eingabe",
         **({"sub": "Name tippen"} if drei else {}), "color": "#2eb048"},
        {"id": "inventory", "label": "Inventar", **({"sub": "Bestand"} if drei else {}),
         "color": "#cc9218"},
        {"id": "nachdruck", "label": "Nachdruck", "sub": "letztes Etikett", "color": "#8e5bd0"},
        *([{"id": "new_roll", "label": "Neue Rolle", "sub": "Etiketten", "color": "#2c3e50"}]
          if drei else []),
        {"id": "system", "label": "System", **({"sub": "WLAN · Scanner"} if drei else {}),
         "color": "#1c222a"},
    ]
    offen = await _offline_offen(session, sess)
    if offen:
        # Ganz vorn und auffaellig: ohne Nachtragen fehlen diese Artikel im
        # Bestand, und niemand sonst erinnert daran.
        items.insert(0, {"id": "nachtrag", "label": f"Nachtragen ({len(offen)})",
                         "sub": "offline gescannt", "color": "#fb8c00"})
    stats = [
        {"label": "Produkte", "value": counts["total"], "color": "#4c9eff"},
        {"label": "Ablaufend", "value": counts["expiring"], "color": "#cc9218"},
        {"label": "Kritisch", "value": counts["expired"], "color": "#f04640"},
        # Ohne eingerichtete Rolle meldet roll_state -1. Als 0 angezeigt sieht
        # das aus wie "Etiketten alle", dabei ist nur nichts hinterlegt.
        {
            "label": "Label-Rest" if roll["remaining"] >= 0 else "Rolle",
            "value": roll["remaining"] if roll["remaining"] >= 0 else "-",
            "color": "#2eb048" if roll["remaining"] != 0 else "#f04640",
        },
    ]
    subtitle = "Produkt scannen = einlagern  ·  Etikett scannen = auslagern"
    return proto.screen(
        screen_id=sid,
        kind="home",
        title="Übersicht",
        subtitle=subtitle,
        items=items,
        meta={
            "stats": stats,
            "wifi": True,
            "ble": bool(status["scanner"]),
            **({"cols": 3, "roll_button": False} if drei else {}),
        },
        status=status,
    )


async def _screen_locations(session, sess, sid, status) -> dict:
    rows = (
        await session.execute(select(Location).order_by(Location.sort_order, Location.name))
    ).scalars().all()
    items = [
        {
            "id": f"loc:{row.name}",
            "label": row.name,
            # Die Farbe des Orts, wie in der Web-Oberflaeche. Welcher gerade
            # gilt, steht als "aktiv" darunter.
            "sub": "aktiv" if row.name == sess.location else "",
            "color": row.color or "#1c222a",
        }
        for row in rows
    ]
    return proto.screen(
        screen_id=sid,
        kind="list",
        # Lagerorte werden bewusst nur in der Verwaltung angelegt: am Geraet
        # tippt man sich auf einer Bildschirmtastatur schnell "Kuelschrank"
        # ein und hat den Bestand ab da auf zwei Orte verteilt.
        title="Lagerort wählen",
        items=items,
        status=status,
    )


async def _screen_expiring(session, sess, sid, status) -> dict:
    ui_cfg = await settings_store.get(session, "ui", {})
    horizon = (date.today() + timedelta(days=int(ui_cfg.get("expiring_days", 7)))).isoformat()
    rows = (
        await session.execute(
            select(InventoryItem)
            .where(
                InventoryItem.status == "active",
                InventoryItem.expiry_date != "",
                InventoryItem.expiry_date <= horizon,
                *_eigener_haushalt(sess),
            )
            .order_by(InventoryItem.expiry_date.asc())
            .limit(50)
        )
    ).scalars().all()
    items = []
    for row in rows:
        left = days_left(row.expiry_date)
        if left is None:
            sub = ""
            color = "#1c222a"
        elif left < 0:
            sub = f"abgelaufen seit {abs(left)} T"
            color = "#f04640"
        elif left == 0:
            sub = "heute"
            color = "#f04640"
        else:
            sub = f"noch {left} Tage"
            color = "#fb8c00" if left <= 3 else "#2eb048"
        items.append(
            {
                "id": f"item:{row.label}",
                "label": cat.display_name(row.name, row.subcategory)[:32],
                "sub": f"{sub} - {row.location}" if row.location else sub,
                "color": color,
            }
        )
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Ablaufend",
        # Nicht "Antippen = auslagern": das stimmt seit der Umstellung nicht
        # mehr, ausgelagert wird ausschliesslich per Scanner. Eine Anleitung,
        # die ins Leere fuehrt, ist schlimmer als gar keine.
        subtitle="Etikett scannen zum Auslagern",
        items=items or [{"id": "none", "label": "Nichts läuft ab", "color": "#2eb048"}],
        status=status,
    )


def _screen_unknown(sess, sid, status) -> dict:
    return proto.screen(
        screen_id=sid,
        kind="tiles",
        title="Unbekannter Barcode",
        subtitle=sess.draft.barcode,
        lines=["Kein Produkt gefunden.", "Über eine Vorlage anlegen oder verwerfen."],
        items=[
            {"id": "templates", "label": "Vorlage", "color": "#4c9eff"},
            {"id": "name", "label": "Namen eingeben", "color": "#4c9eff"},
            {"id": "generic", "label": "Ohne Namen", "sub": "nur MHD", "color": "#1c222a"},
            {"id": "home", "label": "Verwerfen", "color": "#f04640"},
        ],
        status=status,
    )


def _screen_unknown_name(sess, sid, status) -> dict:
    return proto.screen(
        screen_id=sid,
        kind="keyboard",
        title="Produktname",
        subtitle=sess.draft.barcode,
        value=sess.draft.name,
        meta={"max_len": 60},
        status=status,
    )


def _screen_date(sess, sid, status) -> dict:
    """MHD eintippen - Ziffernblock wie im Vorgaengerprojekt.

    Links Produktangaben und die grosse Anzeige TT.MM.JJ, rechts der
    Ziffernblock. Das Geraet nimmt nach der sechsten Ziffer von selbst an;
    unmoegliche Ziffern sind dort gar nicht erst antippbar.
    """
    draft = sess.draft
    lines = [draft.name or "(ohne Namen)"]
    lines.append(draft.brand or draft.barcode or "")
    return proto.screen(
        screen_id=sid,
        kind="datepad",
        title="Mindesthaltbarkeit",
        lines=lines,
        value=draft.expiry_date,
        # Zwei Wege an der Tastatur vorbei. "Kein MHD" gab es im
        # Vorgaengerprojekt auf diesem Bildschirm nicht, hier aber schon -
        # sonst liesse sich Ware ohne Datum ueberhaupt nicht einlagern.
        items=[
            {"id": "p:0", "label": "Kein MHD", "color": "#1c222a"},
            {"id": "cancel", "label": "Abbrechen", "color": "#cc9218"},
        ],
        status=status,
    )


def _screen_qty(sess, sid, status) -> dict:
    draft = sess.draft
    return proto.screen(
        screen_id=sid,
        kind="number",
        title="Anzahl Etiketten",
        subtitle=draft.name or draft.barcode,
        lines=[f"MHD {to_display(draft.expiry_date) or '-'}", f"Ort {sess.location or '-'}",
               *([f"Menge {mengen.text(draft.quantity, draft.unit)}"] if draft.unit else [])],
        value=draft.count,
        meta={"min": 1, "max": 20, "step": 1, "unit": "Stk"},
        status=status,
    )


def _screen_result(sess, sid, status) -> dict:
    return proto.screen(
        screen_id=sid,
        kind="message",
        title="Gespeichert",
        subtitle=sess.draft.name,
        lines=sess.last_result,
        items=[{"id": "home", "label": "Weiter", "color": "#2eb048"},
               {"id": "nachdruck", "label": "Nochmal drucken", "color": "#8e5bd0"}],
        status=status,
    )


async def _screen_tmpl_category(session, sess, sid, status) -> dict:
    cats = (
        await session.execute(
            select(Template.category).where(Template.category != "").distinct()
        )
    ).scalars().all()
    colors = dict(
        (
            await session.execute(select(Category.name, Category.color))
        ).all()
    )
    items = [
        {"id": f"cat:{c}", "label": c, "color": colors.get(c, "#4c9eff")}
        for c in sorted(cats)
    ]
    items.append({"id": "cat:", "label": "Ohne Kategorie", "color": "#1c222a"})
    return proto.screen(
        screen_id=sid,
        kind="tiles",
        title="Kategorie",
        items=items,
        status=status,
    )


async def _screen_tmpl_product(session, sess, sid, status) -> dict:
    rows = (
        await session.execute(
            select(Template)
            .where(Template.category == sess.draft.category)
            .order_by(Template.sort_order, Template.name)
        )
    ).scalars().all()
    items = [
        {
            "id": f"tpl:{row.id}",
            "label": row.name,
            "sub": f"{row.shelf_days} T" if row.shelf_days else "",
            "color": "#4c9eff",
        }
        for row in rows
    ]
    return proto.screen(
        screen_id=sid,
        kind="list",
        title=sess.draft.category or "Vorlagen",
        items=items or [{"id": "none", "label": "Keine Vorlagen", "color": "#1c222a"}],
        status=status,
    )


async def _screen_tmpl_brand(session, sess, sid, status) -> dict:
    tpl = await session.get(Template, sess.draft.template_id)
    brands = list(tpl.brands or []) if tpl else []
    items = [{"id": f"brand:{b}", "label": b, "color": "#4c9eff"} for b in brands]
    items.append({"id": "brand:", "label": "Ohne Marke", "color": "#1c222a"})
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Marke",
        subtitle=sess.draft.name,
        items=items,
        status=status,
    )


async def _screen_tmpl_sorte(session, sess, sid, status) -> dict:
    tpl = await session.get(Template, sess.draft.template_id)
    # Ohne eigene Liste die der Kategorie (Fleisch: Schwein, Gefluegel, ...).
    sorten = (list(tpl.sorten or []) or list(cat.subcategories_for(tpl.category))) if tpl else []
    items = [{"id": f"sorte:{s}", "label": s, "color": "#4c9eff"} for s in sorten]
    items.append({"id": "sorte:", "label": "Ohne Sorte", "color": "#1c222a"})
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Was ist es?",
        subtitle=f"{sess.draft.name} - die Sorte steht danach auf dem Etikett",
        lines=["Sorten an der Vorlage im Web-Interface"],
        items=items,
        status=status,
    )


def _screen_tmpl_amount(sess, sid, status) -> dict:
    draft = sess.draft
    return proto.screen(
        screen_id=sid,
        kind="number",
        title="Menge",
        subtitle=f"{draft.name} {draft.brand}".strip(),
        # Ueber einen Ziffernblock (pad), weil "3 Wuerstchen" oder "750 g"
        # sich tippen schneller als mit +/- zusammenklicken lassen. Was in
        # einer Packung steckt; wie viele Packungen, fragt ENTER_QTY.
        lines=["Was steckt in einer Packung?", "Je Packung ein Etikett"],
        value=draft.quantity,
        meta={"min": 0, "max": 5000, "step": 50 if draft.unit in ("g", "ml") else 1,
              "unit": draft.unit or "Stk", "pad": True, "label": "Menge:"},
        status=status,
    )


def _screen_subcategory(sess, sid, status) -> dict:
    """Welche Sorte - bei Fleisch etwa Schwein, Rind oder Lamm."""
    options = cat.subcategories_for(sess.draft.category)
    items = [
        {"id": f"sub:{name}", "label": name, "color": "#4c9eff"} for name in options
    ]
    items.append({"id": "sub:", "label": "Ohne Angabe", "color": "#1c222a"})
    return proto.screen(
        screen_id=sid,
        kind="tiles",
        title=sess.draft.category or "Sorte",
        subtitle=sess.draft.name or "",
        items=items,
        status=status,
    )


async def _screen_inventory(session, sess, sid, status) -> dict:
    rows = (
        await session.execute(select(InventoryItem).where(
            InventoryItem.status == "active", *_eigener_haushalt(sess)))
    ).scalars().all()

    search = sess.inv_search.strip().lower()
    if search:
        rows = [r for r in rows if search in r.name.lower()]

    # Gruppierung wie im Vorgaengerprojekt: gleicher Name/Kategorie/
    # Unterkategorie zaehlt als ein Eintrag, Menge wird summiert, das
    # fruehste MHD (und dessen Lagerort/Label) fuehrt die Gruppe an.
    groups: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (row.name, row.category, row.subcategory)
        g = groups.get(key)
        if g is None:
            g = {
                "name": row.name,
                "category": row.category,
                "subcategory": row.subcategory,
                "unit": row.unit,
                "qty": 0.0,
                "expiry": "",
                "location": row.location,
                "label": row.label,
            }
            groups[key] = g
        g["qty"] += row.quantity or 0
        if row.expiry_date and (not g["expiry"] or row.expiry_date < g["expiry"]):
            g["expiry"] = row.expiry_date
            g["location"] = row.location
            g["label"] = row.label

    values = list(groups.values())
    if sess.inv_sort == "name":
        values.sort(key=lambda g: g["name"].lower())
    elif sess.inv_sort == "location":
        values.sort(key=lambda g: (g["location"] or "￿", g["name"].lower()))
    else:
        values.sort(key=lambda g: g["expiry"] or "9999-99-99")

    items = []
    for g in values[:200]:
        left = days_left(g["expiry"]) if g["expiry"] else None
        if left is None:
            color = "#1c222a"
        elif left < 0:
            color = "#f04640"
        elif left <= 3:
            color = "#fb8c00"
        else:
            color = "#2eb048"
        qty = mengen.text(g["qty"], g["unit"]) if g["unit"] else f"{int(g['qty'])}x"
        parts = [g["category"] or "-"]
        if g["location"]:
            parts.append(g["location"])
        if g["expiry"]:
            parts.append(to_display(g["expiry"]))
        parts.append(qty)
        items.append(
            {
                "id": f"item:{g['label']}",
                "label": cat.display_name(g["name"], g["subcategory"])[:32],
                "sub": "  ·  ".join(parts),
                "color": color,
            }
        )

    sort_label = {"mhd": "MHD", "name": "Name", "location": "Ort"}[sess.inv_sort]
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Inventar",
        subtitle=f"Sortiert: {sort_label}  ·  {len(values)} Artikel"
        + (f"  ·  Suche: {sess.inv_search}" if sess.inv_search else ""),
        items=items or [{"id": "none", "label": "Nichts im Bestand", "color": "#1c222a"}],
        # Steuerzeile ueber der Liste - frueher Knoepfe in der Fussleiste.
        meta={
            "controls": [
                {"id": "sort", "label": f"Sortierung: {sort_label}"},
                {"id": "search", "label": "Suche" + (f": {sess.inv_search}" if sess.inv_search else "")},
            ]
        },
        status=status,
    )


def _screen_inv_search(sess, sid, status) -> dict:
    return proto.screen(
        screen_id=sid,
        kind="keyboard",
        title="Inventar durchsuchen",
        subtitle="Nach Namen filtern",
        value=sess.inv_search,
        meta={"max_len": 40},
        status=status,
    )


async def _screen_system(session, sess, sid, status) -> dict:
    device = (
        await session.execute(select(Device).where(Device.device_id == sess.device_id))
    ).scalar_one_or_none()
    tel = (device.telemetry if device else None) or {}
    roll = await labels.roll_state(session)

    net_connected = bool(device and device.online)
    scanner = tel.get("scanner") or {}
    ble_connected = bool(scanner.get("connected"))

    heap_kb = int(tel.get("heap", 0)) // 1024
    uptime_s = int(tel.get("uptime", 0))
    uptime = f"{uptime_s // 60}m {uptime_s % 60}s"

    cards = [
        {
            "title": "NETZWERK",
            "title_color": "#2eb048" if net_connected else "#f04640",
            "status": "Verbunden" if net_connected else "Getrennt",
            "status_color": "#2eb048" if net_connected else "#f04640",
            "lines": [f"{tel.get('ssid') or '-'}  ·  {(device.ip if device else '') or '-'}"],
            "button": {"id": "__local_wifi_setup", "label": "WLAN einrichten", "color": "#cc9218"},
        },
        {
            "title": "BLE SCANNER",
            "title_color": "#cc9218",
            "status": scanner_info.zustand(scanner).capitalize(),
            "status_color": "#2eb048" if ble_connected else "#cc9218",
            # Zweite Zeile: verbunden -> wie lange noch; getrennt -> warum.
            "lines": [
                scanner.get("name") or "kein Gerät gekoppelt",
                (f"bleibt {scanner['idle_min']} min nach dem letzten Scan"
                 if scanner.get("idle_min") else "bleibt verbunden")
                if ble_connected and "idle_min" in scanner
                else scanner_info.trenngrund(scanner.get("reason")),
            ],
            "button": {"id": "__local_ble_toggle", "label": "Verbinden / Trennen", "color": "#4c9eff"},
        },
        {
            "title": "GERÄT",
            "title_color": "#4c9eff",
            "status": (device.name if device else "") or "Terminal",
            "status_color": "#e6edf3",
            # Nur nennen, was auch bekannt ist. "FW: -  ·  ?  ·  ? MB Flash"
            # fuellt die Zeile mit Fragezeichen und sagt nichts.
            "lines": [
                "  ·  ".join(
                    teil
                    for teil in (
                        f"FW {device.firmware}" if device and device.firmware else "",
                        str(tel.get("res") or ""),
                        f"{tel['flash_mb']} MB Flash" if tel.get("flash_mb") else "",
                    )
                    if teil
                )
                or "keine Angaben",
            ],
            "button": {"id": "firmware_update", "label": "Firmware Update", "color": "#2eb048"},
        },
        {
            "title": "SYSTEM",
            # Nicht #1c222a: das ist die Flaechenfarbe der Oberflaeche und auf
            # dem dunklen Grund praktisch unsichtbar. Die Karte hatte dadurch
            # als einzige keine erkennbare Ueberschrift.
            "title_color": "#7a8490",
            "status": "",
            "status_color": "#e6edf3",
            "lines": [
                f"SD: {'eingelegt' if tel.get('sd') else 'nicht eingelegt'}",
                f"Heap: {heap_kb} KB frei",
                f"Uptime: {uptime}",
                f"Labels: {max(0, roll['remaining'])} verbl.",
            ],
        },
    ]
    return proto.screen(
        screen_id=sid,
        kind="cards",
        title="System",
        subtitle=sess.location or "",
        meta={"cards": cards},
        status=status,
    )


async def _screen_roll_new(session, sess, sid, status) -> dict:
    roll = await labels.roll_state(session)
    return proto.screen(
        screen_id=sid,
        kind="number",
        title="Neue Etikettenrolle",
        subtitle=f"Bisherige Rolle: {roll['size'] or '-'} Etiketten",
        value=float(roll["size"] or 200),
        meta={"min": 10, "max": 2000, "step": 10, "unit": "Etiketten"},
        status=status,
    )


# ---------------------------------------------------------------------------
# Ereignisse
# ---------------------------------------------------------------------------
async def push_screen(session: AsyncSession, sess: DeviceSession) -> None:
    await hub.send_to(sess.device_id, await render(session, sess))


async def _ort_vergessen(session: AsyncSession, sess: DeviceSession) -> None:
    sess.location = ""
    device = (
        await session.execute(select(Device).where(Device.device_id == sess.device_id))
    ).scalar_one_or_none()
    if device is not None:
        device.active_location = ""
        await session.commit()


async def expire_location(session: AsyncSession, sess: DeviceSession) -> bool:
    """Lagerort verwerfen, wenn zu lange nichts erfasst wurde.

    Gibt True zurueck, wenn der Ort gerade verfallen ist. Erfragt wird er
    nicht hier, sondern erst, wenn er gebraucht wird (ort_erfragen) - beim
    naechsten Scan oder Einlagern. So bricht kein angefangener Ablauf ab.
    """
    if not sess.location or sess.erfasst_vor() < await ort_gilt_sekunden(session):
        return False
    await _ort_vergessen(session, sess)
    return True


async def ort_erfragen(session: AsyncSession, sess: DeviceSession, weiter: str) -> None:
    """Lagerort abfragen, bevor eingelagert wird - mit eigenem Ton.

    `weiter` ist, was danach weiterlaeuft ("scan:<code>" oder "store"). Der
    Scan geht also nicht verloren: nach der Wahl laeuft er so weiter, als
    waere der Ort schon gesetzt gewesen. Eine Firmware ohne den Ton "ort"
    spielt ihren Standardton.
    """
    sess.pending = weiter
    if sess.current != LOCATIONS:
        sess.push(LOCATIONS)
    await hub.send_to(sess.device_id, proto.beep("ort"))
    await hub.send_to(sess.device_id, proto.toast("Wo wird eingelagert?", "warn"))
    await push_screen(session, sess)


async def check_idle_locations() -> None:
    """Vom Scheduler im Minutentakt aufgerufen.

    Ohne das stuende das Geraet noch eine Stunde spaeter mit dem alten Ort in
    der Statusleiste und behauptete etwas, das nicht mehr gilt. Der Ort wird
    nur still verworfen; gefragt wird beim naechsten Scan.
    """
    kandidaten = [s for s in list(_sessions.values()) if s.location]
    if not kandidaten:
        return

    from ..db import session_scope

    async with session_scope() as session:
        for sess in kandidaten:
            if sess.device_id in hub.online_ids() and await expire_location(session, sess):
                await push_screen(session, sess)


def config_fuer_geraet(device_cfg: dict, sess: DeviceSession | None = None) -> dict:
    """config-Nachricht: Anzeige-Einstellungen plus, woran das Terminal offline
    eigene Etiketten erkennt (nur fuer den Text "wird ausgelagert").

    Mit Sitzung auch, was am Terminal haengt (Drucker, USB-Scanner) - davon
    haengt ab, ob die Firmware den USB-Anschluss als Host betreibt. Ohne
    (Rundruf nach einer Anzeige-Einstellung) fehlen die Felder, und die
    Firmware behaelt ihren Stand.
    """
    from ..config import settings as app_settings

    cfg = {**device_cfg, "label_prefix": app_settings.label_prefix}
    if sess is not None:
        cfg.update(drucker=sess.drucker, usb_scanner=sess.usb_scanner)
    return cfg


async def on_connect(session: AsyncSession, sess: DeviceSession, device: Device) -> None:
    from ..services import haushalte

    sess.household = device.household or await haushalte.standard(session)
    sess.drucker = device.drucker or "seriell"
    sess.usb_scanner = bool(device.usb_scanner)
    sess.location = device.active_location or await inv.default_location(session)
    sess.reset()
    sess.touch()

    # Ein Geraet, das laenger als die Ruhezeit weg war, faengt beim Lagerort
    # an. Der gespeicherte Ort stammt sonst womoeglich von gestern.
    if _seconds_since(device.last_seen) >= await ort_gilt_sekunden(session):
        sess.location = ""
    sess.erfasst()

    device_cfg = await settings_store.get(session, "device_ui", {})
    await hub.send_to(sess.device_id, proto.config(config_fuer_geraet(device_cfg, sess)))
    await push_screen(session, sess)
    # Nach einem Neustart ist die Warteschlange der Firmware leer - was hier
    # noch als "sent" steht, hat das Geraet nie zu Ende gebracht.
    await flush_print_queue(session, sess.device_id, resend_in_flight=True)


async def on_tap(session: AsyncSession, sess: DeviceSession, item: str) -> None:
    """Ein Tipp auf eine Kachel, eine Listenzeile oder einen Fussleisten-Knopf."""
    sess.touch()

    if item == "home":
        sess.reset()
        return await push_screen(session, sess)
    if item == "back":
        sess.pop()
        sess.pending = ""   # Ortswahl abgebrochen - der Scan verfaellt
        return await push_screen(session, sess)
    if item == "cancel":
        sess.reset()
        return await push_screen(session, sess)

    handler = {
        HOME: _tap_home,
        LOCATIONS: _tap_locations,
        EXPIRING: _tap_expiring,
        UNKNOWN: _tap_unknown,
        UNKNOWN_NAME: _tap_unknown_name,
        ENTER_DATE: _tap_date,
        ENTER_QTY: _tap_confirm_save,
        RESULT: _tap_result,
        TMPL_CATEGORY: _tap_tmpl_category,
        TMPL_PRODUCT: _tap_tmpl_product,
        TMPL_BRAND: _tap_tmpl_brand,
        TMPL_SORTE: _tap_tmpl_sorte,
        TMPL_AMOUNT: _tap_confirm_save,
        SUBCATEGORY: _tap_subcategory,
        INVENTORY: _tap_inventory,
        INV_SEARCH: _tap_inv_search,
        SYSTEM: _tap_system,
        ROLL_NEW: _tap_roll_new,
        NACHTRAG: _tap_nachtrag,
    }.get(sess.current)

    if handler is not None:
        await handler(session, sess, item)
    await push_screen(session, sess)


async def _tap_home(session, sess, item) -> None:
    if item == "templates":
        sess.draft = Draft()
        sess.push(TMPL_CATEGORY)
    elif item == "locations":
        sess.push(LOCATIONS)
    elif item == "expiring":
        sess.push(EXPIRING)
    elif item == "manual_entry":
        sess.draft = Draft()
        sess.push(UNKNOWN_NAME)
    elif item == "inventory":
        sess.push(INVENTORY)
    elif item == "system":
        sess.push(SYSTEM)
    elif item == "new_roll":
        sess.push(ROLL_NEW)
    elif item == "nachtrag":
        sess.push(NACHTRAG)
    elif item == "nachdruck":
        await _letztes_nachdrucken(session, sess)


async def _tap_subcategory(session, sess, item) -> None:
    if not item.startswith("sub:"):
        return
    sess.draft.subcategory = item[4:]
    sess.pop()                       # Auswahl ist erledigt
    sess.push(ENTER_DATE)


async def _tap_inventory(session, sess, item) -> None:
    if item == "sort":
        idx = INV_SORT_MODES.index(sess.inv_sort) if sess.inv_sort in INV_SORT_MODES else 0
        sess.inv_sort = INV_SORT_MODES[(idx + 1) % len(INV_SORT_MODES)]
    elif item == "search":
        sess.push(INV_SEARCH)
    # Antippen einer Zeile bucht bewusst nichts aus: Auslagern geht
    # ausschliesslich ueber den Scanner. Die Liste ist reine Anzeige.


async def _tap_inv_search(session, sess, item) -> None:
    if item == "ok":
        sess.pop()


async def _tap_system(session, sess, item) -> None:
    # "WLAN einrichten" und "Verbinden/Trennen" behandelt das Geraet lokal
    # (__local_-Aktionen erreichen den Server gar nicht, siehe main.cpp) -
    # hier landet nur, was tatsaechlich der Server entscheiden muss.
    if item != "firmware_update":
        return

    device = (
        await session.execute(select(Device).where(Device.device_id == sess.device_id))
    ).scalar_one_or_none()
    board = str(((device.telemetry if device else None) or {}).get("board") or "")

    image = firmware.meta(board) if board in firmware.BOARDS else None
    if image is None:
        await hub.send_to(
            sess.device_id, proto.toast("Keine Firmware hinterlegt", "warn")
        )
        return

    # Schon aktuell? Dann nicht ohne Not neu schreiben - ein OTA kostet einen
    # Neustart und einen kompletten Schreibvorgang im Flash.
    if device is not None and device.firmware == image["version"]:
        await hub.send_to(
            sess.device_id, proto.toast(f"Bereits aktuell ({image['version']})")
        )
        return

    await hub.send_to(
        sess.device_id,
        proto.ota(
            path=f"/firmware/{board}.bin",
            version=image["version"],
            size=image["size"],
            sha256=image["sha256"],
        ),
    )


async def _tap_roll_new(session, sess, item) -> None:
    if item != "ok":
        return
    try:
        size = int(float(sess.roll_size_draft or 0))
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        await hub.send_to(sess.device_id, proto.toast("Ungültige Größe", "warn"))
        return
    await labels.new_roll(session, size)
    sess.pop()
    await hub.send_to(sess.device_id, proto.toast(f"Neue Rolle: {size} Etiketten", "success"))


async def _tap_locations(session, sess, item) -> None:
    if not item.startswith("loc:"):
        return
    sess.location = item[4:]
    sess.erfasst()   # die Wahl zaehlt wie Erfassen: der Ort gilt ab jetzt
    device = (
        await session.execute(select(Device).where(Device.device_id == sess.device_id))
    ).scalar_one_or_none()
    if device is not None:
        device.active_location = sess.location
        await session.commit()
    sess.pop()
    await hub.send_to(sess.device_id, proto.toast(f"Ort: {sess.location}"))

    # Wartet etwas auf den Ort, laeuft es jetzt weiter.
    weiter, sess.pending = sess.pending, ""
    if weiter.startswith("scan:"):
        await _scan_barcode(session, sess, weiter[5:])
    elif weiter.startswith("tpl:"):
        await _tap_tmpl_product(session, sess, weiter)
    elif weiter == "store":
        await _save_draft(session, sess)


async def _tap_expiring(session, sess, item) -> None:
    # Wie im Inventar: reine Anzeige. Ausgelagert wird nur ueber den Scanner.
    return


async def _tap_unknown(session, sess, item) -> None:
    if item == "templates":
        sess.stack = [HOME, TMPL_CATEGORY]
    elif item == "name":
        sess.push(UNKNOWN_NAME)
    elif item == "generic":
        sess.draft.name = sess.draft.name or f"Artikel {sess.draft.barcode[-4:]}"
        sess.stack = [HOME, _after_product(sess)]


async def _tap_unknown_name(session, sess, item) -> None:
    if item != "ok":
        return
    if not sess.draft.name.strip():
        await hub.send_to(sess.device_id, proto.toast("Name fehlt", "warn"))
        return
    sess.draft.name = sess.draft.name.strip()
    sess.stack = [HOME, ENTER_DATE]


async def _tap_date(session, sess, item) -> None:
    if item.startswith("p:"):
        days = int(item[2:])
        sess.draft.expiry_date = shift_iso(days) if days > 0 else ""
        sess.push(ENTER_QTY)          # "Kein MHD" fuehrt gleich weiter
    elif item == "ok":
        sess.push(ENTER_QTY)


async def _tap_result(session, sess, item) -> None:
    if item == "nachdruck":
        await _letztes_nachdrucken(session, sess)
        return
    sess.reset()


async def _tap_tmpl_category(session, sess, item) -> None:
    if item.startswith("cat:"):
        sess.draft.category = item[4:]
        sess.push(TMPL_PRODUCT)


async def _tap_tmpl_product(session, sess, item) -> None:
    if not item.startswith("tpl:"):
        return
    tpl = await session.get(Template, int(item[4:]))
    if tpl is None:
        return
    # Das MHD haengt am Ort - also erst den Ort, dann alles andere.
    await expire_location(session, sess)
    if not sess.location:
        await ort_erfragen(session, sess, item)
        return
    draft = sess.draft
    draft.template_id = tpl.id
    draft.name = tpl.name
    draft.category = tpl.category
    draft.unit = tpl.unit
    await _vorlage_mhd(session, sess, tpl)
    draft.quantity = 1.0

    if tpl.brands:
        sess.push(TMPL_BRAND)
    elif tpl.use_sorten:
        sess.push(TMPL_SORTE)
    else:
        sess.push(TMPL_AMOUNT if tpl.unit else _after_product(sess))


async def _vorlage_mhd(session: AsyncSession, sess: DeviceSession, tpl: Template | None = None) -> None:
    """MHD einer Vorlage aus der Lagerart des aktuellen Orts.

    Gefragt wird es bei Vorlagen nicht - "Hack" im Gefrierschrank haelt eben
    anders als im Kuehlschrank, und genau das steht an der Vorlage.
    """
    draft = sess.draft
    if tpl is None:
        tpl = await session.get(Template, draft.template_id)
    if tpl is None:
        return
    draft.expiry_date = (await haltbarkeit.vorschlag(
        session, ort=sess.location, kategorie=tpl.category,
        # Alte Vorlagen kennen nur shelf_days; wer je Lagerart eingetragen
        # hat, meint mit einer leeren Art "wie die Kategorie".
        eigene_tage=0 if tpl.haltbarkeit else tpl.shelf_days,
        vorlage=tpl.haltbarkeit, herkunft="Vorlage"))["datum"]


async def _tap_tmpl_brand(session, sess, item) -> None:
    if not item.startswith("brand:"):
        return
    sess.draft.brand = item[6:]
    tpl = await session.get(Template, sess.draft.template_id)
    if tpl is not None and tpl.use_sorten:
        sess.push(TMPL_SORTE)
    else:
        sess.push(TMPL_AMOUNT if sess.draft.unit else _after_product(sess))


async def _tap_tmpl_sorte(session, sess, item) -> None:
    if not item.startswith("sorte:"):
        return
    sess.draft.subcategory = item[6:]
    sess.push(TMPL_AMOUNT if sess.draft.unit else _after_product(sess))


async def _tap_confirm_save(session, sess, item) -> None:
    if item != "ok":
        return
    if sess.current == TMPL_AMOUNT:
        sess.push(_after_product(sess))
        return
    await _save_draft(session, sess)


async def _einfrieren(session: AsyncSession, sess: DeviceSession) -> None:
    """Aufgedrucktes MHD im Gefrierschrank verlaengern - ausser die Ware ist
    schon tiefgekuehlt gekauft (services/haltbarkeit.eingefroren)."""
    draft = sess.draft
    if draft.template_id is not None:
        return                          # Vorlagen rechnen ihr MHD selbst
    produkt = await inv.product_for_barcode(session, draft.barcode) if draft.barcode else None
    neu = await haltbarkeit.eingefroren(session, mhd=draft.expiry_date, ort=sess.location,
                                        kategorie=draft.category, produkt=produkt)
    if neu["verlaengert"]:
        draft.expiry_date = neu["datum"]
        await hub.send_to(sess.device_id, proto.toast(
            f"Eingefroren: MHD {to_display(neu['datum'])}", "success"))
    elif neu["hinweis"]:                # Gefrierschrank, aber MHD bleibt - und warum
        await hub.send_to(sess.device_id, proto.toast(neu["hinweis"].split(" (")[0]))


async def on_input(session: AsyncSession, sess: DeviceSession, value) -> None:
    """Wert aus einem date/number/text-Bildschirm uebernehmen."""
    sess.touch()

    current = sess.current
    if current == ENTER_DATE:
        sess.draft.expiry_date = to_iso_date(value)
        await _einfrieren(session, sess)
    elif current == ENTER_QTY:
        try:
            sess.draft.count = max(1, min(20, int(float(value))))
        except (TypeError, ValueError):
            sess.draft.count = 1
    elif current == TMPL_AMOUNT:
        try:
            sess.draft.quantity = float(value)
        except (TypeError, ValueError):
            sess.draft.quantity = 1.0
    elif current == UNKNOWN_NAME:
        sess.draft.name = str(value or "").strip()
    elif current == INV_SEARCH:
        sess.inv_search = str(value or "").strip()
    elif current == ROLL_NEW:
        sess.roll_size_draft = str(value or "")
    await push_screen(session, sess)


async def _save_draft(session: AsyncSession, sess: DeviceSession) -> None:
    # Auch ohne Scan (Vorlage, manuelle Eingabe) nie ohne gueltigen Ort.
    await expire_location(session, sess)
    if not sess.location:
        await ort_erfragen(session, sess, "store")
        return
    sess.erfasst()
    draft = sess.draft
    if draft.template_id:
        # Ort kann sich unterwegs geaendert haben (Statusleiste, Ablauf).
        await _vorlage_mhd(session, sess)
    # Ohne Fuellmenge ist jedes Etikett genau ein Artikel; mit Einheit ist die
    # Menge die Fuellmenge und die Etikettenzahl davon unabhaengig.
    quantity = draft.quantity if draft.unit else 1.0
    items, jobs = await inv.add_items(
        session,
        name=draft.name,
        barcode=draft.barcode,
        brand=draft.brand,
        category=draft.category,
        subcategory=draft.subcategory,
        expiry_date=draft.expiry_date,
        quantity=quantity,
        unit=draft.unit,
        location=sess.location,
        count=draft.count,
        device_id=sess.device_id,
        want_print=True,
        household=sess.household,
    )
    if sess.offline_id:
        row = await session.get(OfflineScan, sess.offline_id)
        if row is not None:
            row.done = True
        sess.offline_id = None
    # Am Terminal erfasst = an diesem Terminal gedruckt (ausser der Haushalt
    # druckt am Brother - dann gilt kein Terminal).
    pk = await geraet_pk(session, sess.device_id)
    for job in jobs:
        if not job.ziel:
            job.device_pk = pk
    produkt = await inv.produkt_merken(session, barcode=draft.barcode, name=draft.name,
                                       brand=draft.brand, category=draft.category,
                                       subcategory=draft.subcategory)
    await session.commit()
    ki.im_hintergrund(produkt.id if produkt else None)
    if any(job.ziel == brother.ZIEL for job in jobs):
        brother.anstossen()

    sess.last_result = [
        f"{len(items)} Etikett(en)",
        items[0].label if len(items) == 1 else f"{items[0].label} - {items[-1].label}",
        f"MHD {to_display(draft.expiry_date) or '-'}",
        f"Ort {sess.location or '-'}",
    ]
    sess.stack = [HOME, RESULT]
    await hub.send_to(sess.device_id, proto.beep("print" if jobs else "ok"))
    await flush_print_queue(session, sess.device_id)
    await hub.notify_ui("inventory")


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def is_label(code: str) -> bool:
    """Ist das eine eigene Etikettennummer (LEB000123) und kein Produktcode?

    Oeffentlich, weil die REST-Schnittstelle dieselbe Unterscheidung braucht:
    ein Etikett gehoert nie zu OpenFoodFacts.
    """
    return code.upper().startswith(settings.label_prefix.upper()) and code[
        len(settings.label_prefix):
    ].isdigit()


async def on_scan(session: AsyncSession, sess: DeviceSession, code: str) -> None:
    code = code.strip()
    if not code:
        return
    log.info("[%s] Scan: %s (Modus %s)", sess.device_id, code, sess.current)

    # Auslagern geht immer: der Artikel liegt schon irgendwo, dafuer braucht es
    # keinen aktiven Lagerort. Einlagern dagegen schon - sonst entstuende ein
    # Eintrag ohne Ort, und danach sucht ihn niemand.
    if is_label(code):
        sess.touch()
        await _scan_label(session, sess, code)
        await push_screen(session, sess)
        return

    # Einlagern braucht einen gueltigen Ort. Ist er abgelaufen oder fehlt,
    # kommt erst die Ortswahl - der Scan wartet und laeuft danach weiter.
    sess.touch()
    await expire_location(session, sess)
    if not sess.location:
        sess.reset()
        await ort_erfragen(session, sess, f"scan:{code}")
        return
    await _scan_barcode(session, sess, code)
    await push_screen(session, sess)


async def _scan_label(session, sess, code) -> None:
    """Ein eigenes Etikett wurde gescannt."""
    item = await inv.find_active_by_label(session, code)
    if item is not None:
        await inv.remove_item(session, item, reason="scan", device_id=sess.device_id,
                              household=sess.household)
        await session.commit()
        await hub.send_to(sess.device_id, proto.beep("ok"))
        await hub.send_to(
            sess.device_id, proto.toast(f"{item.name} ausgelagert", "success")
        )
        await hub.notify_ui("inventory")
        sess.reset()
        return

    # Bereits ausgelagert und noch im Rueckbuch-Fenster: derselbe Scan bucht
    # wieder ein - unveraendert aus dem ersten Projekt uebernommen, weil das
    # Verhalten sich im Alltag bewaehrt hat.
    restored = await inv.restore_by_label(
        session, code, location=sess.location, device_id=sess.device_id
    )
    if restored is not None:
        await session.commit()
        await hub.send_to(sess.device_id, proto.beep("ok"))
        await hub.send_to(
            sess.device_id, proto.toast(f"{restored.name} zurückgebucht", "success")
        )
        await hub.notify_ui("inventory")
        sess.reset()
        return

    await hub.send_to(sess.device_id, proto.beep("error"))
    await hub.send_to(sess.device_id, proto.toast(f"{code} unbekannt", "error"))


async def _scan_barcode(session, sess, code) -> None:
    """Ein Produkt-Barcode wurde gescannt."""
    await hub.send_to(sess.device_id, proto.beep("scan"))
    product = await openfoodfacts.lookup(session, code)
    await session.commit()

    draft = Draft(barcode=code)
    if product is not None and product.name:
        draft.name = product.name
        draft.brand = product.brand
        draft.category = product.category
        draft.subcategory = product.subcategory
        # "1l" aus der Produktdatenbank: Menge einer Packung, steht so auf
        # dem Etikett ("1 l"). Ohne lesbare Angabe bleibt es beim Stueck.
        angabe = mengen.aus_angabe(product.amount)
        if angabe:
            draft.quantity, draft.unit = angabe
        # Je nach Lagerart des Orts: im Gefrierschrank haelt dasselbe Produkt
        # Monate, im Kuehlschrank Tage (services/haltbarkeit.py).
        draft.expiry_date = (await haltbarkeit.vorschlag(
            session, ort=sess.location, kategorie=product.category, produkt=product))["datum"]
        sess.draft = draft
        ki.im_hintergrund(product.id)
        sess.stack = [HOME, _after_product(sess)]
    else:
        await inv.log_event(
            session, "scan_unknown", barcode=code, device=sess.device_id
        )
        await session.commit()
        sess.draft = draft
        sess.stack = [HOME, UNKNOWN]


# ---------------------------------------------------------------------------
# Druckwarteschlange
# ---------------------------------------------------------------------------
# Wie viele Druckauftraege ein Durchgang an das Geraet schickt.
#
# Die Warteschlange in der Firmware fasst acht (Printer::MAX_QUEUE); was
# darueber hinaus ankommt, lehnt das Geraet ab. Vorher gingen bis zu zwanzig
# auf einmal hinaus - bei einer Sammlung Etiketten zaehlte der Server damit
# fuer jeden abgelehnten Auftrag einen Versuch, und nach fuenf Durchgaengen
# stand ein voellig gesunder Auftrag auf "failed", ohne je gedruckt worden zu
# sein. Vier auf einmal lassen Luft fuer die Auftraege, die gerade laufen.
PRINT_BATCH = 4

# So lange darf ein Auftrag beim Geraet liegen, bevor er als verloren gilt.
# Ein Hochformat-Etikett ist ein Bild und braucht bei 9600 Baud rund zehn
# Sekunden (bei 115200 unter einer); mit vier Auftraegen beim Geraet kommt
# die Rueckmeldung zum letzten also erst nach gut 40 s. Mit 60 s lag das zu knapp an der Grenze -
# ein Auftrag, der nur langsam war, waere erneut gesendet und doppelt
# gedruckt worden.
PRINT_STALE_SECONDS = 180


async def letztes_etikett(session: AsyncSession, haushalt: str) -> InventoryItem | None:
    """Artikel zum zuletzt gedruckten Etikett - Test- und Messstreifen zaehlen nicht."""
    stmt = (select(InventoryItem)
            .join(PrintJob, PrintJob.label == InventoryItem.label)
            .order_by(PrintJob.id.desc()).limit(1))
    if haushalt:
        stmt = stmt.where(PrintJob.household == haushalt)
    return (await session.execute(stmt)).scalars().first()


async def nachdrucken(session: AsyncSession, item: InventoryItem,
                      device_id: str | None = None, ziel: str | None = None) -> dict:
    """Etikett eines Artikels neu drucken, ohne zweiten Bestandseintrag.

    Im ersten Projekt gab es nur "letztes Etikett erneut drucken" aus einem
    RAM-Puffer; nach einem Neustart war die Vorlage weg. Hier wird aus dem
    Artikel neu gerendert - mit den aktuellen Druckereinstellungen.
    """
    printer_cfg = await settings_store.get(session, "printer", {})
    daten = {
        "label": item.label,
        "name": item.name,
        "brand": item.brand,
        "subcategory": item.subcategory,
        "expiry_date": item.expiry_date,
        "added_date": item.added_date,
        "quantity": item.quantity,
        "unit": item.unit,
        "location": item.location,
    }
    # Ist der Brother das Ziel (gewaehlt oder Standardziel), dann auch fuer den
    # Nachdruck - egal, an welchem Terminal "Nachdruck" angetippt wurde.
    if brother.an_brother(await settings_store.get(session, "brother", {}),
                          item.household or "", ziel):
        job = PrintJob(label=item.label, payload=brother.auftrag(daten), status="queued",
                       household=item.household or "", ziel=brother.ZIEL)
        session.add(job)
        await labels.consume_roll(session, 1)
        await inv.log_event(session, "print", label=item.label, name=item.name)
        await session.commit()
        brother.anstossen()
        return {"ok": True, "job": job.id, "sent": True, "label": item.label,
                "name": item.name, "ziel": brother.ZIEL}

    payload = labels.render_label(daten, printer_cfg)
    target, pk = await druckziel(session, item.household or "", device_id)
    job = PrintJob(label=item.label, payload=payload, status="queued",
                   household=item.household or "", device_pk=pk)
    session.add(job)
    # Ein Nachdruck verbraucht ein Etikett wie jeder andere Druck auch.
    await labels.consume_roll(session, 1)
    await inv.log_event(session, "print", label=item.label, name=item.name)
    await session.commit()

    if target is None and pk is None:
        target = terminal_fuer(item.household) or next(iter(drucker_online()), None)
    sent = await flush_print_queue(session, target) if target else 0
    return {"ok": True, "job": job.id, "sent": bool(sent), "label": item.label, "name": item.name}


async def _letztes_nachdrucken(session: AsyncSession, sess: DeviceSession) -> None:
    item = await letztes_etikett(session, sess.household or "")
    if item is None:
        await hub.send_to(sess.device_id, proto.toast("Noch kein Etikett gedruckt", "warn"))
        return
    # Am eigenen Terminal drucken - dort steht, wer gerade nachdruckt.
    await nachdrucken(session, item, sess.device_id)
    await hub.send_to(sess.device_id, proto.toast(f"Nachdruck {item.label}", "success"))


async def flush_print_queue(
    session: AsyncSession, device_id: str, resend_in_flight: bool = False
) -> int:
    """Offene Druckauftraege an ein Geraet senden.

    Wird nach dem Speichern, nach jeder Rueckmeldung des Geraets und nach
    jedem Reconnect aufgerufen - ein Etikett, das waehrend eines Neustarts
    entstanden ist, geht damit nicht verloren.

    Es bleiben hoechstens `PRINT_BATCH` Auftraege gleichzeitig beim Geraet.
    Vorher ging jeder Durchgang mit voller Breite hinaus, ohne zu zaehlen, was
    noch unterwegs war - bei einer Sammlung Etiketten lief die Warteschlange
    der Firmware damit ueber, jeder abgelehnte Auftrag zaehlte als Versuch,
    und nach fuenf Durchgaengen stand ein voellig gesunder Auftrag auf
    "failed", ohne je gedruckt worden zu sein.

    `resend_in_flight` gehoert **nur** zum Reconnect. Ein Auftrag im Zustand
    "sent" liegt in der Warteschlange der Firmware und wird gerade gedruckt;
    ihn noch einmal zu schicken heisst, ihn ein zweites Mal zu drucken. Genau
    das geschah bisher bei jedem Aufruf: wer zwei Artikel kurz hintereinander
    speicherte, bekam das erste Etikett doppelt aus dem Drucker. Nach einem
    Geraeteneustart ist die Warteschlange dagegen leer - dort ist erneutes
    Senden die einzige Rettung fuer das Etikett.
    """
    conn = hub.get(device_id)
    if conn is None or not druckt(device_id):
        return 0

    if resend_in_flight:
        offen = ("queued", "sent")
        platz = PRINT_BATCH
    else:
        # Auftraege, zu denen seit PRINT_STALE_SECONDS keine Rueckmeldung kam,
        # gelten als verloren und gehen zurueck in die Schlange.
        #
        # Ein Etikett braucht unter zwei Sekunden. Bleibt die Antwort aus, ist
        # das Geraet zwischendurch weggewesen, ohne dass der Server es gemerkt
        # hat (halboffener Socket). Ohne diesen Schritt blieb der Auftrag fuer
        # immer auf "sent" stehen: er wurde nie gedruckt, tauchte in keiner
        # Fehlerliste auf, und weil er als "unterwegs" zaehlte, versperrte er
        # auch noch den Platz fuer die naechsten.
        verfallen = utcnow() - timedelta(seconds=PRINT_STALE_SECONDS)
        for job in (
            await session.execute(
                select(PrintJob).where(
                    PrintJob.status == "sent", PrintJob.updated_at < verfallen,
                    PrintJob.ziel == "",
                )
            )
        ).scalars().all():
            log.info("Druckauftrag %d ohne Rueckmeldung - zurueck in die Schlange", job.id)
            job.status = "queued"

        offen = ("queued",)
        unterwegs = (
            await session.execute(
                select(func.count())
                .select_from(PrintJob)
                .where(PrintJob.status == "sent", PrintJob.ziel == "")
            )
        ).scalar_one()
        platz = PRINT_BATCH - int(unterwegs)
        if platz <= 0:
            await session.commit()
            return 0

    # Nur, was an dieses Terminal gehoert: fest zugeordnete Auftraege, und
    # offene seines Haushalts (oder ohne Haushalt, oder eines Haushalts ohne
    # eigenes Terminal). Vorher druckte jedes Terminal alles, was in der
    # Schlange lag - auch die Etiketten des Nachbarhaushalts.
    pk = await geraet_pk(session, device_id)
    haushalt = session_for(device_id).household
    mit_terminal = select(Device.household).where(Device.household != "")
    gehoert_hierher = or_(
        PrintJob.device_pk == pk if pk is not None else false(),
        and_(PrintJob.device_pk.is_(None),
             or_(PrintJob.household == "", PrintJob.household == haushalt,
                 PrintJob.household.not_in(mit_terminal))),
    )
    jobs = (
        await session.execute(
            select(PrintJob)
            .where(PrintJob.status.in_(offen), PrintJob.ziel == "", gehoert_hierher)
            .order_by(PrintJob.id)
            .limit(platz)
        )
    ).scalars().all()

    if session_for(device_id).drucker == "usb":
        return await _usb_senden(session, conn, device_id, jobs)

    # Was das Terminal kann, aus seinem hello() in *dieser* Verbindung. Ein
    # Hochformat-Etikett an eine Firmware ohne Bildstreifen zu schicken
    # hiesse: sie wirft leeres Papier aus und meldet Erfolg. Solange das
    # hello noch aussteht, bleibt so ein Auftrag einfach liegen - der
    # Aufruf nach dem hello schickt ihn dann los oder sagt, woran es liegt.
    kann = session_for(device_id).caps
    # Eine andere Baudrate als 9600 versteht nur eine Firmware, die "baud"
    # meldet. Eine aeltere liesse die Angabe fallen und sendete mit 9600 an
    # einen Drucker, der auf etwas anderes wartet - Zeichensalat statt
    # Etikett, und dem Server gegenueber "gedruckt".
    baud = labels.baudrate(await settings_store.get(session, "printer", {}) or {})

    sent = 0
    for job in jobs:
        if job.attempts >= 5:
            job.status = "failed"
            job.error = job.error or "zu viele Versuche"
            continue
        braucht = (job.payload or {}).get("braucht")
        noetig = [n for n in (braucht if isinstance(braucht, list) else [braucht]) if n]
        if baud != labels.BAUD_STANDARD:
            noetig.append(labels.BAUD_FAEHIGKEIT)
        if noetig and kann is None:
            continue
        fehlt = [n for n in noetig if n not in kann]
        if fehlt:
            job.status = "failed"
            job.error = (
                f"Firmware des Terminals kann nur mit 9600 Baud drucken, eingestellt "
                f"sind {baud} - unter Terminals ein Firmware-Update einspielen"
                if labels.BAUD_FAEHIGKEIT in fehlt else
                "Firmware des Terminals kennt die Testbefehle noch nicht - "
                "unter Terminals ein Firmware-Update einspielen"
                if "raw" in fehlt else
                "Firmware des Terminals kann kein Hochformat - "
                "unter Terminals ein Firmware-Update einspielen"
            )
            continue
        if await conn.send(proto.print_job(job.id, job.payload, baud)):
            job.status = "sent"
            job.attempts += 1
            sent += 1
        else:
            break
    await session.commit()
    return sent


# Groesse eines Stuecks Brother-Daten je Nachricht. Base64 macht daraus ~11 KB,
# die Firmware nimmt hoechstens MAX_MESSAGE_BYTES (14 KB) an.
USB_STUECK = 8192


async def _usb_daten(session: AsyncSession, job: PrintJob, cfg: dict,
                     printer_cfg: dict) -> bytes | str:
    """Brother-Raster fuer einen Terminal-Auftrag - oder warum es keins gibt.

    Terminal-Auftraege tragen die ESC/POS-Bloecke, nicht den Artikel. Fuer den
    Brother am USB-Anschluss wird deshalb aus dem Artikel hinter dem Etikett
    neu gezeichnet, genau wie beim WLAN-Printserver (brother.auftrag_bauen).
    """
    import asyncio

    item = (await session.execute(
        select(InventoryItem).where(InventoryItem.label == job.label))).scalars().first()
    if item is not None:
        payload = brother.auftrag({c.name: getattr(item, c.name) for c in InventoryItem.__table__.columns})
    elif (job.label or "").upper() == "TEST":
        payload = {"test": True}
    else:
        return "Messstreifen und ESC/POS-Tests gehen nur an einen seriellen Drucker"
    try:
        return await asyncio.to_thread(brother.auftrag_bauen, payload, cfg, printer_cfg)
    except Exception as e:    # Zeichnen darf den Druckweg nie anhalten
        log.exception("Brother-Etikett %s laesst sich nicht zeichnen", job.label)
        return f"Etikett nicht darstellbar: {e}"


async def _usb_senden(session: AsyncSession, conn, device_id: str, jobs: list[PrintJob]) -> int:
    """Auftraege an ein Terminal mit Brother am USB-Anschluss: Raster in Stuecken.

    Die Firmware schreibt jedes Stueck sofort auf den USB-Anschluss und meldet
    nach dem letzten ein print_result wie beim seriellen Drucker.
    """
    import base64

    kann = session_for(device_id).caps
    if kann is None:            # hello steht noch aus
        return 0
    cfg = await settings_store.get(session, "brother", {}) or {}
    printer_cfg = await settings_store.get(session, "printer", {}) or {}
    sent = 0
    for job in jobs:
        if job.attempts >= 5:
            job.status, job.error = "failed", job.error or "zu viele Versuche"
            continue
        if proto.USB_DRUCK not in kann:
            job.status = "failed"
            job.error = ("Firmware des Terminals kann noch nicht per USB drucken - "
                         "unter Terminals ein Firmware-Update einspielen")
            continue
        daten = await _usb_daten(session, job, cfg, printer_cfg)
        if isinstance(daten, str):
            job.status, job.error = "failed", daten[:255]
            continue
        stuecke = [daten[i:i + USB_STUECK] for i in range(0, len(daten), USB_STUECK)] or [b""]
        ok = True
        for nr, stueck in enumerate(stuecke):
            nachricht = proto.usb_druck(job.id, nr, nr == len(stuecke) - 1,
                                        base64.b64encode(stueck).decode("ascii"))
            if not await conn.send(nachricht):
                ok = False
                break
        if not ok:
            break                   # Verbindung weg - beim Reconnect neu
        job.status = "sent"
        job.attempts += 1
        job.error = ""
        sent += 1
    await session.commit()
    return sent


async def on_print_result(
    session: AsyncSession, job_id: int, ok: bool, error: str = "",
    device_id: str = "",
) -> None:
    job = await session.get(PrintJob, job_id)
    if job is None:
        return
    if ok:
        job.status = "done"
        job.error = ""
    else:
        # Zurueck in die Queue - der naechste flush versucht es erneut, bis
        # attempts erschoepft ist.
        job.status = "queued"
        job.error = error[:255]
    await session.commit()

    # Nachruecken lassen. Ohne das blieb alles liegen, was nicht in den ersten
    # Durchgang gepasst hat: ein Stapel von zehn Etiketten druckte die ersten
    # und danach nichts mehr, bis jemand etwas anderes speicherte oder das
    # Geraet neu verband. Ein fehlgeschlagener Auftrag wird hier bewusst nicht
    # sofort wiederholt - sonst dreht sich ein Drucker ohne Papier im Kreis.
    if ok and device_id:
        await flush_print_queue(session, device_id)


# ---------------------------------------------------------------------------
# Offline-Betrieb
# ---------------------------------------------------------------------------
async def _offline_offen(session: AsyncSession, sess: DeviceSession) -> list[OfflineScan]:
    return list((await session.execute(
        select(OfflineScan).where(OfflineScan.device_id == sess.device_id,
                                  OfflineScan.done.is_(False))
        .order_by(OfflineScan.scanned_at, OfflineScan.id)
    )).scalars().all())


async def on_offline(session: AsyncSession, sess: DeviceSession, items: list) -> dict:
    """Was das Terminal ohne Verbindung gesammelt hat, nachholen.

    Etiketten werden ausgelagert - aber nur ausgelagert: ein zweiter Scan
    desselben Etiketts bucht offline *nicht* wieder ein. Ohne Rueckmeldung
    am Bildschirm hat man im Zweifel einfach zweimal gescannt.
    Produkt-Scans brauchen MHD, Menge und ein Etikett mit Nummer; sie warten
    unter "Nachtragen". Gibt die Zaehler fuer die Meldung zurueck.
    """
    jetzt = utcnow()
    zaehler = {"ausgelagert": 0, "nachtragen": 0, "unbekannt": 0, "doppelt": 0}
    for eintrag in items if isinstance(items, list) else []:
        if not isinstance(eintrag, dict):
            continue
        code = str(eintrag.get("code", "")).strip()[:64]
        if not code:
            continue
        try:
            alter = int(eintrag.get("age_s", -1))
        except (TypeError, ValueError):
            alter = -1
        zeitpunkt = jetzt - timedelta(seconds=alter) if 0 <= alter < 30 * 86400 else jetzt
        ort = str(eintrag.get("loc", "") or "")[:100]

        if is_label(code):
            item = await inv.find_active_by_label(session, code)
            if item is not None:
                await inv.remove_item(session, item, reason="offline", device_id=sess.device_id,
                                      household=sess.household)
                zaehler["ausgelagert"] += 1
            elif await inv.find_by_label(session, code) is not None:
                zaehler["doppelt"] += 1
            else:
                zaehler["unbekannt"] += 1
        else:
            session.add(OfflineScan(device_id=sess.device_id, household=sess.household,
                                    code=code, location=ort, scanned_at=zeitpunkt))
            zaehler["nachtragen"] += 1
    await session.commit()
    if zaehler["ausgelagert"]:
        await hub.notify_ui("inventory")

    teile = []
    if zaehler["ausgelagert"]:
        teile.append(f"{zaehler['ausgelagert']} ausgelagert")
    if zaehler["nachtragen"]:
        teile.append(f"{zaehler['nachtragen']} nachtragen")
    if zaehler["unbekannt"]:
        teile.append(f"{zaehler['unbekannt']} Etikett(en) unbekannt")
    if teile:
        await hub.send_to(sess.device_id, proto.toast(
            "Offline: " + " · ".join(teile), "warn" if zaehler["unbekannt"] else "success"))
    log.info("[%s] Offline-Abgleich: %s", sess.device_id, zaehler)
    return zaehler


async def _screen_nachtrag(session, sess, sid, status) -> dict:
    offen = await _offline_offen(session, sess)
    items = [
        {
            "id": f"off:{row.id}",
            "label": row.code,
            "sub": " · ".join(t for t in (row.location, _zeit_kurz(row.scanned_at)) if t),
            "color": "#fb8c00",
        }
        for row in offen
    ]
    if offen:
        items.append({"id": "off:verwerfen", "label": "Alle verwerfen",
                      "sub": "nichts einlagern", "color": "#f04640"})
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Nachtragen",
        subtitle="Offline gescannt - antippen zum Einlagern",
        items=items or [{"id": "none", "label": "Alles nachgetragen", "color": "#2eb048"}],
        status=status,
    )


def _zeit_kurz(stamp: datetime | None) -> str:
    if stamp is None:
        return ""
    alter = _seconds_since(stamp)
    if alter < 3600:
        return f"vor {max(1, int(alter // 60))} min"
    if alter < 86400:
        return f"vor {int(alter // 3600)} h"
    return f"vor {int(alter // 86400)} T"


async def _tap_nachtrag(session, sess, item) -> None:
    if item == "off:verwerfen":
        for row in await _offline_offen(session, sess):
            row.done = True
        await session.commit()
        sess.reset()
        return
    if not item.startswith("off:"):
        return
    try:
        row = await session.get(OfflineScan, int(item[4:]))
    except ValueError:
        return
    if row is None or row.done or row.device_id != sess.device_id:
        return
    # Dort einlagern, wo offline gescannt wurde - sofern es den Ort gibt.
    if row.location and row.location != sess.location:
        gibt_es = (await session.execute(
            select(Location.id).where(Location.name == row.location))).scalar_one_or_none()
        if gibt_es is not None:
            sess.location = row.location
            await hub.send_to(sess.device_id, proto.toast(f"Ort: {row.location}"))
    sess.erfasst()
    await _scan_barcode(session, sess, row.code)
    sess.offline_id = row.id
