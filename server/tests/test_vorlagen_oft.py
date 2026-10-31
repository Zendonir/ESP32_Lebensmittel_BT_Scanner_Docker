"""Oft genutzte Vorlagen (Handy, Einlagern): gezaehlt werden Einlagertage."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import Event, utcnow


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_oft_zaehlt_tage_und_haushalte_getrennt(client):
    name = f"Bratwurst {uuid.uuid4().hex[:6]}"
    hof = f"Hof-{uuid.uuid4().hex[:6]}"
    hid = client.post("/api/households", json={"name": hof}).json()["id"]

    async def anlegen():
        async with session_scope() as session:
            jetzt = utcnow()
            # Ein Einkauf mit drei Packungen, ein zweiter vor einer Woche,
            # einer vor einem Jahr (zaehlt nicht mehr), einer bei anderen.
            for ts in (jetzt, jetzt, jetzt, jetzt - timedelta(days=7), jetzt - timedelta(days=365)):
                session.add(Event(type="add", name=name, household=hof, ts=ts))
            session.add(Event(type="add", name=name, household="Andere", ts=jetzt))
            session.add(Event(type="remove", name=name, household=hof, ts=jetzt))
            await session.commit()
    _run(anlegen())

    assert client.get("/api/templates/oft", headers={"X-Haushalt": str(hid)}).json()[name] == 2
    # Verwaltung ohne Haushalt: alle Haushalte
    assert client.get("/api/templates/oft").json()[name] == 2   # heute zaehlt einmal
