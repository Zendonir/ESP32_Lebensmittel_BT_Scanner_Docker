"""Vorlage mit Haken "Sorte abfragen": Fleischsorte waehlen, landet auf dem Etikett."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.device import workflow
from app.device.hub import hub
from app.main import app
from app.services import labels


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _vorlage(client, name, use_sorten, sorten=()):
    r = client.post("/api/templates", json={"name": name, "category": "Fleisch & Fisch",
                                            "use_sorten": use_sorten, "sorten": list(sorten)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_mit_haken_wird_die_sorte_gefragt_und_gedruckt(client, monkeypatch):
    async def senden(device_id, msg):
        return None
    monkeypatch.setattr(hub, "send_to", senden)

    tag = uuid.uuid4().hex[:6]
    tpl = _vorlage(client, f"Filet {tag}", True)   # ohne eigene Liste: die der Kategorie
    sess = workflow.session_for(f"sorte-{tag}")
    sess.location = "Kühlschrank"

    async def ablauf():
        async with session_scope() as session:
            sess.stack = [workflow.HOME, workflow.TMPL_PRODUCT]
            sess.draft = workflow.Draft()
            await workflow.on_tap(session, sess, f"tpl:{tpl}")
            frage = await workflow.render(session, sess)
            await workflow.on_tap(session, sess, "sorte:Schwein")
            return frage, sess.current, sess.draft.subcategory
    frage, danach, sorte = _run(ablauf())
    assert [i["label"] for i in frage["items"]][:5] == ["Schwein", "Geflügel", "Rind", "Kalb", "Lamm"]
    assert sorte == "Schwein" and danach == workflow.ENTER_DATE   # nicht noch einmal fragen

    titel = labels._title({"name": f"Filet {tag}", "subcategory": "Schwein"})
    assert titel == f"Filet {tag} - Schwein"
    workflow.drop_session(f"sorte-{tag}")


def test_ohne_haken_wird_nicht_gefragt(client, monkeypatch):
    async def senden(device_id, msg):
        return None
    monkeypatch.setattr(hub, "send_to", senden)

    tag = uuid.uuid4().hex[:6]
    tpl = _vorlage(client, f"Aufschnitt {tag}", False)
    sess = workflow.session_for(f"ohne-{tag}")
    sess.location = "Kühlschrank"

    async def ablauf():
        async with session_scope() as session:
            sess.stack = [workflow.HOME, workflow.TMPL_PRODUCT]
            sess.draft = workflow.Draft()
            await workflow.on_tap(session, sess, f"tpl:{tpl}")
            return sess.current
    assert _run(ablauf()) == workflow.ENTER_DATE
    workflow.drop_session(f"ohne-{tag}")
