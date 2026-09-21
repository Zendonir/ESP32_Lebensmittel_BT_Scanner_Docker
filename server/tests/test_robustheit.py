"""Tests fuer die Stellen, an denen der Server frueher stehenblieb.

Alle drei Faelle haben gemeinsam, dass nichts abstuerzt und nichts im Log
steht - der Dienst laeuft weiter und tut nur das Falsche oder gar nichts.
Genau solche Fehler faellt sonst erst Monate spaeter jemandem auf.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("DEVICE_TOKEN", "test-token")
_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/robust.db"

from datetime import timedelta  # noqa: E402

from sqlalchemy import select, text  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.device import hub as hub_module  # noqa: E402
from app.device import workflow  # noqa: E402
from app.device.hub import DeviceConnection, hub  # noqa: E402
from app.models import PrintJob, utcnow  # noqa: E402


def _run(coro):
    """Eigene Schleife je Test - wie in test_flow.py."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------- Datenbank
def test_sqlite_pragmas_gelten_auf_jeder_verbindung():
    """Nicht nur auf der einen Verbindung aus init_db().

    `foreign_keys` und `synchronous` sind Verbindungssache. Sie standen
    frueher allein in init_db() - und galten damit fuer keine der
    Verbindungen, ueber die der laufende Betrieb geht. Ein geloeschtes Geraet
    liess seine Druckauftraege mit einem Verweis ins Leere stehen, obwohl das
    Modell ondelete="CASCADE" sagt.
    """

    async def scenario():
        await init_db()
        async with session_scope() as session:
            fk = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
            busy = (await session.execute(text("PRAGMA busy_timeout"))).scalar()
            journal = (await session.execute(text("PRAGMA journal_mode"))).scalar()
        return fk, busy, journal

    fk, busy, journal = _run(scenario())
    assert fk == 1
    assert busy == 5000
    assert journal == "wal"


# --------------------------------------------------------- Sendeverbindungen
class HaengenderSocket:
    """Gegenstelle, die die Verbindung offen haelt, aber nichts mehr abnimmt.

    Das ist kein erfundener Fall: ein Browser-Tab auf einem eingeschlafenen
    Rechner und ein Terminal, dessen WLAN mitten im Frame weg ist, sehen von
    hier aus genau so aus.
    """

    async def send_text(self, _payload: str) -> None:
        await asyncio.sleep(3600)


class ZaehlenderSocket:
    def __init__(self) -> None:
        self.gesendet: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.gesendet.append(payload)


def test_haengendes_geraet_wird_aufgegeben(monkeypatch):
    """send() kehrt zurueck, statt den ganzen Ablauf festzuhalten."""

    async def scenario():
        monkeypatch.setattr(hub_module, "SEND_TIMEOUT_S", 0.05)
        conn = DeviceConnection("haenger", HaengenderSocket())
        begonnen = asyncio.get_running_loop().time()
        ok = await conn.send({"t": "ping"})
        gedauert = asyncio.get_running_loop().time() - begonnen
        return ok, conn.alive, gedauert

    ok, alive, gedauert = _run(scenario())
    assert ok is False
    assert alive is False
    assert gedauert < 2.0


def test_haengender_browser_blockiert_die_uebrigen_nicht(monkeypatch):
    """Ein klemmender Tab darf den Scan am Terminal nicht aufhalten.

    notify_ui() wird mitten in der Verarbeitung eines Scans abgewartet. Lief
    es der Reihe nach und ohne Zeitgrenze, stand dort jemand vor dem Geraet
    und wartete auf einen Piep, den ein fremder Browser blockierte.
    """

    async def scenario():
        monkeypatch.setattr(hub_module, "SEND_TIMEOUT_S", 0.05)
        haenger = HaengenderSocket()
        gesund = ZaehlenderSocket()
        await hub.add_ui(haenger)
        await hub.add_ui(gesund)
        try:
            begonnen = asyncio.get_running_loop().time()
            await hub.notify_ui("inventory")
            gedauert = asyncio.get_running_loop().time() - begonnen
            return gedauert, len(gesund.gesendet), hub.ui_count()
        finally:
            await hub.remove_ui(haenger)
            await hub.remove_ui(gesund)

    gedauert, empfangen, verbleibend = _run(scenario())
    assert empfangen == 1          # der gesunde Browser wurde bedient
    assert gedauert < 2.0          # und musste nicht auf den haengenden warten
    assert verbleibend == 1        # der haengende wurde ausgetragen


# ------------------------------------------------------- Druckwarteschlange
async def _schlange_leeren() -> None:
    async with session_scope() as session:
        for job in (await session.execute(select(PrintJob))).scalars().all():
            await session.delete(job)
        await session.commit()


