"""Haushalte anlegen, umbenennen, loeschen - und was bei wem fehlt."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..device import workflow
from ..device.hub import hub
from ..models import Device, Household, InventoryItem
from ..services import haushalte

router = APIRouter(prefix="/api/households", tags=["haushalte"])


class HaushaltIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 0


async def _mit_zahlen(session: AsyncSession) -> list[dict]:
    zahlen = dict((await session.execute(
        select(InventoryItem.household, func.count())
        .where(InventoryItem.status == "active").group_by(InventoryItem.household)
    )).all())
    terminals = dict((await session.execute(
        select(Device.household, func.count()).group_by(Device.household)
    )).all())
    return [{"id": h.id, "name": h.name, "sort_order": h.sort_order,
             "artikel": zahlen.get(h.name, 0), "terminals": terminals.get(h.name, 0)}
            for h in await haushalte.alle(session)]


@router.get("")
async def liste(session: AsyncSession = Depends(get_session)):
    return await _mit_zahlen(session)


@router.get("/current")
async def aktuell(request: Request, session: AsyncSession = Depends(get_session)):
    """In welchem Haushalt die Seite steht (None = alle)."""
    name = await haushalte.aus_anfrage(request, session)
    row = (await session.execute(select(Household).where(Household.name == name))
           ).scalar_one_or_none() if name else None
    return {"id": row.id if row else None, "name": name}


@router.post("")
async def anlegen(body: HaushaltIn, session: AsyncSession = Depends(get_session)):
    name = body.name.strip()
    if await haushalte.aufloesen(session, name):
        raise HTTPException(409, "Diesen Haushalt gibt es schon")
    row = Household(name=name, sort_order=body.sort_order)
    session.add(row)
    await session.commit()
    await hub.notify_ui("settings")
    return {"id": row.id, "name": row.name}


@router.patch("/{hid}")
async def aendern(hid: int, body: HaushaltIn, session: AsyncSession = Depends(get_session)):
    row = await session.get(Household, hid)
    if row is None:
        raise HTTPException(404, "Haushalt unbekannt")
    neu = body.name.strip()
    if neu != row.name and await haushalte.aufloesen(session, neu):
        raise HTTPException(409, "Diesen Namen hat schon ein anderer Haushalt")
    alt = row.name
    row.name, row.sort_order = neu, body.sort_order
    await haushalte.umbenennen(session, alt, neu)
    await session.commit()
    workflow.haushalt_umbenannt(alt, neu)
    await hub.notify_ui("inventory")
    await hub.notify_ui("devices")
    return {"id": row.id, "name": row.name}


@router.delete("/{hid}")
async def loeschen(hid: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Household, hid)
    if row is None:
        raise HTTPException(404, "Haushalt unbekannt")
    bestand = (await session.execute(
        select(func.count()).select_from(InventoryItem)
        .where(InventoryItem.household == row.name, InventoryItem.status == "active")
    )).scalar_one()
    if bestand:
        raise HTTPException(409, f"Im Haushalt liegen noch {bestand} Artikel")
    if (await session.execute(select(func.count()).select_from(Household))).scalar_one() <= 1:
        raise HTTPException(409, "Der letzte Haushalt bleibt")
    await session.delete(row)
    await session.commit()
    await hub.notify_ui("settings")
    return {"ok": True}


@router.get("/fremd")
async def fremd(request: Request, tage: int = 30, session: AsyncSession = Depends(get_session)):
    """Fremdentnahmen der letzten Tage aus Sicht des aktuellen Haushalts."""
    name = await haushalte.aus_anfrage(request, session)
    if not name:
        return {"bei_uns": [], "von_uns": []}
    return await haushalte.fremdentnahmen(session, name, tage)
