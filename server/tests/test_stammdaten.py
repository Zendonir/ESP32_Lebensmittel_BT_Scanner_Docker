"""Umbenennen in den Stammdaten muss bei den Artikeln ankommen.

Kategorie und Lagerort stehen an Artikeln, Vorlagen und Produkten als Text,
nicht als Verweis. Wer in den Stammdaten "Getraenke" in "Getränke" umbenannte,
aenderte bisher nur die Stammdatenzeile - die Artikel trugen weiter den alten
Namen. Sie fielen damit aus dem Kategoriefilter, standen in der Uebersicht als
eigene, farblose Gruppe, und am Terminal lebte die alte Kategorie in den
Vorlagen weiter. Der naheliegende Weg, einen Namen zu korrigieren, hat also
still Daten auseinandergerissen.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _eindeutig(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def test_kategorie_umbenennen_zieht_artikel_und_vorlagen_mit(client):
    alt, neu = _eindeutig("Getraenke"), _eindeutig("Getränke")
    cat = client.post("/api/categories", json={"name": alt}).json()
    label = client.post(
        "/api/labels", json={"name": "Apfelsaft", "category": alt, "print": False}
    ).json()["labels"][0]
    tpl = client.post(
        "/api/templates", json={"name": _eindeutig("Saft"), "category": alt}
    ).json()

    r = client.put(f"/api/categories/{cat['id']}", json={"name": neu, "color": cat["color"]})
    assert r.status_code == 200

    assert client.get(f"/api/inventory/{label}").json()["category"] == neu
    vorlagen = {t["id"]: t for t in client.get("/api/templates").json()}
    assert vorlagen[tpl["id"]]["category"] == neu
    # Der Filter findet den Artikel unter dem neuen Namen.
    gefunden = client.get("/api/inventory", params={"category": neu}).json()
    assert [i["label"] for i in gefunden] == [label]


def test_lagerort_umbenennen_zieht_artikel_mit(client):
    alt, neu = _eindeutig("Kuehlschrank"), _eindeutig("Kühlschrank")
    loc = client.post("/api/locations", json={"name": alt}).json()
    label = client.post(
        "/api/labels", json={"name": "Butter", "location": alt, "print": False}
    ).json()["labels"][0]

    r = client.put(f"/api/locations/{loc['id']}", json={"name": neu})
    assert r.status_code == 200
    assert client.get(f"/api/inventory/{label}").json()["location"] == neu


def test_umbenennen_auf_vorhandenen_namen_wird_abgewiesen(client):
    """Kein Serverfehler, sondern eine verstaendliche Absage."""
    a = client.post("/api/categories", json={"name": _eindeutig("A")}).json()
    b = client.post("/api/categories", json={"name": _eindeutig("B")}).json()
    r = client.put(f"/api/categories/{a['id']}", json={"name": b["name"]})
    assert r.status_code == 409
    assert "existiert" in r.json()["detail"]


# --------------------------------------------------------- Umschriften nachziehen
#
# Eigene, leere Datenbank im Speicher: die gemeinsame Testdatenbank ist schon
# mit den neuen Namen befuellt, dort gaebe es nichts nachzuziehen.
async def _frische_sitzung():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_alte_umschriften_werden_einmal_nachgezogen():
    from sqlalchemy import select

    from app.models import Category, InventoryItem, Template
    from app.services import seed

    engine, maker = await _frische_sitzung()
    async with maker() as s:
        s.add_all([
            Category(name="Getraenke"), Category(name="Tiefkuehl"),
            InventoryItem(label="LEB900001", name="Wasser", category="Getraenke"),
            Template(name="Gebaeck", category="Backwaren", brands=[], sorten=[]),
        ])
        await s.commit()

        await seed.run(s)

        namen = set((await s.execute(select(Category.name))).scalars())
        assert {"Getränke", "Tiefkühl"} <= namen
        assert not {"Getraenke", "Tiefkuehl"} & namen
        item = (await s.execute(select(InventoryItem))).scalar_one()
        assert item.category == "Getränke"
        assert (await s.execute(select(Template.name))).scalar_one() == "Gebäck"

        # Ein zweiter Start aendert nichts mehr.
        await seed.run(s)
        assert set((await s.execute(select(Category.name))).scalars()) == namen
    await engine.dispose()


@pytest.mark.asyncio
async def test_bewusst_angelegtes_ziel_bleibt_unberuehrt():
    """Gibt es "Getränke" schon, wird nicht zusammengelegt - das waere eine
    Entscheidung, die der Server nicht treffen soll."""
    from sqlalchemy import select

    from app.models import Category
    from app.services import seed

    engine, maker = await _frische_sitzung()
    async with maker() as s:
        s.add_all([Category(name="Getraenke"), Category(name="Getränke")])
        await s.commit()
        await seed.run(s)
        namen = set((await s.execute(select(Category.name))).scalars())
        assert {"Getraenke", "Getränke"} <= namen
    await engine.dispose()
