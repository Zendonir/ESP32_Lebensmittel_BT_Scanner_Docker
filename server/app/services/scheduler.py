"""Hintergrundaufgaben: MHD-Ueberwachung und Aufraeumen.

Laeuft als einzelner asyncio-Task im Container. Er weckt jede Minute auf,
arbeitet aber nur, wenn tatsaechlich etwas faellig ist - so bleibt der
Ressourcenverbrauch im Leerlauf bei praktisch null.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Event, InventoryItem, PrintJob, utcnow
from ..services import notify, settings_store
from ..services.dates import to_display, today_iso

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def check_expiring(force: bool = False) -> dict:
    """Ablaufende und abgelaufene Artikel melden - hoechstens einmal taeglich."""
    async with session_scope() as session:
        today = today_iso()
        last = await settings_store.get(session, "last_expiry_check", "")
        if last == today and not force:
            return {"skipped": True, "last": last}

        warn_days = settings.expiry_warn_days
        horizon = (date.today() + timedelta(days=warn_days)).isoformat()
        rows = (
            await session.execute(
                select(InventoryItem)
                .where(
                    InventoryItem.status == "active",
                    InventoryItem.expiry_date != "",
                    InventoryItem.expiry_date <= horizon,
                )
                .order_by(InventoryItem.expiry_date)
            )
        ).scalars().all()

        expired = [r for r in rows if r.expiry_date < today]
        soon = [r for r in rows if r.expiry_date >= today]

        await settings_store.put(session, "last_expiry_check", today)
        await session.commit()

        if not rows:
            return {"expired": 0, "soon": 0, "notified": False}

        lines = []
        if expired:
            lines.append(f"Abgelaufen ({len(expired)}):")
            lines += [
                f"  - {r.name} ({to_display(r.expiry_date)}, {r.location})"
                for r in expired[:15]
            ]
        if soon:
            lines.append(f"Laeuft ab in {warn_days} Tagen ({len(soon)}):")
            lines += [
                f"  - {r.name} ({to_display(r.expiry_date)}, {r.location})"
                for r in soon[:15]
            ]

        result = await notify.send(
            "Lebensmittel-Scanner: MHD",
            "\n".join(lines),
            priority="high" if expired else "default",
        )
        await notify.mqtt_publish(
            "expiry",
            {"expired": len(expired), "soon": len(soon), "checked": today},
            retain=True,
        )

        session.add(
            Event(
                type="notify",
                name="MHD-Bericht",
                payload={"expired": len(expired), "soon": len(soon), **result},
            )
        )
        await session.commit()
        return {"expired": len(expired), "soon": len(soon), "notified": any(result.values())}


async def publish_state() -> None:
    """Bestandszahlen nach MQTT - fuer Home-Assistant-Sensoren."""
    if not settings.mqtt_host:
        return
    from ..services import inventory as inv

    async with session_scope() as session:
        counts = await inv.stats(session)
    await notify.mqtt_publish("state", counts, retain=True)


async def cleanup() -> None:
    """Alte Events und erledigte Druckauftraege abraeumen."""
    async with session_scope() as session:
        cutoff = utcnow() - timedelta(days=180)
        rows = (
            await session.execute(select(Event).where(Event.ts < cutoff).limit(5000))
        ).scalars().all()
        for row in rows:
            await session.delete(row)

        job_cutoff = utcnow() - timedelta(days=7)
        jobs = (
            await session.execute(
                select(PrintJob).where(
                    PrintJob.status.in_(("done", "failed")),
                    PrintJob.updated_at < job_cutoff,
                )
            )
        ).scalars().all()
        for job in jobs:
            await session.delete(job)

        if rows or jobs:
            await session.commit()
            log.info("Aufgeraeumt: %d Events, %d Druckauftraege", len(rows), len(jobs))


async def _loop() -> None:
    last_cleanup = date.min
    while True:
        try:
            now = datetime.now()
            if now.hour == settings.expiry_check_hour:
                await check_expiring()
            await publish_state()
            # Der Lagerort verfaellt nach einer halben Stunde Ruhe. Ohne
            # diesen Takt faende das erst beim naechsten Antippen statt, und
            # die Statusleiste zeigte bis dahin einen Ort, der nicht mehr gilt.
            # Erst hier importiert: workflow haengt an den Diensten, ein
            # Import oben schloesse den Kreis.
            from ..device import workflow

            await workflow.check_idle_locations()
            if now.date() != last_cleanup and now.hour == 3:
                await cleanup()
                last_cleanup = now.date()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Der Scheduler darf nie sterben - sonst faellt die MHD-Warnung
            # stillschweigend aus, und genau das faellt monatelang niemandem auf.
            log.exception("Fehler im Scheduler-Durchlauf")
        await asyncio.sleep(60)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="scheduler")
        log.info("Scheduler gestartet (MHD-Pruefung um %02d:00 Uhr)", settings.expiry_check_hour)


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
