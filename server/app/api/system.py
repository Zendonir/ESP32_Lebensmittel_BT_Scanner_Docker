"""Geraete, Einkaufsliste, Einstellungen, Diagnose."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import platform
import sqlite3
import tempfile
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ..config import settings
from ..db import get_session
from ..device import protocol as proto
from ..device import workflow
from ..device.hub import hub
from ..models import Device, Event, InventoryItem, ShoppingItem
from ..schemas import (
    DeviceOut,
    DevicePatch,
    EventOut,
    ShoppingIn,
    ShoppingOut,
)
from ..services import importer as importer_service
from ..services import inventory as inv
from ..services import notify, settings_store
from ..services.dates import to_display

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["system"])
_STARTED = time.time()


# --------------------------------------------------------------------- Geraete
@router.post("/ws-ticket")
async def ws_ticket():
    """Eintrittskarte fuer den Live-Socket der Oberflaeche.

    Diese Route liegt hinter dem Web-Passwort; wer sie aufrufen darf, darf
    auch zuhoeren. Siehe device/routes.py fuer den Grund, warum es ueberhaupt
    eine Karte braucht.
    """
    from ..device.routes import TICKET_GUELTIG_S, neues_ui_ticket

    return {"ticket": neues_ui_ticket(), "expires_in": TICKET_GUELTIG_S}


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Device).order_by(Device.name))).scalars().all()
    online = set(hub.online_ids())
    for row in rows:
        row.online = row.device_id in online
    return rows


@router.patch("/devices/{device_id}", response_model=DeviceOut)
async def patch_device(
    device_id: str, body: DevicePatch, session: AsyncSession = Depends(get_session)
):
    row = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Geraet unbekannt")
    for key, value in body.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(row, key, value)
    await session.commit()

    if body.active_location is not None:
        sess = workflow.session_for(device_id)
        sess.location = body.active_location
        await workflow.push_screen(session, sess)
    return row


@router.post("/devices/{device_id}/reboot")
async def reboot_device(device_id: str):
    if not await hub.send_to(device_id, proto.reboot()):
        raise HTTPException(503, "Geraet nicht erreichbar")
    return {"ok": True}


@router.post("/devices/{device_id}/beep")
async def beep_device(device_id: str, pattern: str = "ok"):
    if not await hub.send_to(device_id, proto.beep(pattern)):
        raise HTTPException(503, "Geraet nicht erreichbar")
    return {"ok": True}


@router.post("/devices/{device_id}/scan")
async def inject_scan(
    device_id: str,
    code: str = Query(min_length=1),
    session: AsyncSession = Depends(get_session),
):
    """Scan simulieren - unschaetzbar zum Testen ohne BLE-Scanner."""
    sess = workflow.session_for(device_id)
    await workflow.on_scan(session, sess, code)
    return {"ok": True}


@router.delete("/devices/{device_id}")
async def forget_device(device_id: str, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Geraet unbekannt")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


# --------------------------------------------------------------- Einkaufsliste
@router.get("/shopping", response_model=list[ShoppingOut])
async def list_shopping(session: AsyncSession = Depends(get_session)):
    return (
        await session.execute(
            select(ShoppingItem).order_by(ShoppingItem.done, ShoppingItem.name)
        )
    ).scalars().all()


@router.post("/shopping", response_model=ShoppingOut, status_code=201)
async def add_shopping(body: ShoppingIn, session: AsyncSession = Depends(get_session)):
    row = ShoppingItem(**body.model_dump())
    session.add(row)
    await session.commit()
    await hub.notify_ui("shopping")
    return row


@router.put("/shopping/{item_id}", response_model=ShoppingOut)
async def update_shopping(
    item_id: int, body: ShoppingIn, session: AsyncSession = Depends(get_session)
):
    row = await session.get(ShoppingItem, item_id)
    if row is None:
        raise HTTPException(404, "Eintrag unbekannt")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await hub.notify_ui("shopping")
    return row


@router.delete("/shopping/{item_id}")
async def delete_shopping(item_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(ShoppingItem, item_id)
    if row is None:
        raise HTTPException(404, "Eintrag unbekannt")
    await session.delete(row)
    await session.commit()
    await hub.notify_ui("shopping")
    return {"ok": True}


# ------------------------------------------------------------- Einstellungen
@router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_session)):
    stored = await settings_store.all_settings(session)
    return {
        **stored,
        "household": settings.household,
        "label_prefix": settings.label_prefix,
        "notify": notify.configured_channels(),
        "openfoodfacts": settings.openfoodfacts_enabled,
        "expiry_warn_days": settings.expiry_warn_days,
        "restore_window_hours": settings.restore_window_hours,
    }


@router.patch("/settings/{key}")
async def patch_settings(
    key: str, body: dict, session: AsyncSession = Depends(get_session)
):
    if key not in settings_store.DEFAULTS:
        raise HTTPException(404, f"Unbekannter Einstellungsschluessel: {key}")
    merged = await settings_store.merge(session, key, body)
    await session.commit()

    # Anzeige-Einstellungen sofort an alle Geraete durchreichen.
    if key == "device_ui":
        await hub.broadcast_devices(proto.config(merged))
    await hub.notify_ui("settings")
    return merged


# ------------------------------------------------------------------ Diagnose
@router.get("/events", response_model=list[EventOut])
async def list_events(
    session: AsyncSession = Depends(get_session),
    type: str = "",
    limit: int = Query(200, ge=1, le=2000),
):
    stmt = select(Event).order_by(Event.id.desc()).limit(limit)
    if type:
        stmt = stmt.where(Event.type == type)
    return (await session.execute(stmt)).scalars().all()


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    """Fuer Docker-Healthcheck und Monitoring."""
    await session.execute(select(1))
    return {
        "status": "ok",
        "uptime": int(time.time() - _STARTED),
        "devices_online": len(hub.online_ids()),
        "ui_clients": hub.ui_count(),
    }


@router.get("/system")
async def system_info(session: AsyncSession = Depends(get_session)):
    counts = await inv.stats(session)
    return {
        "version": os.getenv("APP_VERSION", "dev"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime": int(time.time() - _STARTED),
        "database": settings.database_url.split("://", 1)[0],
        "timezone": settings.timezone,
        "devices": {
            "online": hub.online_ids(),
            "count": len(hub.online_ids()),
        },
        "inventory": counts,
        "notify": notify.configured_channels(),
    }


@router.post("/notify/test")
async def notify_test():
    result = await notify.send(
        "Lebensmittel-Scanner", "Testbenachrichtigung vom Server."
    )
    if not any(result.values()):
        raise HTTPException(503, "Kein Kanal konfiguriert oder alle fehlgeschlagen")
    return result


# ---------------------------------------------------------------- Export/Import
@router.get("/export/csv")
async def export_csv(
    session: AsyncSession = Depends(get_session), status: str = "active"
):
    stmt = select(InventoryItem).order_by(InventoryItem.expiry_date)
    if status != "all":
        stmt = stmt.where(InventoryItem.status == status)
    rows = (await session.execute(stmt)).scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "Etikett", "Barcode", "Name", "Marke", "Kategorie", "Sorte",
            "MHD", "Eingang", "Menge", "Einheit", "Ort", "Status",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.label, row.barcode, row.name, row.brand, row.category,
                row.subcategory, to_display(row.expiry_date), to_display(row.added_date),
                f"{row.quantity:g}", row.unit, row.location, row.status,
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="inventar.csv"'},
    )


class ImportResult(BaseModel):
    imported: int
    skipped_duplicate: int
    skipped_invalid: int
    total_rows: int
    errors: list[str]
    dry_run: bool


@router.post("/import/v1", response_model=ImportResult)
async def import_v1(
    file: UploadFile,
    dry_run: bool = Query(
        False, description="Nur pruefen, nichts in die Datenbank schreiben"
    ),
    session: AsyncSession = Depends(get_session),
):
    """Bestand aus Version 1 uebernehmen - drei erkannte Dateiformen:

    * `.csv`  - der MySQL-View `current_inventory`
    * `.json` - eine einzelne Datei aus der SD-Sicherung
                (`inventory.json` oder `removed_items.json`)
    * `.zip`  - der komplette Ordner `/scanner_backup/` von der SD-Karte

    Etiketten mit bereits vorhandenem `label_barcode` werden uebersprungen,
    ein wiederholter Import nach einem Abbruch ist damit gefahrlos.
    """
    name = (file.filename or "").lower()
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "Datei zu groß (Grenze 20 MB)")

    if name.endswith(".csv"):
        result = await importer_service.import_csv(session, content, dry_run=dry_run)
    elif name.endswith(".json"):
        result = await importer_service.import_sd_json(session, content, dry_run=dry_run)
    elif name.endswith(".zip"):
        result = await importer_service.import_sd_zip(session, content, dry_run=dry_run)
    else:
        raise HTTPException(400, "Bitte eine .csv-, .json- oder .zip-Datei hochladen")

    if not dry_run and result.imported:
        await hub.notify_ui("inventory")

    return ImportResult(
        imported=result.imported,
        skipped_duplicate=result.skipped_duplicate,
        skipped_invalid=result.skipped_invalid,
        total_rows=result.total_rows,
        errors=result.errors[:50],
        dry_run=dry_run,
    )


@router.get("/backup/db")
def backup_db():
    """Die komplette Datenbank als eine Datei.

    Anders als der JSON-Export unten ist das wirklich *alles*: auch das
    Ereignisprotokoll, die Geraete und die Druckauftraege.

    Bewusst ueber die Online-Sicherung von SQLite und nicht ueber ein Kopieren
    der Datei: die Datenbank laeuft im WAL-Modus, die zuletzt geschriebenen
    Aenderungen stehen also noch in `-wal` und nicht in der `.db`. Eine
    schlichte Kopie waere damit unvollstaendig - und das faellt erst auf, wenn
    man sie im Ernstfall braucht. backup() zieht stattdessen eine in sich
    stimmige Momentaufnahme, auch waehrend nebenher geschrieben wird.

    Die Funktion ist absichtlich synchron: FastAPI legt sie damit in einen
    Arbeitsthread, statt den Ereignisschleifen-Thread zu blockieren.
    """
    url = make_url(settings.database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise HTTPException(
            409,
            "Nur fuer die eingebaute SQLite-Datenbank. Bei einer externen "
            "Datenbank sichert deren eigenes Werkzeug (z.B. pg_dump); die "
            "Nutzdaten gibt es hier ueber /api/export/json.",
        )
    if ":memory:" in url.database:
        raise HTTPException(409, "Datenbank liegt nur im Arbeitsspeicher")

    handle, temp_path = tempfile.mkstemp(prefix="backup-", suffix=".db")
    os.close(handle)
    try:
        source = sqlite3.connect(url.database)
        target = sqlite3.connect(temp_path)
        with target:
            source.backup(target)
        target.close()
        source.close()
    except Exception:
        os.unlink(temp_path)
        raise

    stamp = datetime.now().strftime("%Y-%m-%d")
    return FileResponse(
        temp_path,
        media_type="application/octet-stream",
        filename=f"lebensmittel-{stamp}.db",
        # Erst loeschen, wenn die Antwort durch ist - sonst zieht man dem
        # laufenden Download die Datei unter den Fuessen weg.
        background=BackgroundTask(os.unlink, temp_path),
    )


@router.get("/export/json")
async def export_json(session: AsyncSession = Depends(get_session)):
    """Vollstaendige Sicherung aller Nutzdaten."""
    from ..models import Category, Location, Product, Template

    async def dump(model):
        rows = (await session.execute(select(model))).scalars().all()
        return [
            {
                c.name: (
                    v.isoformat() if hasattr(v := getattr(r, c.name), "isoformat") else v
                )
                for c in model.__table__.columns
            }
            for r in rows
        ]

    payload = {
        "version": 2,
        "categories": await dump(Category),
        "locations": await dump(Location),
        "templates": await dump(Template),
        "products": await dump(Product),
        "inventory": await dump(InventoryItem),
        "shopping": await dump(ShoppingItem),
        "settings": await settings_store.all_settings(session),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="backup.json"'},
    )
