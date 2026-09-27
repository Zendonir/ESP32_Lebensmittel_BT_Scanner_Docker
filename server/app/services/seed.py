"""Erstbefuellung mit sinnvollen Voreinstellungen.

Laeuft nur, wenn die jeweilige Tabelle leer ist - ein Neustart ueberschreibt
also nichts.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Category, Location, Template
from . import inventory as inv

log = logging.getLogger(__name__)

CATEGORIES = [
    ("Getränke", "#1e88e5"),
    ("Milchprodukte", "#00acc1"),
    ("Fleisch & Fisch", "#e53935"),
    ("Obst & Gemüse", "#43a047"),
    ("Backwaren", "#8d6e63"),
    ("Tiefkühl", "#5e35b1"),
    ("Konserven", "#fb8c00"),
    ("Trockenware", "#fdd835"),
    ("Süßes & Snacks", "#d81b60"),
    ("Sonstiges", "#546e7a"),
]

LOCATIONS = [
    ("Kühlschrank", True),
    ("Gefrierschrank", False),
    ("Vorratskammer", False),
    ("Keller", False),
]

TEMPLATES = [
    # (Name, Kategorie, Haltbarkeit Tage, Einheit, Sorten nutzen)
    ("Aufschnitt", "Fleisch & Fisch", 5, "g", False),
    ("Hackfleisch", "Fleisch & Fisch", 2, "g", False),
    ("Suppe", "Konserven", 4, "ml", True),
    ("Eingekochtes", "Konserven", 365, "ml", True),
    ("Gebäck", "Backwaren", 3, "", False),
    ("Restessen", "Sonstiges", 3, "", True),
]


async def _empty(session: AsyncSession, model) -> bool:
    count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
    return count == 0


# Fruehere Voreinstellungen standen mit Umschrift in der Datenbank, obwohl
# Anzeige, Terminal und Drucker Umlaute koennen. Nachgezogen wird nur, was
# noch genau so heisst wie ausgeliefert und dessen Ziel es noch nicht gibt -
# was jemand bewusst umbenannt hat, bleibt unberuehrt.
UMSCHRIFTEN_KATEGORIEN = (("Getraenke", "Getränke"), ("Tiefkuehl", "Tiefkühl"))
UMSCHRIFTEN_VORLAGEN = (("Gebaeck", "Gebäck"),)


async def _umschriften_nachziehen(session: AsyncSession) -> list[str]:
    erledigt = []
    for alt, neu in UMSCHRIFTEN_KATEGORIEN:
        row = (await session.execute(select(Category).where(Category.name == alt))).scalar_one_or_none()
        schon_da = (await session.execute(select(Category).where(Category.name == neu))).scalar_one_or_none()
        if row is None or schon_da is not None:
            continue
        row.name = neu
        await inv.rename_category(session, alt, neu)
        erledigt.append(f"{alt} -> {neu}")
    for alt, neu in UMSCHRIFTEN_VORLAGEN:
        rows = (await session.execute(select(Template).where(Template.name == alt))).scalars().all()
        for row in rows:
            doppelt = (
                await session.execute(
                    select(Template).where(Template.name == neu, Template.category == row.category)
                )
            ).scalar_one_or_none()
            if doppelt is None:
                row.name = neu
                erledigt.append(f"Vorlage {alt} -> {neu}")
    return erledigt


async def run(session: AsyncSession) -> None:
    seeded = []

    umbenannt = await _umschriften_nachziehen(session)
    if umbenannt:
        await session.commit()
        log.info("Umlaute nachgezogen: %s", ", ".join(umbenannt))

    if await _empty(session, Category):
        for index, (name, color) in enumerate(CATEGORIES):
            session.add(Category(name=name, color=color, sort_order=index))
        seeded.append("Kategorien")

    if await _empty(session, Location):
        for index, (name, is_default) in enumerate(LOCATIONS):
            session.add(Location(name=name, is_default=is_default, sort_order=index))
        seeded.append("Lagerorte")

    if await _empty(session, Template):
        for index, (name, category, days, unit, sorten) in enumerate(TEMPLATES):
            session.add(
                Template(
                    name=name,
                    category=category,
                    shelf_days=days,
                    unit=unit,
                    use_sorten=sorten,
                    brands=[],
                    sorten=[],
                    sort_order=index,
                )
            )
        seeded.append("Vorlagen")

    if seeded:
        await session.commit()
        log.info("Erstbefuellung: %s", ", ".join(seeded))
