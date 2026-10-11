"""Reconnect: die alte Verbindung darf beim Abbau die neue nicht beschaedigen.

Gefunden am echten Geraet: nach dem Firmware-Update blieben alle
Testauftraege auf "queued" stehen. Die alte Verbindung wurde erst nach dem
hello der neuen abgebaut und warf dabei deren Sitzung samt Faehigkeiten weg.
"""

from __future__ import annotations

import asyncio

from app.device import workflow
from app.device.hub import hub


class _Socket:
    async def send_text(self, payload: str) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_spaeter_abbau_der_alten_verbindung_laesst_die_neue_stehen():
    async def scenario():
        geraet = "reconnect-test"
        alt = await hub.register(geraet, _Socket())
        sitzung_alt = workflow.new_session(geraet)

        neu = await hub.register(geraet, _Socket())
        sitzung_neu = workflow.new_session(geraet)
        sitzung_neu.caps = {"raster2", "baud", "raw"}

        # Jetzt erst raeumt die alte Verbindung auf - wie in routes.py.
        if await hub.unregister(alt):
            workflow.drop_session(geraet, sitzung_alt)

        try:
            assert hub.get(geraet) is neu
            assert workflow.session_for(geraet) is sitzung_neu
            assert workflow.session_for(geraet).caps == {"raster2", "baud", "raw"}
        finally:
            assert await hub.unregister(neu)
            workflow.drop_session(geraet, sitzung_neu)

    _run(scenario())


def test_veraltete_sitzung_wird_nicht_verworfen():
    geraet = "reconnect-test-2"
    alt = workflow.new_session(geraet)
    neu = workflow.new_session(geraet)
    workflow.drop_session(geraet, alt)
    assert workflow.session_for(geraet) is neu
    workflow.drop_session(geraet, neu)
    assert workflow.session_for(geraet) is not neu
    workflow.drop_session(geraet)
