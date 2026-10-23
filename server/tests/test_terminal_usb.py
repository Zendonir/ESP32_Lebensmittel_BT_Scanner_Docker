"""Drucker je Terminal: keiner, seriell (ESC/POS) oder Brother am USB-Anschluss."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import init_db, session_scope
from app.device import protocol as proto
from app.device import workflow
from app.device.hub import hub
from app.main import app
from app.models import Device, InventoryItem, PrintJob
from app.services import labels


class _Socket:
    def __init__(self) -> None:
        self.gesendet: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.gesendet.append(json.loads(payload))


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _drucken(drucker: str, caps, label: str, mit_artikel: bool = True):
    """Einen Auftrag an ein Terminal mit dieser Druckerart schicken."""
    async def scenario():
        await init_db()
        geraet = f"usb-{uuid.uuid4().hex[:6]}"
        async with session_scope() as session:
            for job in (await session.execute(select(PrintJob))).scalars().all():
                await session.delete(job)
            if mit_artikel:
                session.add(InventoryItem(label=label, name="Hähnchen", brand="Hof",
                                          expiry_date="2027-01-03", quantity=500, unit="g"))
            await session.commit()
        socket = _Socket()
        conn = await hub.register(geraet, socket)
        sess = workflow.session_for(geraet)
        sess.caps, sess.drucker = caps, drucker
        try:
            async with session_scope() as session:
                job = PrintJob(label=label, status="queued",
                               payload=labels.render_label({"label": label, "name": "x"},
                                                           {"label_layout": "standard"}))
                session.add(job)
                await session.commit()
                gesendet = await workflow.flush_print_queue(session, geraet)
                await session.refresh(job)
                return gesendet, job.status, job.error, socket.gesendet, job.id
        finally:
            await hub.unregister(conn)
            workflow.drop_session(geraet)

    return _run(scenario())


def test_usb_drucker_bekommt_brother_raster_in_stuecken():
    label = f"LEB{uuid.uuid4().int % 10**6:06d}"
    anzahl, status, _, gesendet, job_id = _drucken("usb", {"usb", proto.USB_DRUCK}, label)
    assert anzahl == 1 and status == "sent"
    stuecke = [m for m in gesendet if m["t"] == "usbdruck"]
    assert stuecke and not any(m["t"] == "print" for m in gesendet)
    assert [m["teil"] for m in stuecke] == list(range(len(stuecke)))
    assert stuecke[-1]["letzter"] and not any(m["letzter"] for m in stuecke[:-1])
    assert all(m["job"] == job_id for m in stuecke)
    daten = b"".join(base64.b64decode(m["daten"]) for m in stuecke)
    # Brother-Raster: beginnt mit dem Ungueltig-Block, endet mit Drucken (1Ah).
    assert daten[:100] == bytes(100) and daten.endswith(b"\x1a")
    # Jede Nachricht passt unter die Grenze der Firmware.
    assert all(len(json.dumps(m)) < 14336 for m in stuecke)


def test_usb_ohne_passende_firmware_scheitert_mit_hinweis():
    label = f"LEB{uuid.uuid4().int % 10**6:06d}"
    _, status, fehler, gesendet, _ = _drucken("usb", {"raster2"}, label)
    assert status == "failed" and "Firmware" in fehler and not gesendet


def test_usb_kann_keine_esc_pos_messstreifen():
    _, status, fehler, _, _ = _drucken("usb", {proto.USB_DRUCK}, "KALIB", mit_artikel=False)
    assert status == "failed" and "seriellen" in fehler


def test_terminal_ohne_drucker_bekommt_nichts():
    label = f"LEB{uuid.uuid4().int % 10**6:06d}"
    anzahl, status, _, gesendet, _ = _drucken("keiner", {"raster2"}, label)
    assert anzahl == 0 and status == "queued" and not gesendet


def test_seriell_bleibt_wie_bisher():
    label = f"LEB{uuid.uuid4().int % 10**6:06d}"
    anzahl, status, _, gesendet, _ = _drucken("seriell", set(), label)
    assert anzahl == 1 and status == "sent" and any(m["t"] == "print" for m in gesendet)


def test_terminal_ohne_drucker_ist_kein_druckziel():
    geraet = f"kein-{uuid.uuid4().hex[:6]}"

    async def scenario():
        conn = await hub.register(geraet, _Socket())
        sess = workflow.session_for(geraet)
        sess.household, sess.drucker = "Testhaus", "keiner"
        try:
            vorher = workflow.terminals_fuer("Testhaus")
            sess.drucker = "usb"
            return vorher, workflow.terminals_fuer("Testhaus"), workflow.drucker_online()
        finally:
            await hub.unregister(conn)
            workflow.drop_session(geraet)

    vorher, nachher, alle = _run(scenario())
    assert geraet not in vorher and geraet in nachher and geraet in alle


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_drucker_und_usb_scanner_einstellen(client):
    geraet = f"cfg-{uuid.uuid4().hex[:6]}"

    async def anlegen():
        async with session_scope() as session:
            session.add(Device(device_id=geraet, name="Küche"))
            await session.commit()
    _run(anlegen())

    zeile = next(d for d in client.get("/api/devices").json() if d["device_id"] == geraet)
    assert zeile["drucker"] == "seriell" and zeile["usb_scanner"] is False
    r = client.patch(f"/api/devices/{geraet}", json={"drucker": "usb", "usb_scanner": True})
    assert r.status_code == 200 and r.json()["drucker"] == "usb" and r.json()["usb_scanner"]
    assert client.patch(f"/api/devices/{geraet}", json={"drucker": "laser"}).status_code == 422

    sess = workflow.session_for(geraet)
    assert sess.drucker == "usb" and sess.usb_scanner
    cfg = workflow.config_fuer_geraet({"brightness": 80}, sess)
    assert cfg["drucker"] == "usb" and cfg["usb_scanner"] is True
    # Rundruf ohne Sitzung: die Felder fehlen, die Firmware behaelt ihren Stand.
    assert "drucker" not in workflow.config_fuer_geraet({"brightness": 80})
    workflow.drop_session(geraet)
