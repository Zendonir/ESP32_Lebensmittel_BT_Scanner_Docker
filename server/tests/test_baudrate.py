"""Baudrate zum Drucker: einstellbar, mit jedem Auftrag unterwegs.

Die Einstellung `printer.baud` stand lange in den Vorgaben, ohne dass sie
jemand las - die Firmware sendete fest mit 9600. Geprueft wird hier, dass sie
jetzt beim Terminal ankommt, und dass eine Firmware, die nicht umschalten
kann, keine Etiketten bekommt, die sie nur als Zeichensalat drucken wuerde.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.db import init_db, session_scope
from app.device import protocol as proto
from app.device import workflow
from app.device.hub import hub
from app.models import PrintJob, Setting
from app.services import labels, settings_store

ITEM = {"label": "LEB000321", "name": "Suppe", "expiry_date": "2026-10-01"}


@pytest.mark.parametrize("eingestellt,erwartet", [
    (115200, 115200), ("38400", 38400), (9600, 9600),
    (12345, 9600), ("schnell", 9600), (None, 9600),
])
def test_nur_bekannte_baudraten(eingestellt, erwartet):
    assert labels.baudrate({"baud": eingestellt}) == erwartet


def test_baudrate_steht_im_auftrag():
    nachricht = proto.print_job(7, {"chars": 32, "blocks": []}, 57600)
    assert nachricht["baud"] == 57600
    assert proto.print_job(7, {"blocks": []})["baud"] == 9600


def test_baudrate_ist_eine_bekannte_einstellung():
    assert settings_store.DEFAULTS["printer"]["baud"] == labels.BAUD_STANDARD


class _Socket:
    def __init__(self) -> None:
        self.gesendet: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.gesendet.append(payload)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("baud,caps,erwartet", [
    (9600, set(), "sent"),              # alte Firmware, alte Leitung: wie bisher
    (115200, None, "queued"),           # hello steht aus: warten
    (115200, set(), "failed"),          # alte Firmware kann nicht umschalten
    (115200, {"raster2", "baud"}, "sent"),
])
def test_andere_baudrate_nur_an_terminals_die_umschalten(baud, caps, erwartet):
    async def scenario():
        await init_db()
        async with session_scope() as session:
            for job in (await session.execute(select(PrintJob))).scalars().all():
                await session.delete(job)
            await settings_store.put(session, "printer",
                                     {**settings_store.DEFAULTS["printer"], "baud": baud})
            await session.commit()
        geraet = f"baud-{baud}-{erwartet}"
        socket = _Socket()
        conn = await hub.register(geraet, socket)
        workflow.session_for(geraet).caps = caps
        try:
            async with session_scope() as session:
                job = PrintJob(label="LEB000321", status="queued",
                               payload=labels.render_label(ITEM, {"label_layout": "standard"}))
                session.add(job)
                await session.commit()
                await workflow.flush_print_queue(session, geraet)
                await session.refresh(job)
                return job.status, job.error, socket.gesendet
        finally:
            await hub.unregister(conn)
            workflow.drop_session(geraet)
            async with session_scope() as session:
                zeile = await session.get(Setting, "printer")
                if zeile is not None:
                    await session.delete(zeile)
                await session.commit()

    status, fehler, gesendet = _run(scenario())
    assert status == erwartet
    if erwartet == "failed":
        assert "Baud" in fehler and "Firmware" in fehler
    if erwartet == "sent":
        auftraege = [json.loads(g) for g in gesendet if json.loads(g).get("t") == "print"]
        assert auftraege and auftraege[-1]["baud"] == baud
