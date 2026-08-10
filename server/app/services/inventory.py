"""Bestandslogik - die eine Stelle, an der sich das Inventar aendert.

Alle Eingangswege (Web-UI, REST, Geraete-Workflow) laufen hier zusammen, damit
Etikettenvergabe, Event-Log, Druckauftrag und Benachrichtigung nicht je Pfad
neu erfunden werden. Genau diese Duplizierung (`App::finishStorageWorkflow()`
vs. `POST /api/labels/create`) war im ersten Projekt eine stete Fehlerquelle.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Event, InventoryItem, PrintJob, Product, utcnow
from . import labels as label_service
from . import settings_store
from .dates import days_left, today_iso

log = logging.getLogger(__name__)


async def log_event(session: AsyncSession, type_: str, **kw) -> Event:
    event = Event(
        type=type_,
        label=kw.get("label", ""),
        barcode=kw.get("barcode", ""),
        name=kw.get("name", ""),
        location=kw.get("location", ""),
        device=kw.get("device", ""),
        payload=kw.get("payload", {}) or {},
    )
    session.add(event)
    return event


async def default_location(session: AsyncSession) -> str:
    from ..models import Location

    row = (
        await session.execute(
            select(Location).where(Location.is_default.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if row:
        return row.name
    row = (
        await session.execute(select(Location).order_by(Location.sort_order).limit(1))
    ).scalar_one_or_none()
    return row.name if row else ""


async def add_items(
    session: AsyncSession,
    *,
    name: str,
    barcode: str = "",
    brand: str = "",
    category: str = "",
    subcategory: str = "",
    expiry_date: str = "",
    quantity: float = 1.0,
    unit: str = "",
    location: str = "",
    note: str = "",
    count: int = 1,
    device_id: str = "",
    want_print: bool = True,
) -> tuple[list[InventoryItem], list[PrintJob]]:
    """`count` Etiketten anlegen und optional Druckauftraege einreihen."""
    count = max(1, min(int(count), 20))
    if not location:
        location = await default_location(session)

    numbers = await label_service.next_labels(session, count)
    printer_cfg = await settings_store.get(session, "printer", {})
    added_date = today_iso()

    items: list[InventoryItem] = []
    jobs: list[PrintJob] = []
    for number in numbers:
        item = InventoryItem(
            label=number,
            barcode=barcode,
            name=name,
            brand=brand,
            category=category,
            subcategory=subcategory,
            expiry_date=expiry_date,
            added_date=added_date,
            quantity=float(quantity),
            unit=unit,
            location=location,
            household=settings.household,
            note=note,
            status="active",
            source_device=device_id,
        )
        session.add(item)
        items.append(item)
        await log_event(
            session,
            "add",
            label=number,
            barcode=barcode,
            name=name,
            location=location,
            device=device_id,
            payload={"expiry_date": expiry_date, "quantity": quantity, "unit": unit},
        )

        if want_print and printer_cfg.get("enabled", True):
            payload = label_service.render_label(
                {
                    "label": number,
                    "name": name,
                    "brand": brand,
                    "subcategory": subcategory,
                    "expiry_date": expiry_date,
                    "added_date": added_date,
                    "quantity": quantity,
                    "unit": unit,
                    "location": location,
                },
                printer_cfg,
            )
            job = PrintJob(label=number, payload=payload, status="queued")
            session.add(job)
            jobs.append(job)

    await session.flush()
    return items, jobs


async def find_active_by_label(session: AsyncSession, label: str) -> InventoryItem | None:
    return (
        await session.execute(
            select(InventoryItem).where(
                InventoryItem.label == label, InventoryItem.status == "active"
            )
        )
    ).scalar_one_or_none()


async def find_removable_by_barcode(
    session: AsyncSession, barcode: str
) -> InventoryItem | None:
    """Aeltestes aktives Exemplar eines Produkt-Barcodes (FIFO nach MHD)."""
    rows = (
        await session.execute(
            select(InventoryItem)
            .where(InventoryItem.barcode == barcode, InventoryItem.status == "active")
            .order_by(InventoryItem.expiry_date.asc(), InventoryItem.id.asc())
            .limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


async def remove_item(
    session: AsyncSession, item: InventoryItem, reason: str = "manual", device_id: str = ""
) -> InventoryItem:
    item.status = "removed"
    item.removed_at = utcnow()
    item.removed_reason = reason
    await log_event(
        session,
        "remove",
        label=item.label,
        barcode=item.barcode,
        name=item.name,
        location=item.location,
        device=device_id,
        payload={"reason": reason},
    )
    await session.flush()
    return item


async def restore_by_label(
    session: AsyncSession, label: str, location: str = "", device_id: str = ""
) -> InventoryItem | None:
    """Versehentlich ausgebuchtes Etikett per Re-Scan zurueckbuchen.

    Uebernimmt das Verhalten aus dem ersten Projekt (48-h-Puffer), aber
    dauerhaft in der Datenbank statt in einem RAM-Ring, der jeden Reset
    verlor.
    """
    cutoff = utcnow() - timedelta(hours=settings.restore_window_hours)
    item = (
        await session.execute(
            select(InventoryItem).where(
                InventoryItem.label == label,
                InventoryItem.status == "removed",
                InventoryItem.removed_at.is_not(None),
                InventoryItem.removed_at >= cutoff,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        return None

    item.status = "active"
    item.removed_at = None
    item.removed_reason = ""
    if location:
        item.location = location
    await log_event(
        session,
        "restore",
        label=item.label,
        barcode=item.barcode,
        name=item.name,
        location=item.location,
        device=device_id,
    )
    await session.flush()
    return item


def with_days_left(item: InventoryItem) -> dict:
    data = {
        column.name: getattr(item, column.name) for column in item.__table__.columns
    }
    data["days_left"] = days_left(item.expiry_date)
    return data


async def stats(session: AsyncSession) -> dict:
    ui_cfg = await settings_store.get(session, "ui", {})
    warn_days = int(ui_cfg.get("expiring_days", 7))
    today = today_iso()
    horizon = (
        __import__("datetime").date.today() + timedelta(days=warn_days)
    ).isoformat()

    active = InventoryItem.status == "active"
    total = (
        await session.execute(
            select(func.count()).select_from(InventoryItem).where(active)
        )
    ).scalar_one()
    expired = (
        await session.execute(
            select(func.count())
            .select_from(InventoryItem)
            .where(active, InventoryItem.expiry_date != "", InventoryItem.expiry_date < today)
        )
    ).scalar_one()
    expiring = (
        await session.execute(
            select(func.count())
            .select_from(InventoryItem)
            .where(
                active,
                InventoryItem.expiry_date != "",
                InventoryItem.expiry_date >= today,
                InventoryItem.expiry_date <= horizon,
            )
        )
    ).scalar_one()

    by_category = dict(
        (
            await session.execute(
                select(InventoryItem.category, func.count())
                .where(active)
                .group_by(InventoryItem.category)
            )
        ).all()
    )
    by_location = dict(
        (
            await session.execute(
                select(InventoryItem.location, func.count())
                .where(active)
                .group_by(InventoryItem.location)
            )
        ).all()
    )

    since = utcnow() - timedelta(days=30)
    added_30d = (
        await session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.type == "add", Event.ts >= since)
        )
    ).scalar_one()
    removed_30d = (
        await session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.type == "remove", Event.ts >= since)
        )
    ).scalar_one()

    return {
        "total": total,
        "expiring": expiring,
        "expired": expired,
        "by_category": {k or "Ohne": v for k, v in by_category.items()},
        "by_location": {k or "Ohne": v for k, v in by_location.items()},
        "added_30d": added_30d,
        "removed_30d": removed_30d,
    }


async def product_for_barcode(session: AsyncSession, barcode: str) -> Product | None:
    return (
        await session.execute(select(Product).where(Product.barcode == barcode))
    ).scalar_one_or_none()
