"""Handscanner: Verbindungsdauer einstellbar, Zustand und Trenngrund lesbar."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import Device, Setting
from app.services import scanner as scanner_info
from app.services import settings_store


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_trenngruende_in_worten():
    assert "Scanner" in scanner_info.trenngrund(0x13)
    assert "Terminal" in scanner_info.trenngrund(0x16)
    assert "Funk" in scanner_info.trenngrund(0x08)
    assert scanner_info.trenngrund(0) == ""
    assert scanner_info.trenngrund(0x99) == "Code 0x99"
    assert scanner_info.zustand({"connected": True, "state": "wartet"}) == "verbunden"
    assert scanner_info.zustand({"state": "wartet"}) == "wartet auf den Scanner"


def test_neue_schluessel_gelten_auch_in_alten_installationen():
    """Ein gespeicherter Bereich ohne den neuen Schluessel bekommt die Vorgabe."""
    async def scenario():
        async with session_scope() as session:
            await settings_store.put(session, "device_ui", {"brightness": 40})
            await session.commit()
            cfg = await settings_store.get(session, "device_ui")
            alle = await settings_store.all_settings(session)
            row = await session.get(Setting, "device_ui")
            await session.delete(row)
            await session.commit()
            return cfg, alle["device_ui"]

    cfg, alle = _run(scenario())
    for werte in (cfg, alle):
        assert werte["brightness"] == 40
        assert werte["scanner_minutes"] == settings_store.DEFAULTS["device_ui"]["scanner_minutes"]
    # DEFAULTS bleibt unberuehrt
    assert settings_store.DEFAULTS["device_ui"]["brightness"] == 80


def test_terminalliste_nennt_zustand_und_trenngrund(client):
    geraet = f"scan-{uuid.uuid4().hex[:6]}"

    async def anlegen():
        async with session_scope() as session:
            session.add(Device(device_id=geraet, telemetry={
                "scanner": {"connected": False, "state": "wartet", "reason": 0x13}}))
            await session.commit()

    _run(anlegen())
    eintrag = next(d for d in client.get("/api/devices").json() if d["device_id"] == geraet)
    sc = eintrag["telemetry"]["scanner"]
    assert sc["state_text"] == "wartet auf den Scanner"
    assert "Energiesparen" in sc["reason_text"]
    client.delete(f"/api/devices/{geraet}")


def test_verbindungsdauer_ist_eine_einstellung(client):
    r = client.patch("/api/settings/device_ui", json={"scanner_minutes": 0})
    assert r.status_code == 200 and r.json()["scanner_minutes"] == 0
    client.patch("/api/settings/device_ui", json={"scanner_minutes": 10})
