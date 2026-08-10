"""Inventar-Endpunkte."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..device.hub import hub
from ..models import InventoryItem
from ..schemas import (
    InventoryOut,
    InventoryPatch,
    RemoveRequest,
    ScanRequest,
    Stats,
)
from ..services import inventory as inv
from ..services import openfoodfacts, settings_store
from ..services.dates import today_iso

router = APIRouter(prefix="/api/inventory", tags=["inventar"])


@router.get("", response_model=list[InventoryOut])
async def list_inventory(
    session: AsyncSession = Depends(get_session),
    status: str = Query("active", pattern="^(active|removed|all)$"),
    q: str = "",
    category: str = "",
    location: str = "",
    expiring: bool = False,
    sort: str = Query("expiry", pattern="^(expiry|name|added|location)$"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    stmt = select(InventoryItem)
    if status != "all":
        stmt = stmt.where(InventoryItem.status == status)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                InventoryItem.name.ilike(needle),
                InventoryItem.brand.ilike(needle),
                InventoryItem.barcode.ilike(needle),
                InventoryItem.label.ilike(needle),
                InventoryItem.subcategory.ilike(needle),
            )
        )
    if category:
        stmt = stmt.where(InventoryItem.category == category)
    if location:
        stmt = stmt.where(InventoryItem.location == location)
    if expiring:
        ui_cfg = await settings_store.get(session, "ui", {})
        horizon = (
            date.today() + timedelta(days=int(ui_cfg.get("expiring_days", 7)))
        ).isoformat()
        stmt = stmt.where(
            InventoryItem.expiry_date != "", InventoryItem.expiry_date <= horizon
        )

    order = {
        "expiry": (InventoryItem.expiry_date == "", InventoryItem.expiry_date.asc()),
        "name": (InventoryItem.name.asc(),),
        "added": (InventoryItem.id.desc(),),
        "location": (InventoryItem.location.asc(), InventoryItem.expiry_date.asc()),
    }[sort]
    stmt = stmt.order_by(*order).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).scalars().all()
    return [inv.with_days_left(row) for row in rows]


@router.get("/stats", response_model=Stats)
async def get_stats(session: AsyncSession = Depends(get_session)):
    return await inv.stats(session)


@router.get("/{label}", response_model=InventoryOut)
async def get_item(label: str, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(select(InventoryItem).where(InventoryItem.label == label))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Etikett unbekannt")
    return inv.with_days_left(row)


@router.patch("/{label}", response_model=InventoryOut)
async def patch_item(
    label: str, patch: InventoryPatch, session: AsyncSession = Depends(get_session)
):
    row = (
        await session.execute(select(InventoryItem).where(InventoryItem.label == label))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Etikett unbekannt")

    changes = patch.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(row, key, value)
    await inv.log_event(
        session, "edit", label=row.label, name=row.name, payload=changes
    )
    await session.commit()
    await hub.notify_ui("inventory")
    return inv.with_days_left(row)


@router.post("/remove", response_model=InventoryOut)
async def remove(req: RemoveRequest, session: AsyncSession = Depends(get_session)):
    """Auslagern - per Etikett (exakt) oder per Produkt-Barcode (aeltestes zuerst)."""
    if req.label:
        row = await inv.find_active_by_label(session, req.label)
    elif req.barcode:
        row = await inv.find_removable_by_barcode(session, req.barcode)
    else:
        raise HTTPException(400, "label oder barcode noetig")

    if row is None:
        raise HTTPException(404, "Kein aktiver Artikel gefunden")
    await inv.remove_item(session, row, reason=req.reason)
    await session.commit()
    await hub.notify_ui("inventory")
    return inv.with_days_left(row)


@router.post("/restore", response_model=InventoryOut)
async def restore(req: RemoveRequest, session: AsyncSession = Depends(get_session)):
    if not req.label:
        raise HTTPException(400, "label noetig")
    row = await inv.restore_by_label(session, req.label)
    if row is None:
        raise HTTPException(404, "Nicht rueckbuchbar (Zeitfenster abgelaufen?)")
    await session.commit()
    await hub.notify_ui("inventory")
    return inv.with_days_left(row)


@router.delete("/{label}")
async def delete_item(label: str, session: AsyncSession = Depends(get_session)):
    """Endgueltig loeschen - fuer Fehleingaben. Der Event-Log bleibt erhalten."""
    row = (
        await session.execute(select(InventoryItem).where(InventoryItem.label == label))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Etikett unbekannt")
    await inv.log_event(session, "delete", label=row.label, name=row.name)
    await session.delete(row)
    await session.commit()
    await hub.notify_ui("inventory")
    return {"ok": True}


@router.post("/scan")
async def scan(req: ScanRequest, session: AsyncSession = Depends(get_session)):
    """Denselben Scan-Pfad wie das Geraet ausloesen - fuer Handy-Kamera-Scans.

    Ist ein Geraet angegeben (oder genau eines online), wandert der Code in
    dessen Workflow; sonst wird nur nachgeschlagen.
    """
    from ..device import workflow

    code = req.code.strip()
    sess_id = req.device_id or (hub.online_ids()[0] if hub.online_ids() else None)
    if sess_id:
        dev_sess = workflow.session_for(sess_id)
        await workflow.on_scan(session, dev_sess, code)
        return {"ok": True, "routed_to": sess_id}

    product = await openfoodfacts.lookup(session, code)
    await session.commit()
    return {
        "ok": True,
        "routed_to": None,
        "product": None
        if product is None
        else {
            "barcode": product.barcode,
            "name": product.name,
            "brand": product.brand,
            "category": product.category,
        },
    }


@router.post("/compact")
async def compact(
    session: AsyncSession = Depends(get_session),
    older_than_days: int = Query(90, ge=1),
):
    """Alte, ausgelagerte Eintraege entfernen. Der Event-Log bleibt bestehen."""
    cutoff = (date.today() - timedelta(days=older_than_days)).isoformat()
    rows = (
        await session.execute(
            select(InventoryItem).where(
                InventoryItem.status == "removed",
                InventoryItem.added_date != "",
                InventoryItem.added_date < cutoff,
            )
        )
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    await session.commit()
    return {"removed": len(rows), "cutoff": cutoff, "today": today_iso()}
