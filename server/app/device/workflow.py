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
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Category, Device, InventoryItem, Location, PrintJob, Template
from ..services import inventory as inv
from ..services import firmware, labels, openfoodfacts, settings_store
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
LOCATION_NEW = "location_new"
INVENTORY = "inventory"
SYSTEM = "system"
INV_SEARCH = "inv_search"
ROLL_NEW = "roll_new"

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
    remove_mode: bool = False
    location: str = ""
    screen_id: int = 0
    last_result: list[str] = field(default_factory=list)
    location_draft: str = ""
    scanner: dict = field(default_factory=dict)
    online_since: datetime = field(default_factory=datetime.utcnow)
    inv_sort: str = "mhd"
    inv_search: str = ""
    roll_size_draft: str = ""

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


_sessions: dict[str, DeviceSession] = {}


def session_for(device_id: str) -> DeviceSession:
    sess = _sessions.get(device_id)
    if sess is None:
        sess = DeviceSession(device_id=device_id)
        _sessions[device_id] = sess
    return sess


def drop_session(device_id: str) -> None:
    _sessions.pop(device_id, None)


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------
async def _status_bar(session: AsyncSession, sess: DeviceSession) -> dict:
    counts = await inv.stats(session)
    return {
        "location": sess.location,
        "mode": "remove" if sess.remove_mode else "store",
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
    if name == LOCATION_NEW:
        return _screen_location_new(sess, sid, status)
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

    sess.reset()
    return await _screen_home(session, sess, sid, status)


async def _screen_home(session, sess, sid, status) -> dict:
    counts = await inv.stats(session)
    roll = await labels.roll_state(session)
    items = [
        {"id": "templates", "label": "Kategorie", "sub": "ohne Barcode", "color": "#4c9eff"},
        {"id": "manual_entry", "label": "Manuelle Eingabe", "color": "#2eb048"},
        {"id": "inventory", "label": "Inventar", "color": "#cc9218"},
        {"id": "system", "label": "System", "color": "#1c222a"},
    ]
    stats = [
        {"label": "Produkte", "value": counts["total"], "color": "#4c9eff"},
        {"label": "Ablaufend", "value": counts["expiring"], "color": "#cc9218"},
        {"label": "Kritisch", "value": counts["expired"], "color": "#f04640"},
        {"label": "Label-Rest", "value": max(0, roll["remaining"]), "color": "#2eb048"},
    ]
    subtitle = (
        "Barcode scannen zum Auslagern"
        if sess.remove_mode
        else "Barcode scannen zum Einlagern"
    )
    return proto.screen(
        screen_id=sid,
        kind="home",
        title="HOME",
        subtitle=subtitle,
        items=items,
        meta={
            "stats": stats,
            "wifi": True,
            "ble": bool(status["scanner"]),
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
            "sub": "aktiv" if row.name == sess.location else "",
            "color": "#2eb048" if row.name == sess.location else "#1c222a",
        }
        for row in rows
    ]
    items.append({"id": "new", "label": "+ Neuer Ort", "color": "#4c9eff"})
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Lagerort waehlen",
        items=items,
        buttons=[proto.BTN_BACK],
        status=status,
    )


def _screen_location_new(sess, sid, status) -> dict:
    return proto.screen(
        screen_id=sid,
        kind="keyboard",
        title="Neuer Lagerort",
        subtitle="Name eingeben",
        value="",
        meta={"max_len": 40},
        buttons=[proto.BTN_BACK],
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
                "label": row.name[:28],
                "sub": f"{sub} - {row.location}" if row.location else sub,
                "color": color,
            }
        )
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Ablaufend",
        subtitle="Antippen = auslagern",
        items=items or [{"id": "none", "label": "Nichts laeuft ab", "color": "#2eb048"}],
        buttons=[proto.BTN_BACK],
        status=status,
    )


def _screen_unknown(sess, sid, status) -> dict:
    return proto.screen(
        screen_id=sid,
        kind="tiles",
        title="Unbekannter Barcode",
        subtitle=sess.draft.barcode,
        lines=["Kein Produkt gefunden.", "Ueber eine Vorlage anlegen oder verwerfen."],
        items=[
            {"id": "templates", "label": "Vorlage", "color": "#4c9eff"},
            {"id": "name", "label": "Namen eingeben", "color": "#4c9eff"},
            {"id": "generic", "label": "Ohne Namen", "sub": "nur MHD", "color": "#1c222a"},
            {"id": "home", "label": "Verwerfen", "color": "#f04640"},
        ],
        buttons=[proto.BTN_HOME],
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
        buttons=[proto.BTN_BACK],
        status=status,
    )


