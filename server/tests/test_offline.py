"""Offline-Betrieb: was das Terminal ohne Verbindung gesammelt hat, wird nachgeholt."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.device import workflow
from app.device.hub import hub
from app.main import app
from app.models import OfflineScan
from app.services import inventory as inv
from app.services import openfoodfacts


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_abgleich_lagert_aus_und_merkt_produkte(client, monkeypatch):
    gesendet = []

    async def senden(device_id, msg):
        gesendet.append(msg)

    async def nachschlagen(session, code):
        return None

    monkeypatch.setattr(hub, "send_to", senden)
    monkeypatch.setattr(openfoodfacts, "lookup", nachschlagen)

    etikett = client.post("/api/labels", json={"name": "Brot", "print": False}).json()["labels"][0]
    geraet = f"off-{uuid.uuid4().hex[:6]}"
    sess = workflow.session_for(geraet)
    sess.location = "Kühlschrank"

    async def abgleich():
        async with session_scope() as session:
            return await workflow.on_offline(session, sess, [
                {"code": etikett, "age_s": 120, "loc": "Keller"},
                {"code": etikett, "age_s": 100, "loc": "Keller"},      # doppelt gescannt
                {"code": "LEB999998", "age_s": 90},                   # gibt es nicht
                {"code": "4000000000017", "age_s": 60, "loc": "Keller"},
                {"code": "", "age_s": 1},
                "Unsinn",
            ])

    zaehler = _run(abgleich())
    assert zaehler == {"ausgelagert": 1, "nachtragen": 1, "unbekannt": 1, "doppelt": 1}
    # Doppelt gescannt bucht offline NICHT wieder ein.
    assert client.get(f"/api/inventory/{etikett}").json()["status"] == "removed"
    assert any(m.get("t") == "toast" and "1 ausgelagert" in m["text"] for m in gesendet)

    # Startseite bietet das Nachtragen an, die Liste fuehrt hin.
    async def ablauf():
        async with session_scope() as session:
            sess.reset()
            start = await workflow.render(session, sess)
            await workflow.on_tap(session, sess, "nachtrag")
            liste = await workflow.render(session, sess)
            ersten = liste["items"][0]["id"]
            await workflow.on_tap(session, sess, ersten)
            return start, liste, sess.current, sess.offline_id, sess.location, sess.draft.barcode
    start, liste, aktuell, offline_id, ort, code = _run(ablauf())
    assert start["items"][0]["id"] == "nachtrag" and "(1)" in start["items"][0]["label"]
    assert liste["items"][0]["label"] == "4000000000017"
    assert aktuell == workflow.UNKNOWN and offline_id and code == "4000000000017"
    assert ort == "Keller"            # dort einlagern, wo gescannt wurde

    # Eingelagert -> erledigt.
    async def einlagern():
        async with session_scope() as session:
            sess.draft.name = "Offline-Suppe"
            await workflow._save_draft(session, sess)
            row = await session.get(OfflineScan, offline_id)
            return row.done
    assert _run(einlagern()) is True
    workflow.drop_session(geraet)


def test_alle_verwerfen(client, monkeypatch):
    async def senden(device_id, msg):
        return None

    monkeypatch.setattr(hub, "send_to", senden)
    geraet = f"off-{uuid.uuid4().hex[:6]}"
    sess = workflow.session_for(geraet)

    async def ablauf():
        async with session_scope() as session:
            await workflow.on_offline(session, sess, [{"code": "4000000000024", "age_s": -1}])
            sess.stack = [workflow.HOME, workflow.NACHTRAG]
            await workflow.on_tap(session, sess, "off:verwerfen")
            offen = (await session.execute(select(OfflineScan).where(
                OfflineScan.device_id == geraet, OfflineScan.done.is_(False)))).scalars().all()
            return len(offen), sess.current
    assert _run(ablauf()) == (0, workflow.HOME)
    workflow.drop_session(geraet)


def test_offline_nachricht_wird_quittiert(client, monkeypatch):
    from app.config import settings

    with client.websocket_connect(
            f"/ws/device?id=offws-{uuid.uuid4().hex[:6]}&token={settings.device_token}") as ws:
        ws.send_json({"t": "hello", "caps": []})
        ws.send_json({"t": "offline", "items": [{"code": "4000000000031", "age_s": 5}]})
        for _ in range(20):
            msg = ws.receive_json()
            if msg.get("t") == "offline_ack":
                assert msg["n"] == 1
                break
        else:
            raise AssertionError("keine Quittung")


def test_config_nennt_das_etikettenpraefix():
    from app.config import settings

    assert workflow.config_fuer_geraet({"brightness": 50}) == {
        "brightness": 50, "label_prefix": settings.label_prefix}


def test_find_by_label_findet_auch_ausgelagerte(client):
    etikett = client.post("/api/labels", json={"name": "Käse", "print": False}).json()["labels"][0]
    client.post("/api/inventory/scan", json={"code": etikett})

    async def suchen():
        async with session_scope() as session:
            return (await inv.find_by_label(session, etikett)).status
    assert _run(suchen()) == "removed"
