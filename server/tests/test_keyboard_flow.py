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
    """Nach einer halben Stunde Ruhe steht zuerst wieder der Lagerort an.

    Sonst laeuft der naechste Einkauf stillschweigend in den Schrank von
    vorhin - ein Fehler, den man erst bemerkt, wenn man das Etikett am
    falschen Regal sucht.
    """
    async def scenario():
        from datetime import datetime, timedelta

        sess = workflow.session_for("kbtest4")
        sess.location = "Kühlschrank"
        sess.stack = [workflow.HOME]

        async with session_scope() as session:
            # Frisch bedient: nichts verfaellt.
            sess.touch()
            assert await workflow.expire_location(session, sess) is False
            assert sess.location == "Kühlschrank"

            # Eine Minute ueber der Ruhezeit.
            sess.last_action = datetime.utcnow() - timedelta(
                seconds=workflow.LOCATION_IDLE_SECONDS + 60
            )
            assert await workflow.expire_location(session, sess) is True
            assert sess.location == ""
            assert sess.current == workflow.LOCATIONS

            # Danach faengt die Ruhezeit von vorn an, statt bei jedem weiteren
            # Tipp erneut auszuloesen.
            assert await workflow.expire_location(session, sess) is False

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