def _screen_date(sess, sid, status) -> dict:
    draft = sess.draft
    lines = [draft.name or "(ohne Namen)"]
    if draft.brand:
        lines.append(draft.brand)
    if draft.barcode:
        lines.append(draft.barcode)
    presets = [
        {"id": "p:3", "label": "+3 T"},
        {"id": "p:7", "label": "+1 W"},
        {"id": "p:30", "label": "+1 M"},
        {"id": "p:180", "label": "+6 M"},
        {"id": "p:365", "label": "+1 J"},
        {"id": "p:0", "label": "Kein MHD"},
    ]
    return proto.screen(
        screen_id=sid,
        kind="date",
        title="Mindesthaltbarkeit",
        subtitle=to_display(draft.expiry_date) or "kein Datum",
        lines=lines,
        value=draft.expiry_date,
        meta={"presets": presets},
        buttons=[proto.BTN_BACK, proto.BTN_OK],
        status=status,
    )


def _screen_qty(sess, sid, status) -> dict:
    draft = sess.draft
    return proto.screen(
        screen_id=sid,
        kind="number",
        title="Anzahl Etiketten",
        subtitle=draft.name or draft.barcode,
        lines=[f"MHD {to_display(draft.expiry_date) or '-'}", f"Ort {sess.location or '-'}"],
        value=draft.count,
        meta={"min": 1, "max": 20, "step": 1, "unit": "Stk"},
        buttons=[proto.BTN_BACK, {"id": "ok", "label": "Drucken", "style": "primary"}],
        status=status,
    )


def _screen_result(sess, sid, status) -> dict:
    return proto.screen(
        screen_id=sid,
        kind="message",
        title="Gespeichert",
        subtitle=sess.draft.name,
        lines=sess.last_result,
        items=[{"id": "home", "label": "Weiter", "color": "#2eb048"}],
        buttons=[proto.BTN_HOME],
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
        buttons=[proto.BTN_BACK, proto.BTN_HOME],
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
        buttons=[proto.BTN_BACK, proto.BTN_HOME],
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
        buttons=[proto.BTN_BACK, proto.BTN_HOME],
        status=status,
    )


async def _screen_tmpl_sorte(session, sess, sid, status) -> dict:
    tpl = await session.get(Template, sess.draft.template_id)
    sorten = list(tpl.sorten or []) if tpl else []
    items = [{"id": f"sorte:{s}", "label": s, "color": "#4c9eff"} for s in sorten]
    items.append({"id": "sorte:", "label": "Ohne Sorte", "color": "#1c222a"})
    return proto.screen(
        screen_id=sid,
        kind="list",
        title="Sorte",
        subtitle=sess.draft.name,
        lines=["Neue Sorten im Web-Interface anlegen"],
        items=items,
        buttons=[proto.BTN_BACK, proto.BTN_HOME],
        status=status,
    )


def _screen_tmpl_amount(sess, sid, status) -> dict:
    draft = sess.draft
    return proto.screen(
        screen_id=sid,
        kind="number",
        title="Fuellmenge",
        subtitle=f"{draft.name} {draft.brand}".strip(),
        value=draft.quantity,
        meta={"min": 0, "max": 5000, "step": 50 if draft.unit in ("g", "ml") else 1,
              "unit": draft.unit or "Stk"},
        buttons=[proto.BTN_BACK, proto.BTN_OK],
        status=status,
    )


