"""Haushalte: jeder sieht seins, Terminals lagern in ihren ein, Fremdentnahmen
werden beim Besitzer vermerkt."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.device import workflow
from app.main import app
from app.models import Device
from app.services import inventory as inv


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _neu(client, name):
    r = client.post("/api/households", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _anlegen(client, hid, name):
    r = client.post("/api/labels", json={"name": name, "expiry_date": "2027-01-01",
                                          "print": False}, headers={"X-Haushalt": str(hid)})
    assert r.status_code == 201, r.text
    return r.json()["labels"][0] if "labels" in r.json() else r.json()["items"][0]["label"]


def test_jeder_sieht_nur_seins_und_kann_ausdruecklich_ueberall_suchen(client):
    tag = uuid.uuid4().hex[:6]
    a, b = _neu(client, f"A-{tag}"), _neu(client, f"B-{tag}")
    _anlegen(client, a, f"Apfelmus {tag}")
    _anlegen(client, b, f"Birnen {tag}")

    nur_a = client.get("/api/inventory", headers={"X-Haushalt": str(a)}).json()
    namen = {i["name"] for i in nur_a}
    assert f"Apfelmus {tag}" in namen and f"Birnen {tag}" not in namen

    # "alle" wirkt nur mit Suchbegriff
    ohne_q = client.get("/api/inventory?alle=true", headers={"X-Haushalt": str(a)}).json()
    assert f"Birnen {tag}" not in {i["name"] for i in ohne_q}
    mit_q = client.get(f"/api/inventory?alle=true&q=Birnen {tag}",
                       headers={"X-Haushalt": str(a)}).json()
    assert [i["household"] for i in mit_q] == [f"B-{tag}"]

    # Ohne Haushalt (Verwaltung): alles
    alle = {i["name"] for i in client.get("/api/inventory").json()}
    assert {f"Apfelmus {tag}", f"Birnen {tag}"} <= alle


def test_fremdentnahme_steht_beim_besitzer(client):
    tag = uuid.uuid4().hex[:6]
    a, b = _neu(client, f"Fa-{tag}"), _neu(client, f"Fb-{tag}")
    label = _anlegen(client, b, f"Gulasch {tag}")

    r = client.post("/api/inventory/remove", json={"label": label, "reason": "web"},
                    headers={"X-Haushalt": str(a)})
    assert r.status_code == 200, r.text

    bei_b = client.get("/api/households/fremd", headers={"X-Haushalt": str(b)}).json()
    assert [(e["label"], e["fremd"]) for e in bei_b["bei_uns"]] == [(label, f"Fa-{tag}")]
    von_a = client.get("/api/households/fremd", headers={"X-Haushalt": str(a)}).json()
    assert [e["label"] for e in von_a["von_uns"]] == [label]

    # Die eigene Entnahme ist keine Fremdentnahme
    eigen = _anlegen(client, a, f"Eigenes {tag}")
    client.post("/api/inventory/remove", json={"label": eigen, "reason": "web"},
                headers={"X-Haushalt": str(a)})
    bei_a = client.get("/api/households/fremd", headers={"X-Haushalt": str(a)}).json()
    assert eigen not in [e["label"] for e in bei_a["bei_uns"]]


def test_terminal_lagert_in_seinen_haushalt_ein(client):
    tag = uuid.uuid4().hex[:6]
    _neu(client, f"T-{tag}")
    geraet = f"term-{tag}"

    async def anlegen_und_pruefen():
        async with session_scope() as session:
            session.add(Device(device_id=geraet, name="Küche"))
            await session.commit()
        r = client.patch(f"/api/devices/{geraet}", json={"household": f"T-{tag}"})
        assert r.status_code == 200, r.text
        sess = workflow.session_for(geraet)
        assert sess.household == f"T-{tag}"
        async with session_scope() as session:
            items, _ = await inv.add_items(session, name=f"Suppe {tag}", want_print=False,
                                           household=sess.household)
            await session.commit()
            return items[0].household

    try:
        assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            anlegen_und_pruefen()) == f"T-{tag}"
    finally:
        workflow.drop_session(geraet)


def test_seiten_je_haushalt_und_auswahl(client):
    tag = uuid.uuid4().hex[:6]
    hid = _neu(client, f"Seite-{tag}")
    seite = client.get(f"/h/{hid}/").text
    assert f'name="haushalt" content="{hid}"' in seite
    assert f'href="/h/{hid}/manifest.json"' in seite
    mobil = client.get(f"/h/{hid}/mobile").text
    assert f'href="/h/{hid}/?desktop=1"' in mobil

    manifest = client.get(f"/h/{hid}/manifest.json").json()
    assert manifest["start_url"] == f"/h/{hid}/mobile"
    assert manifest["scope"] == f"/h/{hid}/"

    # Mehrere Haushalte: die Startseite fragt
    start = client.get("/").text
    assert "Welcher Haushalt?" in start and f'href="/h/{hid}/"' in start
    assert "Welcher Haushalt?" not in client.get("/?alle=1").text
    assert client.get("/h/999999/").status_code == 404


def test_umbenennen_zieht_nach_und_loeschen_nur_wenn_leer(client):
    tag = uuid.uuid4().hex[:6]
    hid = _neu(client, f"Alt-{tag}")
    label = _anlegen(client, hid, f"Reis {tag}")
    r = client.patch(f"/api/households/{hid}", json={"name": f"Neu-{tag}"})
    assert r.status_code == 200
    assert client.get(f"/api/inventory/{label}").json()["household"] == f"Neu-{tag}"
    assert client.delete(f"/api/households/{hid}").status_code == 409
    client.post("/api/inventory/remove", json={"label": label, "reason": "web"})
    assert client.delete(f"/api/households/{hid}").status_code == 200
