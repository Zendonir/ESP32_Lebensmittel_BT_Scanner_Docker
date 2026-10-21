"""Haushalte - wer was besitzt, und wer was bei wem entnommen hat.

Ein Haushalt ist ein Name. Artikel, Terminals und Protokolleintraege tragen
ihn als Text, genau wie Kategorie und Lagerort. Die Web-Oberflaeche laeuft je
Haushalt unter `/h/<id>/` und schickt die Kennung im Kopf `X-Haushalt` mit;
ohne Kennung sieht sie alles (Verwaltung, und wie vor den Haushalten).

Grundsaetzlich sieht ein Haushalt nur seine Dinge. Wer ausdruecklich in
allen Haushalten sucht und dort etwas entnimmt, macht eine *Fremdentnahme*:
sie steht im Protokoll beim Besitzer, mit dem entnehmenden Haushalt in
`payload["fremd"]`, und erscheint dort als Hinweis.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Device, Event, Household, InventoryItem, utcnow

log = logging.getLogger(__name__)

KOPF = "X-Haushalt"


async def alle(session: AsyncSession) -> list[Household]:
    return list((await session.execute(
        select(Household).order_by(Household.sort_order, Household.name)
    )).scalars().all())


async def standard(session: AsyncSession) -> str:
    """Der Haushalt, dem ohne weitere Angabe etwas gehoert."""
    erster = (await session.execute(
        select(Household).order_by(Household.sort_order, Household.id).limit(1)
    )).scalar_one_or_none()
    return erster.name if erster else settings.household


async def aufloesen(session: AsyncSession, kennung: str | int | None) -> str | None:
    """Kennung (Nummer oder Name) -> Name. None, wenn es ihn nicht gibt."""
    if kennung in (None, ""):
        return None
    text = str(kennung).strip()
    if text.isdigit():
        row = await session.get(Household, int(text))
        if row:
            return row.name
    row = (await session.execute(
        select(Household).where(Household.name == text)
    )).scalar_one_or_none()
    return row.name if row else None


async def aus_anfrage(request: Request, session: AsyncSession) -> str | None:
    """Der Haushalt, in dem die Web-Seite gerade steht - oder None (alle)."""
    return await aufloesen(
        session, request.headers.get(KOPF) or request.query_params.get("haushalt")
    )


async def einrichten(session: AsyncSession) -> None:
    """Beim Start: ohne Haushalt geht nichts, also mindestens einen anlegen.

    Bestehende Artikel tragen schon einen Haushaltsnamen (bisher aus der
    Umgebung, HOUSEHOLD). Aus diesen Namen entstehen die ersten Haushalte -
    so gehoert der vorhandene Bestand von Anfang an jemandem.
    """
    if (await session.execute(select(func.count()).select_from(Household))).scalar_one():
        return
    namen = [n for (n,) in (await session.execute(
        select(InventoryItem.household).distinct()
    )).all() if n]
    if not namen:
        namen = [settings.household or "Zuhause"]
    for i, name in enumerate(sorted(set(namen))):
        session.add(Household(name=name, sort_order=i))
    await session.commit()
    log.info("Haushalte angelegt: %s", ", ".join(sorted(set(namen))))


async def umbenennen(session: AsyncSession, alt: str, neu: str) -> None:
    """Namen ueberall nachziehen, wo er als Text steht - ausser im Protokoll."""
    if not alt or alt == neu:
        return
    await session.execute(update(InventoryItem).where(
        InventoryItem.household == alt).values(household=neu))
    await session.execute(update(Device).where(Device.household == alt).values(household=neu))
    # Der Brother traegt seinen Haushalt in den Einstellungen.
    from . import brother, settings_store

    cfg = await settings_store.get(session, "brother", {}) or {}
    liste = brother.haushalte_von(cfg)
    if alt in liste:
        await settings_store.merge(session, "brother", {
            "haushalt": ", ".join(neu if h == alt else h for h in liste)})


async def fremdentnahmen(session: AsyncSession, haushalt: str, tage: int = 30) -> dict:
    """Was andere bei uns entnommen haben - und was wir bei anderen."""
    seit = utcnow() - timedelta(days=tage)
    rows = (await session.execute(
        select(Event).where(Event.type == "remove", Event.ts >= seit)
        .order_by(Event.id.desc()).limit(500)
    )).scalars().all()

    def eintrag(e: Event) -> dict:
        return {"ts": e.ts, "label": e.label, "name": e.name, "location": e.location,
                "besitzer": e.household, "fremd": (e.payload or {}).get("fremd", "")}

    fremd = [e for e in rows if (e.payload or {}).get("fremd")]
    return {
        "bei_uns": [eintrag(e) for e in fremd if e.household == haushalt],
        "von_uns": [eintrag(e) for e in fremd if e.payload.get("fremd") == haushalt],
    }