async def _screen_inventory(session, sess, sid, status) -> dict:
    rows = (
        await session.execute(select(InventoryItem).where(InventoryItem.status == "active"))
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
        qty = f"{g['qty']:g} {g['unit']}".strip() if g["unit"] else f"{int(g['qty'])}x"
        parts = [g["category"] or "-"]
        if g["location"]:
            parts.append(g["location"])
        if g["expiry"]:
            parts.append(to_display(g["expiry"]))
        parts.append(qty)
        items.append(
            {
                "id": f"item:{g['label']}",
                "label": g["name"][:28],
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
        buttons=[
            proto.BTN_BACK,
            {"id": "sort", "label": "Sortierung", "style": "ghost"},
            {"id": "search", "label": "Suche", "style": "ghost"},
        ],
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
        buttons=[proto.BTN_BACK, proto.BTN_OK],
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
            "status": "Verbunden" if ble_connected else "Getrennt",
            "status_color": "#2eb048" if ble_connected else "#cc9218",
            "lines": [scanner.get("name") or "kein Geraet gekoppelt"],
            "button": {"id": "__local_ble_toggle", "label": "Verbinden / Trennen", "color": "#4c9eff"},
        },
        {
            "title": "GERAET",
            "title_color": "#4c9eff",
            "status": (device.name if device else "") or "Terminal",
            "status_color": "#e6edf3",
            "lines": [
                f"FW: {(device.firmware if device else '') or '-'}  ·  "
                f"{tel.get('res', '?')}  ·  {tel.get('flash_mb', '?')} MB Flash",
            ],
            "button": {"id": "firmware_update", "label": "Firmware Update", "color": "#2eb048"},
        },
        {
            "title": "SYSTEM",
            "title_color": "#1c222a",
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
        title="SYSTEM",
        subtitle=sess.location or "",
        meta={"cards": cards},
        buttons=[proto.BTN_BACK],
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
        buttons=[proto.BTN_BACK, proto.BTN_OK],
        status=status,
    )


# ---------------------------------------------------------------------------
# Ereignisse
# ---------------------------------------------------------------------------
async def push_screen(session: AsyncSession, sess: DeviceSession) -> None:
    await hub.send_to(sess.device_id, await render(session, sess))


async def on_connect(session: AsyncSession, sess: DeviceSession, device: Device) -> None:
    sess.location = device.active_location or await inv.default_location(session)
    sess.reset()
    device_cfg = await settings_store.get(session, "device_ui", {})
    await hub.send_to(sess.device_id, proto.config(device_cfg))
    await push_screen(session, sess)
    await flush_print_queue(session, sess.device_id)


async def on_tap(session: AsyncSession, sess: DeviceSession, item: str) -> None:
    """Ein Tipp auf eine Kachel, eine Listenzeile oder einen Fussleisten-Knopf."""
    if item == "home":
        sess.reset()
        return await push_screen(session, sess)
    if item == "back":
        sess.pop()
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
        LOCATION_NEW: _tap_location_new,
        ENTER_DATE: _tap_date,
        ENTER_QTY: _tap_confirm_save,
        RESULT: _tap_result,
        TMPL_CATEGORY: _tap_tmpl_category,
        TMPL_PRODUCT: _tap_tmpl_product,
        TMPL_BRAND: _tap_tmpl_brand,
        TMPL_SORTE: _tap_tmpl_sorte,
        TMPL_AMOUNT: _tap_confirm_save,
        INVENTORY: _tap_inventory,
        INV_SEARCH: _tap_inv_search,
        SYSTEM: _tap_system,
        ROLL_NEW: _tap_roll_new,
    }.get(sess.current)

    if handler is not None:
        await handler(session, sess, item)
    await push_screen(session, sess)


async def _tap_home(session, sess, item) -> None:
    if item == "mode":
        sess.remove_mode = not sess.remove_mode
        await hub.send_to(sess.device_id, proto.beep("warn" if sess.remove_mode else "ok"))
    elif item == "templates":
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


async def _tap_inventory(session, sess, item) -> None:
    if item == "sort":
        idx = INV_SORT_MODES.index(sess.inv_sort) if sess.inv_sort in INV_SORT_MODES else 0
        sess.inv_sort = INV_SORT_MODES[(idx + 1) % len(INV_SORT_MODES)]
    elif item == "search":
        sess.push(INV_SEARCH)
    elif item.startswith("item:"):
        label = item[5:]
        row = await inv.find_active_by_label(session, label)
        if row is None:
            await hub.send_to(sess.device_id, proto.toast("Schon ausgelagert", "warn"))
            return
        await inv.remove_item(session, row, reason="device_list", device_id=sess.device_id)
        await session.commit()
        await hub.send_to(sess.device_id, proto.beep("ok"))
        await hub.send_to(sess.device_id, proto.toast(f"{row.name} ausgelagert"))
        await hub.notify_ui("inventory")


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
        await hub.send_to(sess.device_id, proto.toast("Ungueltige Groesse", "warn"))
        return
    await labels.new_roll(session, size)
    sess.pop()
    await hub.send_to(sess.device_id, proto.toast(f"Neue Rolle: {size} Etiketten", "success"))


async def _tap_locations(session, sess, item) -> None:
    if item == "new":
        sess.location_draft = ""
        sess.push(LOCATION_NEW)
        return
    if not item.startswith("loc:"):
        return
    sess.location = item[4:]
    device = (
        await session.execute(select(Device).where(Device.device_id == sess.device_id))
    ).scalar_one_or_none()
    if device is not None:
        device.active_location = sess.location
        await session.commit()
    sess.pop()
    await hub.send_to(sess.device_id, proto.toast(f"Ort: {sess.location}"))


async def _tap_location_new(session, sess, item) -> None:
    if item != "ok":
        return
    name = sess.location_draft.strip()
    if not name:
        await hub.send_to(sess.device_id, proto.toast("Name fehlt", "warn"))
        return

    existing = (
        await session.execute(select(Location).where(Location.name == name))
    ).scalar_one_or_none()
    if existing is None:
        max_order = (
            await session.execute(
                select(Location.sort_order).order_by(Location.sort_order.desc()).limit(1)
            )
        ).scalar_one_or_none()
        session.add(Location(name=name, sort_order=(max_order or 0) + 1))
        await session.flush()

    sess.location = name
    device = (
        await session.execute(select(Device).where(Device.device_id == sess.device_id))
    ).scalar_one_or_none()
    if device is not None:
        device.active_location = name
    await session.commit()
    await hub.notify_ui("catalog")

    sess.pop()
    await hub.send_to(sess.device_id, proto.toast(f"Ort: {name}", "success"))


async def _tap_expiring(session, sess, item) -> None:
    if not item.startswith("item:"):
        return
    label = item[5:]
    row = await inv.find_active_by_label(session, label)
    if row is None:
        await hub.send_to(sess.device_id, proto.toast("Schon ausgelagert", "warn"))
        return
    await inv.remove_item(session, row, reason="device_list", device_id=sess.device_id)
    await session.commit()
    await hub.send_to(sess.device_id, proto.beep("ok"))
    await hub.send_to(sess.device_id, proto.toast(f"{row.name} ausgelagert"))
    await hub.notify_ui("inventory")


async def _tap_unknown(session, sess, item) -> None:
    if item == "templates":
        sess.stack = [HOME, TMPL_CATEGORY]
    elif item == "name":
        sess.push(UNKNOWN_NAME)
    elif item == "generic":
        sess.draft.name = sess.draft.name or f"Artikel {sess.draft.barcode[-4:]}"
        sess.stack = [HOME, ENTER_DATE]


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
    elif item == "ok":
        sess.push(ENTER_QTY)


async def _tap_result(session, sess, item) -> None:
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
    draft = sess.draft
    draft.template_id = tpl.id
    draft.name = tpl.name
    draft.category = tpl.category
    draft.unit = tpl.unit
    draft.expiry_date = shift_iso(tpl.shelf_days) if tpl.shelf_days else ""
    draft.quantity = 1.0

    if tpl.brands:
        sess.push(TMPL_BRAND)
    elif tpl.use_sorten:
        sess.push(TMPL_SORTE)
    else:
        sess.push(TMPL_AMOUNT if tpl.unit else ENTER_DATE)


async def _tap_tmpl_brand(session, sess, item) -> None:
    if not item.startswith("brand:"):
        return
    sess.draft.brand = item[6:]
    tpl = await session.get(Template, sess.draft.template_id)
    if tpl is not None and tpl.use_sorten:
        sess.push(TMPL_SORTE)
    else:
        sess.push(TMPL_AMOUNT if sess.draft.unit else ENTER_DATE)


async def _tap_tmpl_sorte(session, sess, item) -> None:
    if not item.startswith("sorte:"):
        return
    sess.draft.subcategory = item[6:]
    sess.push(TMPL_AMOUNT if sess.draft.unit else ENTER_DATE)


async def _tap_confirm_save(session, sess, item) -> None:
    if item != "ok":
        return
    if sess.current == TMPL_AMOUNT:
        sess.push(ENTER_DATE)
        return
    await _save_draft(session, sess)


async def on_input(session: AsyncSession, sess: DeviceSession, value) -> None:
    """Wert aus einem date/number/text-Bildschirm uebernehmen."""
    current = sess.current
    if current == ENTER_DATE:
        sess.draft.expiry_date = to_iso_date(value)
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
    elif current == LOCATION_NEW:
        sess.location_draft = str(value or "").strip()
    elif current == INV_SEARCH:
        sess.inv_search = str(value or "").strip()
    elif current == ROLL_NEW:
        sess.roll_size_draft = str(value or "")
    await push_screen(session, sess)


async def _save_draft(session: AsyncSession, sess: DeviceSession) -> None:
    draft = sess.draft
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
    )
    await session.commit()

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
def _is_label(code: str) -> bool:
    return code.upper().startswith(settings.label_prefix.upper()) and code[
        len(settings.label_prefix):
    ].isdigit()


