"""Letztes Etikett nachdrucken - am Terminal und ueber die API."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import session_scope
from app.device import workflow
from app.device.hub import hub
from app.main import app
from app.models import InventoryItem, PrintJob


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_letztes_etikett_ueber_die_api(client):
    name = f"Gulasch {uuid.uuid4().hex[:6]}"
    etiketten = client.post("/api/labels", json={"name": name, "count": 2}).json()["labels"]
    client.post("/api/labels/test-print")       # Testdruck zaehlt nicht als "letztes Etikett"
    r = client.post("/api/labels/reprint-last")
    assert r.status_code == 200, r.text
    assert r.json()["label"] == etiketten[-1] and r.json()["name"] == name

    async def zaehlen():
        async with session_scope() as session:
            bestand = await session.scalar(select(func.count()).select_from(InventoryItem)
                                           .where(InventoryItem.name == name))
            drucke = await session.scalar(select(func.count()).select_from(PrintJob)
                                          .where(PrintJob.label == etiketten[-1]))
            return bestand, drucke
    assert _run(zaehlen()) == (2, 2)     # kein neuer Artikel, ein Druck mehr


def test_nachdruck_am_terminal(client, monkeypatch):
    gesendet = []

    async def senden(device_id, msg):
        gesendet.append(msg)
    monkeypatch.setattr(hub, "send_to", senden)

    name = f"Suppe {uuid.uuid4().hex[:6]}"
    etikett = client.post("/api/labels", json={"name": name}).json()["labels"][0]
    geraet = f"nd-{uuid.uuid4().hex[:6]}"
    sess = workflow.session_for(geraet)

    async def ablauf():
        async with session_scope() as session:
            start = await workflow.render(session, sess)
            await workflow.on_tap(session, sess, "nachdruck")
            return start, sess.current
    start, danach = _run(ablauf())
    assert any(i["id"] == "nachdruck" for i in start["items"])
    assert danach == workflow.HOME
    assert any(m.get("t") == "toast" and etikett in m["text"] for m in gesendet)
    workflow.drop_session(geraet)


def test_startseite_drei_spalten_nur_mit_home3():
    async def bild(caps):
        async with session_scope() as s:
            sess = workflow.DeviceSession(device_id=f"t{uuid.uuid4().hex[:6]}")
            sess.caps = caps
            return await workflow._screen_home(s, sess, 1, {"scanner": False, "location": ""})

    alt = _run(bild(None))
    assert "cols" not in alt["meta"]
    assert "new_roll" not in [i["id"] for i in alt["items"]]
    neu = _run(bild({"home3"}))
    assert neu["meta"]["cols"] == 3 and neu["meta"]["roll_button"] is False
    assert len(neu["items"]) == 6 and "new_roll" in [i["id"] for i in neu["items"]]
