"""Testknoepfe: jede Variante baut einen gueltigen, ein Etikett langen Auftrag."""

from __future__ import annotations

import base64
import json

import pytest

from app.services import druckertest as T


@pytest.mark.parametrize("test", T.TESTS, ids=lambda t: t.id)
def test_jeder_test_baut_einen_auftrag(test):
    job = T.build(test.id, stack_mm=30, line_mm=50)
    assert job["braucht"] == T.FAEHIGKEIT
    for b in job["blocks"]:
        if b["t"] == "raw":
            # Die Firmware entpackt in einen Puffer von 512 Bytes.
            assert 0 < len(base64.b64decode(b["d"])) <= 512
    if test.auffuellen:
        assert sum(T._hoehe(b) for b in job["blocks"]) == 30 * T.DOTS_PER_MM
    assert len(json.dumps(job)) < 12_000


def test_ids_sind_eindeutig_und_katalog_vollstaendig():
    assert len(T.BY_ID) == len(T.TESTS)
    assert {k["id"] for k in T.katalog()} == set(T.BY_ID)


def test_rueckzug_varianten_senden_verschiedene_befehle():
    befehle = set()
    for t in T.TESTS:
        if t.gruppe != "Rückzug":
            continue
        roh = [b for b in T.build(t.id, 30, 50)["blocks"] if b["t"] == "raw"]
        befehle.add(roh[0]["d"])
    assert len(befehle) == sum(t.gruppe == "Rückzug" for t in T.TESTS)


def test_unbekannter_test_gibt_404():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        _pruefen(client)


def _pruefen(client):
    assert client.post("/api/labels/tests/gibtsnicht").status_code == 404
    assert any(t["id"] == "rueck_escj" for t in client.get("/api/labels/tests").json())