async def on_scan(session: AsyncSession, sess: DeviceSession, code: str) -> None:
    code = code.strip()
    if not code:
        return
    log.info("[%s] Scan: %s (Modus %s)", sess.device_id, code, sess.current)

    if _is_label(code):
        await _scan_label(session, sess, code)
    else:
        await _scan_barcode(session, sess, code)
    await push_screen(session, sess)


async def _scan_label(session, sess, code) -> None:
    """Ein eigenes Etikett wurde gescannt."""
    item = await inv.find_active_by_label(session, code)
    if item is not None:
        await inv.remove_item(session, item, reason="scan", device_id=sess.device_id)
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
            sess.device_id, proto.toast(f"{restored.name} zurueckgebucht", "success")
        )
        await hub.notify_ui("inventory")
        sess.reset()
        return

    await hub.send_to(sess.device_id, proto.beep("error"))
    await hub.send_to(sess.device_id, proto.toast(f"{code} unbekannt", "error"))


async def _scan_barcode(session, sess, code) -> None:
    """Ein Produkt-Barcode wurde gescannt."""
    if sess.remove_mode:
        item = await inv.find_removable_by_barcode(session, code)
        if item is None:
            await hub.send_to(sess.device_id, proto.beep("error"))
            await hub.send_to(sess.device_id, proto.toast("Nicht im Bestand", "error"))
            return
        await inv.remove_item(session, item, reason="scan", device_id=sess.device_id)
        await session.commit()
        await hub.send_to(sess.device_id, proto.beep("ok"))
        await hub.send_to(sess.device_id, proto.toast(f"{item.name} ausgelagert"))
        await hub.notify_ui("inventory")
        return

    await hub.send_to(sess.device_id, proto.beep("scan"))
    product = await openfoodfacts.lookup(session, code)
    await session.commit()

    draft = Draft(barcode=code)
    if product is not None and product.name:
        draft.name = product.name
        draft.brand = product.brand
        draft.category = product.category
        draft.expiry_date = (
            shift_iso(product.default_shelf_days) if product.default_shelf_days else ""
        )
        sess.draft = draft
        sess.stack = [HOME, ENTER_DATE]
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
async def flush_print_queue(session: AsyncSession, device_id: str) -> int:
    """Offene Druckauftraege an ein Geraet senden.

    Wird sowohl nach dem Speichern als auch nach jedem Reconnect aufgerufen -
    ein Etikett, das waehrend eines Neustarts entstanden ist, geht damit nicht
    verloren.
    """
    conn = hub.get(device_id)
    if conn is None:
        return 0
    jobs = (
        await session.execute(
            select(PrintJob)
            .where(PrintJob.status.in_(("queued", "sent")))
            .order_by(PrintJob.id)
            .limit(20)
        )
    ).scalars().all()

    sent = 0
    for job in jobs:
        if job.attempts >= 5:
            job.status = "failed"
            job.error = job.error or "zu viele Versuche"
            continue
        if await conn.send(proto.print_job(job.id, job.payload)):
            job.status = "sent"
            job.attempts += 1
            sent += 1
        else:
            break
    await session.commit()
    return sent


async def on_print_result(
    session: AsyncSession, job_id: int, ok: bool, error: str = ""
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