def test_druckwarteschlange_schickt_in_haeppchen_und_rueckt_nach():
    """Mehr Auftraege als das Geraet fasst, duerfen nicht verbrennen.

    Die Warteschlange in der Firmware fasst acht. Frueher gingen zwanzig auf
    einmal hinaus, jeder abgelehnte zaehlte als Versuch, und nach fuenf
    Durchgaengen stand ein voellig gesunder Auftrag auf "failed" - ohne je
    gedruckt worden zu sein. Und was nicht in den ersten Schwung passte, blieb
    liegen, bis zufaellig etwas anderes einen neuen Durchgang ausloeste.
    """

    async def scenario():
        await init_db()
        await _schlange_leeren()
        socket = ZaehlenderSocket()
        conn = await hub.register("druckgeraet", socket)
        try:
            async with session_scope() as session:
                for i in range(10):
                    session.add(
                        PrintJob(
                            label=f"LEB{i:06d}",
                            payload={"chars": 32, "blocks": []},
                            status="queued",
                        )
                    )
                await session.commit()

                erster = await workflow.flush_print_queue(session, "druckgeraet")
                nach_erstem = len(socket.gesendet)

                # Das Geraet meldet den ersten Auftrag als gedruckt - daraufhin
                # muss genau einer nachruecken.
                unterwegs = (
                    await session.execute(
                        select(PrintJob)
                        .where(PrintJob.status == "sent")
                        .order_by(PrintJob.id)
                    )
                ).scalars().all()
                await workflow.on_print_result(
                    session, unterwegs[0].id, True, "", device_id="druckgeraet"
                )
                nach_rueckmeldung = len(socket.gesendet)

                alle = (
                    await session.execute(select(PrintJob).order_by(PrintJob.id))
                ).scalars().all()
                return erster, nach_erstem, nach_rueckmeldung, [j.attempts for j in alle]
        finally:
            await hub.unregister(conn)

    erster, nach_erstem, nach_rueckmeldung, versuche = _run(scenario())

    assert erster == workflow.PRINT_BATCH
    assert nach_erstem == workflow.PRINT_BATCH
    # Die Rueckmeldung schiebt genau einen nach, statt alles liegen zu lassen
    assert nach_rueckmeldung == nach_erstem + 1
    # Kein Auftrag hat Versuche verbraucht, die er nie bekommen hat
    assert max(versuche) == 1


def test_laufender_auftrag_wird_nicht_doppelt_geschickt():
    """Ein Auftrag in "sent" liegt im Drucker - noch einmal senden heisst
    noch einmal drucken.

    Genau das geschah bei jedem Durchgang: wer zwei Artikel kurz hintereinander
    speicherte, bekam das erste Etikett zweimal aus dem Drucker. Ein doppeltes
    Etikett faellt beim Aufkleben nicht auf, im Bestand steht der Artikel
    trotzdem nur einmal - und die Rolle ist schneller leer als gedacht.
    """

    async def scenario():
        await init_db()
        await _schlange_leeren()
        socket = ZaehlenderSocket()
        conn = await hub.register("doppelgeraet", socket)
        try:
            async with session_scope() as session:
                session.add(
                    PrintJob(label="LEB000042", payload={"blocks": []}, status="sent")
                )
                await session.commit()
                return await workflow.flush_print_queue(session, "doppelgeraet"), len(
                    socket.gesendet
                )
        finally:
            await hub.unregister(conn)

    gesendet, frames = _run(scenario())
    assert gesendet == 0
    assert frames == 0


def test_nach_reconnect_geht_der_laufende_auftrag_erneut_hinaus():
    """Nach einem Geraeteneustart ist dessen Warteschlange leer.

    Was der Server als "sent" fuehrt, hat das Geraet dann nie gedruckt - hier
    und nur hier ist erneutes Senden richtig.
    """

    async def scenario():
        await init_db()
        await _schlange_leeren()
        socket = ZaehlenderSocket()
        conn = await hub.register("neustartgeraet", socket)
        try:
            async with session_scope() as session:
                session.add(
                    PrintJob(label="LEB999999", payload={"blocks": []}, status="sent")
                )
                await session.commit()
                return await workflow.flush_print_queue(
                    session, "neustartgeraet", resend_in_flight=True
                )
        finally:
            await hub.unregister(conn)

    assert _run(scenario()) == 1


def test_auftrag_ohne_rueckmeldung_faellt_nicht_unter_den_tisch():
    """Ein "sent" ohne Antwort blockierte die Schlange fuer immer.

    Wenn das Geraet mitten im Druck verschwindet (halboffener Socket, kein
    Disconnect beim Server), kommt nie ein print_result. Der Auftrag stand
    danach unbegrenzt auf "sent": nie gedruckt, in keiner Fehlerliste, und er
    belegte noch den Platz fuer die nachfolgenden.
    """

    async def scenario():
        await init_db()
        await _schlange_leeren()
        socket = ZaehlenderSocket()
        conn = await hub.register("stillesgeraet", socket)
        try:
            async with session_scope() as session:
                alt = utcnow() - timedelta(seconds=workflow.PRINT_STALE_SECONDS + 30)
                session.add(
                    PrintJob(
                        label="LEB000007",
                        payload={"blocks": []},
                        status="sent",
                        updated_at=alt,
                    )
                )
                await session.commit()
                return await workflow.flush_print_queue(session, "stillesgeraet")
        finally:
            await hub.unregister(conn)

    assert _run(scenario()) == 1
