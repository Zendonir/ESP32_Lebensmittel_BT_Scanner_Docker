"""Etiketten aus Web/Handy drucken am Terminal des eigenen Haushalts.

Vorher ging ein Auftrag an das erste Terminal, das online war, und jedes
Terminal druckte, was in der Schlange lag - auch die Etiketten des
Nachbarhaushalts.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.device import workflow
from app.device.hub import hub
from app.main import app
from app.models import Device, PrintJob


class _Socket:
    def __init__(self):
        self.gesendet: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.gesendet.append(json.loads(payload))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _haushalt(client, name):
    r = client.post("/api/households", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _terminal(client, device_id, haushalt):
    async def anlegen():
        async with session_scope() as session:
            session.add(Device(device_id=device_id, name=f"Terminal {device_id}"))
            await session.commit()
        sock = _Socket()
        await hub.register(device_id, sock)
        return sock

    sock = _run(anlegen())
    assert client.patch(f"/api/devices/{device_id}", json={"household": haushalt}).status_code == 200
    workflow.session_for(device_id).caps = {"raster2", "baud", "raw"}
    return sock


def _weg(*device_ids):
    async def trennen():
        for d in device_ids:
            conn = hub.get(d)
            if conn is not None:
                await hub.unregister(conn)
            workflow.drop_session(d)
    _run(trennen())


def _drucke(sock):
    return [m for m in sock.gesendet if m.get("t") == "print"]


def test_druckt_am_terminal_des_haushalts_und_fragt_bei_zweien(client):
    tag = uuid.uuid4().hex[:6]
    a, b = f"A-{tag}", f"B-{tag}"
    ha, hb = _haushalt(client, a), _haushalt(client, b)
    ta, tb = f"ta-{tag}", f"tb-{tag}"
    sock_a, sock_b = _terminal(client, ta, a), _terminal(client, tb, b)
    try:
        r = client.post("/api/labels", json={"name": f"Milch {tag}", "print": True},
                        headers={"X-Haushalt": str(hb)})
        assert r.status_code == 201, r.text
        assert r.json()["printed"]
        assert _drucke(sock_b) and not _drucke(sock_a)

        # Was Terminal A danach abholt, enthaelt nichts von B.
        async def a_holt():
            async with session_scope() as session:
                await workflow.flush_print_queue(session, ta)
        _run(a_holt())
        assert not _drucke(sock_a)

        # Liste fuers Handy: nur das eigene Terminal.
        liste = client.get("/api/labels/terminals", headers={"X-Haushalt": str(ha)}).json()
        assert [t["device_id"] for t in liste] == [ta]

        # Zweites Terminal in A: ohne Wahl wird gefragt, nichts angelegt.
        ta2 = f"ta2-{tag}"
        sock_a2 = _terminal(client, ta2, a)
        try:
            vorher = client.get("/api/inventory", headers={"X-Haushalt": str(ha)}).json()
            r = client.post("/api/labels", json={"name": f"Käse {tag}", "print": True},
                            headers={"X-Haushalt": str(ha)})
            assert r.status_code == 409
            assert {t["device_id"] for t in r.json()["detail"]["terminals"]} == {ta, ta2}
            assert client.get("/api/inventory", headers={"X-Haushalt": str(ha)}).json() == vorher

            r = client.post("/api/labels", json={"name": f"Käse {tag}", "print": True,
                                                 "device_id": ta2},
                            headers={"X-Haushalt": str(ha)})
            assert r.status_code == 201
            assert _drucke(sock_a2) and not _drucke(sock_a)
        finally:
            _weg(ta2)
    finally:
        _weg(ta, tb)


def test_ohne_verbundenes_terminal_wartet_der_auftrag_am_eigenen(client):
    tag = uuid.uuid4().hex[:6]
    a = f"C-{tag}"
    ha = _haushalt(client, a)
    tc = f"tc-{tag}"
    _terminal(client, tc, a)

    _weg(tc)

    r = client.post("/api/labels", json={"name": f"Brot {tag}", "print": True},
                    headers={"X-Haushalt": str(ha)})
    assert r.status_code == 201 and not r.json()["printed"]

    async def auftrag():
        async with session_scope() as session:
            job = (await session.execute(
                select(PrintJob).where(PrintJob.id == r.json()["print_jobs"][0]))).scalar_one()
            pk = await workflow.geraet_pk(session, tc)
            return job.device_pk, pk, job.household
    fest, pk, haushalt = _run(auftrag())
    assert fest == pk and haushalt == a


def test_health_ohne_anmeldung(client):
    from app.config import settings

    object.__setattr__(settings, "ui_password", "x")
    try:
        antwort = client.get("/api/health")
        assert antwort.status_code == 200 and antwort.json() == {"status": "ok"}
    finally:
        object.__setattr__(settings, "ui_password", "")
