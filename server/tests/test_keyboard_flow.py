"""Tastatur-Bildschirme: eigener Produktname und neuer Lagerort."""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("DEVICE_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/test_keyboard.db"
)

from app.db import session_scope  # noqa: E402
from app.device import workflow  # noqa: E402
from app.main import app  # noqa: E402  (triggert init_db beim Testlauf ueber TestClient)
from fastapi.testclient import TestClient  # noqa: E402

with TestClient(app):
    pass


def test_unbekannter_barcode_eigener_name():
    async def scenario():
        sess = workflow.session_for("kbtest1")
        sess.location = "Kuehlschrank"

        async with session_scope() as session:
            sess.draft.barcode = "9999000001"
            sess.stack = [workflow.HOME, workflow.UNKNOWN]

            await workflow.on_tap(session, sess, "name")
            assert sess.current == workflow.UNKNOWN_NAME

            await workflow.on_input(session, sess, "Selbstgemachte Marmelade")
            assert sess.draft.name == "Selbstgemachte Marmelade"

            await workflow.on_tap(session, sess, "ok")
            assert sess.current == workflow.ENTER_DATE
            assert sess.draft.name == "Selbstgemachte Marmelade"

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())


def test_leerer_name_bleibt_auf_der_tastatur():
    async def scenario():
        sess = workflow.session_for("kbtest2")
        async with session_scope() as session:
            sess.stack = [workflow.HOME, workflow.UNKNOWN_NAME]
            sess.draft.name = ""
            await workflow.on_tap(session, sess, "ok")
            assert sess.current == workflow.UNKNOWN_NAME

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())


def test_lagerort_laesst_sich_am_geraet_nur_waehlen():
    """Am Geraet wird kein Lagerort mehr angelegt, nur ausgewaehlt.

    Auf einer Bildschirmtastatur tippt man sich schnell "Kuelschrank" ein und
    hat den Bestand ab da auf zwei Orte verteilt, ohne es zu merken. Angelegt
    wird deshalb nur noch in der Verwaltung.
    """
    async def scenario():
        from sqlalchemy import select

        from app.models import Location

        sess = workflow.session_for("kbtest3")
        async with session_scope() as session:
            vorher = len((await session.execute(select(Location))).scalars().all())

            sess.stack = [workflow.HOME, workflow.LOCATIONS]
            sess.touch()
            screen = await workflow.render(session, sess)
            # Keine Kachel, die einen neuen Ort anlegen wuerde.
            assert all(item["id"].startswith("loc:") for item in screen["items"])

            # Und auch der alte Weg dorthin fuehrt nirgendwo hin.
            await workflow.on_tap(session, sess, "new")
            assert sess.current == workflow.LOCATIONS

        async with session_scope() as session:
            nachher = len((await session.execute(select(Location))).scalars().all())
            assert nachher == vorher

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())


def test_lagerort_verfaellt_nach_ruhe_und_wird_neu_erfragt():
    """Nach einer halben Stunde ohne Erfassen gilt der Lagerort nicht mehr.

    Sonst laeuft der naechste Einkauf stillschweigend in den Schrank von
    vorhin - ein Fehler, den man erst bemerkt, wenn man das Etikett am
    falschen Regal sucht. Gezaehlt wird ab dem letzten Erfassen; blosses
    Antippen haelt den Ort nicht am Leben.
    """
    async def scenario():
        from datetime import timedelta

        from app.models import utcnow

        sess = workflow.session_for("kbtest4")
        sess.location = "Kühlschrank"
        sess.stack = [workflow.HOME]

        async with session_scope() as session:
            sess.erfasst()
            assert await workflow.expire_location(session, sess) is False
            assert sess.location == "Kühlschrank"

            # Eine Minute ueber der Zeit - Antippen dazwischen zaehlt nicht.
            sess.last_erfasst = utcnow() - timedelta(
                seconds=workflow.LOCATION_IDLE_SECONDS + 60
            )
            sess.touch()
            assert await workflow.expire_location(session, sess) is True
            assert sess.location == ""
            # Danach ist nichts mehr zu verwerfen.
            assert await workflow.expire_location(session, sess) is False

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())


def test_scan_ohne_gueltigen_ort_fragt_und_laeuft_danach_weiter(monkeypatch):
    """Scan nach langer Pause: erst Ortswahl mit eigenem Ton, dann der Scan."""
    from datetime import timedelta

    from app.device import protocol as proto
    from app.device.hub import hub
    from app.models import utcnow
    from app.services import openfoodfacts

    gesendet = []

    async def senden(device_id, msg):
        gesendet.append(msg)

    async def nachschlagen(session, code):
        return None

    monkeypatch.setattr(hub, "send_to", senden)
    monkeypatch.setattr(openfoodfacts, "lookup", nachschlagen)

    async def scenario():
        sess = workflow.session_for("kbtest6")
        sess.location = "Kühlschrank"
        sess.stack = [workflow.HOME]
        sess.last_erfasst = utcnow() - timedelta(seconds=workflow.LOCATION_IDLE_SECONDS + 5)

        async with session_scope() as session:
            await workflow.on_scan(session, sess, "4000000000017")
            assert sess.current == workflow.LOCATIONS
            assert sess.pending == "scan:4000000000017"
            assert proto.beep("ort") in gesendet

            # Ort waehlen: der gemerkte Scan laeuft weiter (unbekanntes Produkt).
            await workflow.on_tap(session, sess, "loc:Keller")
            assert sess.location == "Keller" and sess.pending == ""
            assert sess.current == workflow.UNKNOWN
            assert sess.draft.barcode == "4000000000017"
            assert sess.erfasst_vor() < 5

            # Gleich noch ein Scan: kein erneutes Fragen.
            gesendet.clear()
            sess.reset()
            await workflow.on_scan(session, sess, "4000000000024")
            assert sess.current == workflow.UNKNOWN
            assert proto.beep("ort") not in gesendet

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())


def test_ortswahl_abbrechen_verwirft_den_gemerkten_scan(monkeypatch):
    from app.device.hub import hub

    async def senden(device_id, msg):
        return None

    monkeypatch.setattr(hub, "send_to", senden)

    async def scenario():
        sess = workflow.session_for("kbtest7")
        sess.location = ""
        sess.stack = [workflow.HOME]
        async with session_scope() as session:
            await workflow.on_scan(session, sess, "4000000000031")
            assert sess.pending
            await workflow.on_tap(session, sess, "back")
            assert sess.pending == "" and sess.current == workflow.HOME

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
