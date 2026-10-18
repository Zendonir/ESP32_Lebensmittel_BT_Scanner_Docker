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
from app.services.dates import shift_iso


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
    assert sorte == "Schwein" and danach == workflow.ENTER_QTY    # nicht noch einmal fragen, kein MHD

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
    assert _run(ablauf()) == workflow.ENTER_QTY
    workflow.drop_session(f"ohne-{tag}")


def test_vorlage_fragt_kein_mhd_sondern_nimmt_den_ort(client, monkeypatch):
    async def senden(device_id, msg):
        return None
    monkeypatch.setattr(hub, "send_to", senden)

    tag = uuid.uuid4().hex[:6]
    tpl = client.post("/api/templates", json={"name": f"Hack {tag}", "category": "Fleisch & Fisch",
                                              "haltbarkeit": {"kuehl": 2, "tiefkuehl": 90}}).json()["id"]
    for name, art in ((f"Kühl {tag}", "kuehl"), (f"Truhe {tag}", "tiefkuehl")):
        assert client.post("/api/locations", json={"name": name, "kind": art}).status_code == 201
    sess = workflow.session_for(f"mhd-{tag}")

    async def ablauf():
        async with session_scope() as session:
            sess.stack = [workflow.HOME, workflow.TMPL_PRODUCT]
            sess.draft = workflow.Draft()
            # Ohne Ort wird zuerst der Ort gefragt, danach geht es weiter.
            await workflow.on_tap(session, sess, f"tpl:{tpl}")
            gefragt = sess.current
            await workflow.on_tap(session, sess, f"loc:Kühl {tag}")
            kuehl = sess.current, sess.draft.expiry_date
            # Ort unterwegs gewechselt: beim Speichern zaehlt der neue.
            sess.location = f"Truhe {tag}"
            await workflow.on_tap(session, sess, "ok")
            return gefragt, kuehl, sess.current, sess.draft.expiry_date
    gefragt, (danach, mhd_kuehl), ende, mhd_truhe = _run(ablauf())
    assert gefragt == workflow.LOCATIONS
    assert danach == workflow.ENTER_QTY and mhd_kuehl == shift_iso(2)
    assert ende == workflow.RESULT and mhd_truhe == shift_iso(90)
    workflow.drop_session(f"mhd-{tag}")


def test_menge_je_etikett_kommt_ueber_den_ziffernblock(client, monkeypatch):
    async def senden(device_id, msg):
        return None
    monkeypatch.setattr(hub, "send_to", senden)

    tag = uuid.uuid4().hex[:6]
    tpl = client.post("/api/templates", json={"name": f"Würstchen {tag}", "category": "Fleisch & Fisch",
                                              "unit": "St."}).json()["id"]
    sess = workflow.session_for(f"pad-{tag}")
    sess.location = "Kühlschrank"

    async def ablauf():
        async with session_scope() as session:
            sess.stack = [workflow.HOME, workflow.TMPL_PRODUCT]
            sess.draft = workflow.Draft()
            await workflow.on_tap(session, sess, f"tpl:{tpl}")
            menge = await workflow.render(session, sess)
            await workflow.on_input(session, sess, "3")
            await workflow.on_tap(session, sess, "ok")
            anzahl = await workflow.render(session, sess)
            return menge, anzahl, sess.draft.quantity
    menge, anzahl, wert = _run(ablauf())
    assert menge["kind"] == "number" and menge["meta"]["pad"] is True and menge["meta"]["unit"] == "St."
    assert wert == 3.0
    # Die Etikettenzahl ist etwas anderes: zwei Packungen zu je 3 Stueck.
    assert anzahl["title"] == "Anzahl Etiketten" and "Menge 3 St." in anzahl["lines"]
    workflow.drop_session(f"pad-{tag}")
