"""Wochenplan ("Was muss weg?"), Verbrauchsstatistik, Lagerorte in den Zahlen."""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import InventoryItem, utcnow
from app.services import verbrauch


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _haushalt(client):
    hof = f"Hof-{uuid.uuid4().hex[:6]}"
    hid = client.post("/api/households", json={"name": hof}).json()["id"]
    return hof, {"X-Haushalt": str(hid)}


def _artikel(hof, name, tage=None, **kw):
    mhd = (date.today() + timedelta(days=tage)).isoformat() if tage is not None else ""
    return InventoryItem(label=uuid.uuid4().hex[:10], name=name, household=hof,
                         expiry_date=mhd, location=kw.pop("location", "Kühlschrank"), **kw)


def test_eimer_nach_tagen():
    montag = date(2026, 11, 2)              # ein Montag
    assert verbrauch._eimer(-1, montag) == ("abgelaufen", "Abgelaufen")
    assert verbrauch._eimer(0, montag)[0] == "heute"
    assert verbrauch._eimer(1, montag)[0] == "morgen"
    assert verbrauch._eimer(2, montag) == ("tag-2", "Mittwoch, 04.11.")
    assert verbrauch._eimer(6, montag) == ("tag-6", "Sonntag, 08.11.")
    assert verbrauch._eimer(7, montag)[0] == "naechste"
    # Am Samstag ist uebermorgen schon naechste Woche
    assert verbrauch._eimer(2, date(2026, 11, 7))[0] == "naechste"


def test_wochenplan_gruppiert_und_filtert(client):
    hof, kopf = _haushalt(client)

    async def anlegen():
        async with session_scope() as session:
            session.add_all([
                _artikel(hof, "Alt", -2), _artikel(hof, "Joghurt", 0), _artikel(hof, "Milch", 1),
                _artikel(hof, "Weit weg", 40), _artikel(hof, "Ohne MHD"),
                _artikel(hof, "Weg", 0, status="removed"),
                _artikel("Andere", "Fremd", 0),
            ])
            await session.commit()
    _run(anlegen())

    plan = client.get("/api/inventory/wochenplan", headers=kopf).json()
    keys = [g["key"] for g in plan]
    assert keys[:3] == ["abgelaufen", "heute", "morgen"]
    namen = {g["key"]: [i["name"] for i in g["items"]] for g in plan}
    assert namen["heute"] == ["Joghurt"] and namen["abgelaufen"] == ["Alt"]
    alle = [n for liste in namen.values() for n in liste]
    assert "Weit weg" not in alle and "Ohne MHD" not in alle and "Weg" not in alle
    assert "Fremd" not in alle
    assert plan[1]["items"][0]["days_left"] == 0


def test_frist_filter(client):
    hof, kopf = _haushalt(client)

    async def anlegen():
        async with session_scope() as session:
            session.add_all([_artikel(hof, "Alt", -1), _artikel(hof, "Bald", 2),
                             _artikel(hof, "Spaeter", 60)])
            await session.commit()
    _run(anlegen())

    namen = lambda frist: [i["name"] for i in client.get(  # noqa: E731
        f"/api/inventory?frist={frist}", headers=kopf).json()]
    assert namen("abgelaufen") == ["Alt"]
    assert namen("bald") == ["Bald"]
    assert client.get("/api/inventory?frist=quatsch").status_code == 422


def test_statistik(client):
    hof, kopf = _haushalt(client)
    heute = date.today()

    async def anlegen():
        async with session_scope() as session:
            jetzt = utcnow()
            vor = lambda t: (heute - timedelta(days=t)).isoformat()  # noqa: E731
            session.add_all([
                # rechtzeitig verbraucht, 4 Tage gelagert
                _artikel(hof, "Milch", 3, status="removed", removed_at=jetzt,
                         removed_reason="scan", added_date=vor(4)),
                _artikel(hof, "Milch", 3, status="removed", removed_at=jetzt,
                         removed_reason="scan", added_date=vor(6)),
                # nach dem MHD herausgenommen
                _artikel(hof, "Salat", -2, status="removed", removed_at=jetzt,
                         removed_reason="mobile", added_date=vor(10)),
                # ausdruecklich weggeworfen (MHD noch nicht erreicht)
                _artikel(hof, "Salat", 1, status="removed", removed_at=jetzt,
                         removed_reason="weggeworfen", added_date=vor(2)),
                # Import und zu alt zaehlen nicht, aktiv sowieso nicht
                _artikel(hof, "Import", -100, status="removed", removed_at=jetzt,
                         removed_reason="import"),
                _artikel(hof, "Uralt", -400, status="removed",
                         removed_at=jetzt - timedelta(days=300), removed_reason="scan"),
                _artikel(hof, "Aktiv", 5, added_date=heute.isoformat()),
            ])
            await session.commit()
    _run(anlegen())

    s = client.get("/api/inventory/verbrauch?tage=90", headers=kopf).json()
    assert s["entnommen"] == 4
    assert (s["rechtzeitig"], s["nach_mhd"], s["weggeworfen"]) == (2, 1, 1)
    assert s["verlust_prozent"] == 50
    assert s["lagerdauer_tage"] == 5                      # Median aus 2, 4, 6, 10
    assert s["oft_verloren"][0] == {"name": "Salat", "anzahl": 2, "von": 2}
    assert {"name": "Milch", "anzahl": 2} in s["oft_verbraucht"]
    monat = heute.strftime("%Y-%m")
    zeile = next(m for m in s["monate"] if m["monat"] == monat)
    assert zeile["entnommen"] == 4 and zeile["eingelagert"] >= 1
    assert len(s["monate"]) == 3
    assert client.get("/api/inventory/verbrauch?tage=1").status_code == 422


def test_stats_orte(client):
    hof, kopf = _haushalt(client)

    async def anlegen():
        async with session_scope() as session:
            session.add_all([_artikel(hof, "A", -1, location="Keller"),
                             _artikel(hof, "B", 1, location="Keller"),
                             _artikel(hof, "C", 50, location="Keller"),
                             _artikel(hof, "D", location="Schrank")])
            await session.commit()
    _run(anlegen())

    orte = client.get("/api/inventory/stats", headers=kopf).json()["orte"]
    assert orte[0] == {"name": "Keller", "anzahl": 3, "abgelaufen": 1, "laeuft_ab": 1}
    assert orte[1] == {"name": "Schrank", "anzahl": 1, "abgelaufen": 0, "laeuft_ab": 0}
