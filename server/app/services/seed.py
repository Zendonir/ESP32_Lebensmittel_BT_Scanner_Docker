"""Erstbefuellung mit sinnvollen Voreinstellungen.

Laeuft nur, wenn die jeweilige Tabelle leer ist - ein Neustart ueberschreibt
also nichts.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Category, Location, Template

log = logging.getLogger(__name__)

CATEGORIES = [
    ("Getraenke", "#1e88e5"),
    ("Milchprodukte", "#00acc1"),
    ("Fleisch & Fisch", "#e53935"),
    ("Obst & Gemuese", "#43a047"),
    ("Backwaren", "#8d6e63"),
    ("Tiefkuehl", "#5e35b1"),
    ("Konserven", "#fb8c00"),
    ("Trockenware", "#fdd835"),
    ("Suesses & Snacks", "#d81b60"),
    ("Sonstiges", "#546e7a"),
]

LOCATIONS = [
    ("Kuehlschrank", True),
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
    ("Gebaeck", "Backwaren", 3, "", False),
    ("Restessen", "Sonstiges", 3, "", True),
]


async def _empty(session: AsyncSession, model) -> bool:
    count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
    return count == 0


async def run(session: AsyncSession) -> None:
    seeded = []

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
