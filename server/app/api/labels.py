"""Etiketten anlegen, drucken und die Rolle verwalten."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..device import workflow
from ..device.hub import hub
from ..models import InventoryItem, PrintJob
from ..schemas import LabelCreate, LabelCreateResult, PrintJobOut
from ..services import inventory as inv
from ..services import labels as label_service
from ..services import settings_store

router = APIRouter(prefix="/api/labels", tags=["etiketten"])

# Beispiel fuer die Vorschau. Bewusst mit langem Namen und Umlauten - ein
# Layout, das nur mit "Brot" gut aussieht, taugt nichts.
_SAMPLE = {
    "label": "LEB000123",
    "name": "Schweinefilet",
    "subcategory": "Schwein",
    "brand": "Landmetzgerei Grüner",
    "expiry_date": "2026-09-30",
    "added_date": "2026-08-12",
    "quantity": 500,
    "unit": "g",
    "location": "Kühlschrank",
}


@router.get("/layouts")
async def list_layouts(session: AsyncSession = Depends(get_session)):
    """Die waehlbaren Layouts, jeweils mit massstabsgetreuer Vorschau.

    Die Vorschau entsteht aus demselben Payload, den auch der Drucker
    bekommt - sonst waere sie ein zweites Layout, das irgendwann abweicht.
    """
    cfg = dict(await settings_store.get(session, "printer", {}))
    out = []
    for name, description in label_service.LAYOUTS.items():
        payload = label_service.render_label(_SAMPLE, {**cfg, "label_layout": name})
        # Ohne Vorschub und Rueckzug: der Vorschub reicht per Definition bis
        # zur Perforation und wuerde jedes Layout als randvoll ausweisen, der
        # Rueckzug zaehlt negativ und wuerde es kuenstlich leer rechnen.
        used = label_service.total_dots(
            [b for b in payload["blocks"] if b.get("t") not in ("feed", "back")]
        )
        codes = [b["t"] for b in payload["blocks"] if b["t"] in ("qr", "code128")]
        mm = used / label_service.DOTS_PER_MM
        out.append({
            "name": name,
            "mm": round(mm, 1),
            "code_mm": round(
                label_service.total_dots(
                    [b for b in payload["blocks"] if b["t"] in ("qr", "code128")]
                ) / label_service.DOTS_PER_MM, 1),
            "passt": used <= payload["height_dots"] - label_service.SAFETY_DOTS,
            "code": "QR-Code" if codes and codes[0] == "qr" else "Strichcode",
            "rotate": payload["rotate"],
            "title": label_service.TITLES.get(name, name),
            "description": description,
            "dots": used,
            "height_dots": payload["height_dots"],
            "svg": label_service.render_preview_svg(payload, cfg),
        })
    return out


@router.post("", response_model=LabelCreateResult, status_code=201)
async def create_labels(body: LabelCreate, session: AsyncSession = Depends(get_session)):
    """Derselbe Weg wie der Geraete-Workflow - eine Implementierung, zwei Aufrufer."""
    items, jobs = await inv.add_items(
        session,
        name=body.name,
        barcode=body.barcode,
        brand=body.brand,
        category=body.category,
        subcategory=body.subcategory,
        expiry_date=body.expiry_date,
        quantity=body.quantity,
        unit=body.unit,
        location=body.location,
        count=body.count,
        want_print=body.print,
    )
    await session.commit()

    printed = False
    if jobs:
        target = body.device_id or (hub.online_ids()[0] if hub.online_ids() else None)
        if target:
            printed = await workflow.flush_print_queue(session, target) > 0

    await hub.notify_ui("inventory")
    return LabelCreateResult(
        labels=[item.label for item in items],
        printed=printed,
        print_jobs=[job.id for job in jobs],
    )


@router.post("/reprint/{label}")
async def reprint(
    label: str,
    device_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Etikett neu drucken, ohne einen zweiten Bestandseintrag zu erzeugen.

    Im ersten Projekt gab es nur "letztes Etikett erneut drucken" aus einem
    RAM-Puffer; nach einem Neustart war die Vorlage weg.
    """
    item = (
        await session.execute(select(InventoryItem).where(InventoryItem.label == label))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Etikett unbekannt")

    printer_cfg = await settings_store.get(session, "printer", {})
    payload = label_service.render_label(
        {
            "label": item.label,
            "name": item.name,
            "brand": item.brand,
            "subcategory": item.subcategory,
            "expiry_date": item.expiry_date,
            "added_date": item.added_date,
            "quantity": item.quantity,
            "unit": item.unit,
            "location": item.location,
        },
        printer_cfg,
    )
    job = PrintJob(label=item.label, payload=payload, status="queued")
    session.add(job)
    # Ein Nachdruck verbraucht ein Etikett wie jeder andere Druck auch.
    await label_service.consume_roll(session, 1)
    await inv.log_event(session, "print", label=item.label, name=item.name)
    await session.commit()

    target = device_id or (hub.online_ids()[0] if hub.online_ids() else None)
    sent = await workflow.flush_print_queue(session, target) if target else 0
    return {"ok": True, "job": job.id, "sent": bool(sent)}


@router.post("/test-print")
async def test_print(
    device_id: str | None = None, session: AsyncSession = Depends(get_session)
):
    printer_cfg = await settings_store.get(session, "printer", {})
    payload = label_service.render_label(
        {
            "label": "LEB000000",
            "name": "Testetikett",
            "brand": "Lebensmittel-Scanner",
            "subcategory": "",
            "expiry_date": "2099-12-31",
            "added_date": "2099-01-01",
            "quantity": 1,
            "unit": "",
            "location": "Test",
        },
        printer_cfg,
    )
    job = PrintJob(label="TEST", payload=payload, status="queued")
    session.add(job)
    await label_service.consume_roll(session, 1)
    await session.commit()
    target = device_id or (hub.online_ids()[0] if hub.online_ids() else None)
    if not target:
        raise HTTPException(503, "Kein Geraet online")
    sent = await workflow.flush_print_queue(session, target)
    return {"ok": bool(sent), "job": job.id}


@router.get("/queue", response_model=list[PrintJobOut])
async def queue(session: AsyncSession = Depends(get_session), limit: int = 50):
    return (
        await session.execute(
            select(PrintJob).order_by(PrintJob.id.desc()).limit(limit)
        )
    ).scalars().all()


@router.delete("/queue")
async def clear_queue(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(PrintJob).where(PrintJob.status.in_(("queued", "sent", "failed")))
        )
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    await session.commit()
    return {"cleared": len(rows)}


@router.get("/roll")
async def get_roll(session: AsyncSession = Depends(get_session)):
    return await label_service.roll_state(session)


@router.post("/roll")
async def set_roll(
    size: int = Query(ge=0, le=100000), session: AsyncSession = Depends(get_session)
):
    return await label_service.new_roll(session, size)
