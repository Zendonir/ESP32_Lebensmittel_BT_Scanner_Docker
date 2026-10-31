"""Inventar-Endpunkte."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from ..services import gs1, haltbarkeit, haushalte, mengen, openfoodfacts, settings_store, verbrauch
from ..services.dates import shift_iso, today_iso

router = APIRouter(prefix="/api/inventory", tags=["inventar"])


@router.get("", response_model=list[InventoryOut])
async def list_inventory(
    request: Request,
    session: AsyncSession = Depends(get_session),
    status: str = Query("active", pattern="^(active|removed|all)$"),
    q: str = "",
    category: str = "",
    location: str = "",
    expiring: bool = False,
    # Nur die bald ablaufenden (ohne die abgelaufenen) bzw. nur die
    # abgelaufenen - die Kacheln der Uebersicht springen hierher.
    frist: str = Query("", pattern="^(|bald|abgelaufen)$"),
    sort: str = Query("expiry", pattern="^(expiry|name|added|location)$"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    alle: bool = False,
):
    stmt = select(InventoryItem)
    # Grundsaetzlich nur der eigene Haushalt. `alle` ist die ausdrueckliche
    # Suche in den anderen - sie kommt nur mit einem Suchbegriff, damit
    # niemand nebenbei den ganzen fremden Bestand durchblaettert.
    haushalt = await haushalte.aus_anfrage(request, session)
    if haushalt and not (alle and q.strip()):
        stmt = stmt.where(InventoryItem.household == haushalt)
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

    if frist:
        heute = today_iso()
        if frist == "abgelaufen":
            stmt = stmt.where(InventoryItem.expiry_date != "", InventoryItem.expiry_date < heute)
        else:
            ui_cfg = await settings_store.get(session, "ui", {})
            horizon = (
                date.today() + timedelta(days=int(ui_cfg.get("expiring_days", 7)))
            ).isoformat()
            stmt = stmt.where(InventoryItem.expiry_date >= heute,
                              InventoryItem.expiry_date <= horizon)

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
async def get_stats(request: Request, session: AsyncSession = Depends(get_session)):
    return await inv.stats(session, await haushalte.aus_anfrage(request, session))


@router.get("/wochenplan")
async def get_wochenplan(request: Request, session: AsyncSession = Depends(get_session)):
    """Was muss weg? Aktive Artikel bis Ende naechster Woche, nach Tagen."""
    return await verbrauch.wochenplan(session, await haushalte.aus_anfrage(request, session))


@router.get("/verbrauch")
async def get_verbrauch(
    request: Request,
    session: AsyncSession = Depends(get_session),
    tage: int = Query(180, ge=7, le=366),
):
    """Verbrauchsstatistik aus den ausgelagerten Artikeln."""
    return await verbrauch.statistik(session, await haushalte.aus_anfrage(request, session), tage)


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
    # Umlagern in den Gefrierschrank verlaengert das MHD (frische Ware) - wer
    # das Datum im selben Zug selbst aendert, behaelt seins. Der Bearbeiten-
    # Dialog schickt das unveraenderte Datum immer mit; das zaehlt nicht.
    einfrieren = ("location" in changes
                  and changes.get("expiry_date", row.expiry_date) == row.expiry_date
                  and changes["location"] != row.location
                  and await haltbarkeit.art_von_ort(session, row.location) != "tiefkuehl")
    for key, value in changes.items():
        setattr(row, key, value)
    if einfrieren:
        neu = await haltbarkeit.eingefroren(
            session, mhd=row.expiry_date, ort=row.location, kategorie=row.category,
            produkt=await inv.product_for_barcode(session, row.barcode) if row.barcode else None)
        if neu["verlaengert"]:
            row.expiry_date = changes["expiry_date"] = neu["datum"]
    row.quantity, row.unit = mengen.normalisieren(row.quantity, row.unit)
    await inv.log_event(
        session, "edit", label=row.label, name=row.name, payload=changes
    )
    await session.commit()
    await hub.notify_ui("inventory")
    return inv.with_days_left(row)


@router.post("/remove", response_model=InventoryOut)
async def remove(req: RemoveRequest, request: Request,
                 session: AsyncSession = Depends(get_session)):
    """Auslagern - per Etikett (exakt) oder per Produkt-Barcode (aeltestes zuerst)."""
    haushalt = await haushalte.aus_anfrage(request, session)
    if req.label:
        row = await inv.find_active_by_label(session, req.label)
    elif req.barcode:
        row = await inv.find_removable_by_barcode(session, req.barcode, haushalt)
    else:
        raise HTTPException(400, "Etikett oder Barcode angeben")

    if row is None:
        raise HTTPException(404, "Kein aktiver Artikel gefunden")
    await inv.remove_item(session, row, reason=req.reason, household=haushalt)
    await session.commit()
    await hub.notify_ui("inventory")
    return inv.with_days_left(row)


@router.post("/restore", response_model=InventoryOut)
async def restore(req: RemoveRequest, session: AsyncSession = Depends(get_session)):
    if not req.label:
        raise HTTPException(400, "Etikett angeben")
    row = await inv.restore_by_label(session, req.label)
    if row is None:
        raise HTTPException(404, "Lässt sich nicht mehr zurückbuchen – das Zeitfenster ist abgelaufen")
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
async def scan(req: ScanRequest, request: Request,
               session: AsyncSession = Depends(get_session)):
    """Ein Scan von der Handy-Kamera.

    Eigenes Etikett: auslagern bzw. zurueckbuchen, sofort. Produkt-Barcode:
    nur nachschlagen - die Angaben (Stueckzahl, MHD, Ort) macht das Handy
    selbst und legt danach ueber POST /api/labels an.

    Vorher wanderte jeder Produkt-Scan in den Ablauf des ersten Terminals,
    das online war: man scannte am Handy und musste dann am Terminal
    weitertippen. Nur mit ausdruecklicher `device_id` geschieht das noch.
    """
    from ..device import workflow

    code = req.code.strip()
    sess_id = req.device_id
    if sess_id:
        dev_sess = workflow.session_for(sess_id)
        await workflow.on_scan(session, dev_sess, code)
        return {"ok": True, "routed_to": sess_id}

    # Ein eigenes Etikett gehoert nie zu OpenFoodFacts. Ohne diese Abzweigung
    # wanderte die Etikettennummer dorthin, kam erwartungsgemaess als
    # "unbekannt" zurueck - und das Abscannen eines Etiketts am Handy tat
    # nichts, obwohl der Hinweistext daneben "eigenes Etikett = auslagern"
    # verspricht. Auslagern braucht kein Terminal: der Artikel liegt schon
    # irgendwo, die Datenbank weiss wo.
    if workflow.is_label(code):
        item = await inv.find_active_by_label(session, code)
        if item is not None:
            await inv.remove_item(session, item, reason="mobil",
                                  household=await haushalte.aus_anfrage(request, session))
            await session.commit()
            await hub.notify_ui("inventory")
            return {"ok": True, "routed_to": None, "action": "removed",
                    "label": item.label, "name": item.name}

        # Derselbe Zweitscan bucht wieder ein - wie am Geraet.
        restored = await inv.restore_by_label(session, code)
        if restored is not None:
            await session.commit()
            await hub.notify_ui("inventory")
            return {"ok": True, "routed_to": None, "action": "restored",
                    "label": restored.label, "name": restored.name}

        return {"ok": False, "routed_to": None, "action": "unknown_label",
                "label": code}

    # GS1-Code (PDF417/DataMatrix): nachgeschlagen wird die EAN aus der GTIN,
    # MHD und Gewicht gehen als "gs1" an die Maske.
    gs = gs1.lesen(code)
    if gs:
        code = gs["ean"]
    product = await openfoodfacts.lookup(session, code)
    await session.commit()
    return {
        "ok": True,
        "routed_to": None,
        "code": code,
        "gs1": {k: v for k, v in gs.items() if k != "roh"} if gs else None,
        "action": "product",
        "product": None
        if product is None
        else {
            "barcode": product.barcode,
            "name": product.name,
            "brand": product.brand,
            "category": product.category,
            "subcategory": product.subcategory,
            "amount": product.amount,
            # "1l" aus der Produktdatenbank als Menge einer Packung: 1000 ml.
            **dict(zip(("menge", "einheit"), mengen.aus_angabe(product.amount) or (None, ""))),
            # Vorschlag fuers MHD, wie am Terminal (shift_iso).
            "expiry_date": shift_iso(product.default_shelf_days)
            if product.default_shelf_days else "",
            # Haken "Im Original gefroren" vorbelegen.
            "gefroren": haltbarkeit.schon_gefroren(product),
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
