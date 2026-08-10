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


def test_neuer_lagerort_wird_angelegt_und_aktiviert():
    async def scenario():
        from sqlalchemy import select

        from app.models import Location

        sess = workflow.session_for("kbtest3")
        async with session_scope() as session:
            sess.stack = [workflow.HOME, workflow.LOCATIONS, workflow.LOCATION_NEW]
            await workflow.on_input(session, sess, "Kellerregal")
            assert sess.location_draft == "Kellerregal"

            await workflow.on_tap(session, sess, "ok")
            assert sess.current == workflow.LOCATIONS
            assert sess.location == "Kellerregal"

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Location).where(Location.name == "Kellerregal")
                )
            ).scalar_one_or_none()
            assert row is not None

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())


def test_doppelter_lagerort_erzeugt_keine_zweite_zeile():
    async def scenario():
        from sqlalchemy import func, select

        from app.models import Location

        sess = workflow.session_for("kbtest4")
        async with session_scope() as session:
            sess.stack = [workflow.HOME, workflow.LOCATIONS, workflow.LOCATION_NEW]
            await workflow.on_input(session, sess, "Kellerregal")
            await workflow.on_tap(session, sess, "ok")

        async with session_scope() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Location)
                    .where(Location.name == "Kellerregal")
                )
            ).scalar_one()
            assert count == 1

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
