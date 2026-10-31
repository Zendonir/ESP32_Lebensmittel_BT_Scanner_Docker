"""Was muss weg, und was wird ueberhaupt verbraucht?

Zwei Sichten auf den Bestand, beide nur lesend:

* `wochenplan()` - aktive Artikel nach Tagen bis zum MHD: Abgelaufen, Heute,
  Morgen, die uebrigen Tage dieser Woche einzeln, dann "Naechste Woche".
* `statistik()` - aus den **ausgelagerten Artikeln** (nicht aus Events: die
  verfallen nach 180 Tagen, und `inventory` ist der Zustand, siehe CLAUDE.md).
  Weggeworfen heisst: ausdruecklich als "weggeworfen" ausgelagert. "Nach dem
  MHD" ist alles andere, das erst nach seinem Datum herauskam - das ist oft,
  aber nicht immer Muell, deshalb steht es getrennt.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import InventoryItem, utcnow
from . import categories
from .dates import days_left

WEGGEWORFEN = "weggeworfen"
# Altbestand aus Version 1 wurde beim Import auf einen Schlag ausgelagert -
# das waere ein Ausreisser in jeder Zahl.
_NICHT_ZAEHLEN = ("import",)
_WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def _name(item: InventoryItem) -> str:
    return categories.display_name(item.name, item.subcategory)


def _eimer(tage: int, heute: date) -> tuple[str, str]:
    """(Schluessel, Ueberschrift) fuer einen Artikel mit `tage` bis zum MHD."""
    if tage < 0:
        return "abgelaufen", "Abgelaufen"
    if tage == 0:
        return "heute", "Heute"
    if tage == 1:
        return "morgen", "Morgen"
    tag = heute + timedelta(days=tage)
    # "Diese Woche" heisst bis Sonntag - der Rest bis zwei Wochen ist
    # "Naechste Woche", danach ist es kein Plan mehr, sondern Bestand.
    bis_sonntag = 6 - heute.weekday()
    if tage <= bis_sonntag:
        return f"tag-{tage}", f"{_WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.}"
    return "naechste", "Nächste Woche"


async def wochenplan(session: AsyncSession, haushalt: str | None = None) -> list[dict]:
    """Aktive Artikel bis Ende naechster Woche, nach Tagen gruppiert.

    Ergebnis: [{"key", "titel", "items": [...]}] in zeitlicher Folge, leere
    Gruppen fehlen. Die Artikel kommen wie bei `GET /api/inventory`
    (`with_days_left`)."""
    from .inventory import with_days_left

    heute = date.today()
    # Ende naechster Woche: dieser Sonntag + 7.
    ende = heute + timedelta(days=6 - heute.weekday() + 7)
    stmt = (select(InventoryItem)
            .where(InventoryItem.status == "active", InventoryItem.expiry_date != "",
                   InventoryItem.expiry_date <= ende.isoformat())
            .order_by(InventoryItem.expiry_date, InventoryItem.name))
    if haushalt:
        stmt = stmt.where(InventoryItem.household == haushalt)
    gruppen: dict[str, dict] = {}
    for item in (await session.execute(stmt)).scalars().all():
        tage = days_left(item.expiry_date)
        if tage is None:
            continue
        key, titel = _eimer(tage, heute)
        gruppen.setdefault(key, {"key": key, "titel": titel, "items": []})["items"].append(
            with_days_left(item))
    return list(gruppen.values())


def _tag(ts: datetime | None) -> date | None:
    return ts.date() if ts else None


def _iso_tag(iso: str) -> date | None:
    try:
        return date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


async def statistik(session: AsyncSession, haushalt: str | None = None,
                    tage: int = 180) -> dict:
    """Verbrauch der letzten `tage` Tage aus den ausgelagerten Artikeln."""
    seit = utcnow() - timedelta(days=tage)
    stmt = select(InventoryItem).where(
        InventoryItem.status == "removed", InventoryItem.removed_at.is_not(None),
        InventoryItem.removed_at >= seit,
        InventoryItem.removed_reason.not_in(_NICHT_ZAEHLEN))
    if haushalt:
        stmt = stmt.where(InventoryItem.household == haushalt)
    items = (await session.execute(stmt)).scalars().all()

    weg = nach_mhd = rechtzeitig = 0
    dauer: list[int] = []
    alle: Counter[str] = Counter()
    verbraucht: Counter[str] = Counter()     # ohne ausdruecklich Weggeworfenes
    verloren: Counter[str] = Counter()
    for item in items:
        raus = _tag(item.removed_at)
        mhd = _iso_tag(item.expiry_date)
        name = _name(item)
        alle[name] += 1
        if item.removed_reason == WEGGEWORFEN:
            weg += 1
            verloren[name] += 1
        else:
            verbraucht[name] += 1
            if mhd and raus and raus > mhd:
                nach_mhd += 1
                verloren[name] += 1
            else:
                rechtzeitig += 1
        rein = _iso_tag(item.added_date)
        if rein and raus and raus >= rein:
            dauer.append((raus - rein).days)

    # Eingelagert je Monat aus added_date (alle Zustaende), entnommen je Monat
    # aus removed_at - beide ab demselben Stichtag und fuer jeden Monat, den
    # der Zeitraum beruehrt (30 Tage am 1. November = Oktober und November).
    ab = seit.date()
    monate: list[str] = []
    m = date.today().replace(day=1)
    while m >= ab.replace(day=1):
        monate.insert(0, m.strftime("%Y-%m"))
        m = (m - timedelta(days=1)).replace(day=1)
    rein_stmt = select(InventoryItem.added_date).where(
        InventoryItem.added_date >= ab.isoformat())
    if haushalt:
        rein_stmt = rein_stmt.where(InventoryItem.household == haushalt)
    rein_je = Counter(d[:7] for d in (await session.execute(rein_stmt)).scalars().all() if d)
    raus_je = Counter(i.removed_at.strftime("%Y-%m") for i in items if i.removed_at)

    gesamt = len(items)
    return {
        "tage": tage,
        "entnommen": gesamt,
        "rechtzeitig": rechtzeitig,
        "nach_mhd": nach_mhd,
        "weggeworfen": weg,
        # Anteil, der nicht rechtzeitig verbraucht wurde - die eine Zahl, an
        # der man sieht, ob es besser wird.
        "verlust_prozent": round(100 * (weg + nach_mhd) / gesamt) if gesamt else 0,
        "lagerdauer_tage": round(median(dauer)) if dauer else None,
        # Weggeworfenes ist nicht verbraucht - es zaehlt nur unten mit.
        "oft_verbraucht": [{"name": n, "anzahl": a} for n, a in verbraucht.most_common(5)],
        # Was immer wieder verfaellt: absolut, dazu wie oft es ueberhaupt
        # herauskam ("3 von 4") - einmal von einmal ist kein Muster.
        "oft_verloren": [{"name": n, "anzahl": a, "von": alle[n]}
                         for n, a in verloren.most_common(5)],
        "monate": [{"monat": mo, "eingelagert": rein_je.get(mo, 0),
                    "entnommen": raus_je.get(mo, 0)} for mo in monate],
    }
