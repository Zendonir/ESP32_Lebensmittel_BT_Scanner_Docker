"""WebSocket-Endpunkte fuer Geraete und Browser."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Device, utcnow
from ..services import inventory as inv
from . import protocol as proto
from . import workflow
from .hub import hub

log = logging.getLogger(__name__)
router = APIRouter()

PING_INTERVAL = 20  # Sekunden
RECV_TIMEOUT = 90  # ohne jede Nachricht gilt die Verbindung als tot


async def _upsert_device(device_id: str, info: dict) -> Device:
    async with session_scope() as session:
        device = (
            await session.execute(select(Device).where(Device.device_id == device_id))
        ).scalar_one_or_none()
        if device is None:
            device = Device(device_id=device_id)
            session.add(device)
        device.name = info.get("name") or device.name or device_id
        device.firmware = info.get("firmware", device.firmware)
        device.ip = info.get("ip", device.ip)
        device.has_printer = bool(info.get("has_printer", device.has_printer))
        device.online = True
        device.last_seen = utcnow()
        # Statische Geraetedaten aus hello() (siehe Net::sendHello) - selten
        # geaenderte Werte fuers System-Panel, die sonst mit der naechsten
        # Telemetrie (30s) ueberschrieben wuerden, wenn sie dort fehlen.
        telemetry = dict(device.telemetry or {})
        for key in ("ssid", "flash_mb", "res", "sd"):
            if key in info:
                telemetry[key] = info[key]
        device.telemetry = telemetry
        await session.commit()
        await session.refresh(device)
        return device


async def _mark_offline(device_id: str) -> None:
    async with session_scope() as session:
        device = (
            await session.execute(select(Device).where(Device.device_id == device_id))
        ).scalar_one_or_none()
        if device is not None:
            device.online = False
            device.last_seen = utcnow()
            await session.commit()


@router.websocket("/ws/device")
async def device_socket(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    device_id = (ws.query_params.get("id") or "").strip()

    if token != settings.device_token or not device_id:
        await ws.close(code=4401, reason="nicht autorisiert")
        log.warning("Geraeteverbindung abgelehnt (id=%r)", device_id)
        return

    await ws.accept()
    conn = await hub.register(device_id, ws)
    sess = workflow.session_for(device_id)
    log.info("Geraet verbunden: %s", device_id)

    heartbeat: asyncio.Task | None = None
    try:
        client_ip = ws.client.host if ws.client else ""
        device = await _upsert_device(device_id, {"ip": client_ip})
        async with session_scope() as session:
            await workflow.on_connect(session, sess, device)
        await hub.notify_ui("devices")

        heartbeat = asyncio.create_task(_heartbeat(conn))

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=RECV_TIMEOUT)
            except asyncio.TimeoutError:
                log.info("Geraet %s antwortet nicht - Verbindung wird geschlossen", device_id)
                break
            await _handle_message(device_id, sess, raw, client_ip)

    except WebSocketDisconnect:
        log.info("Geraet getrennt: %s", device_id)
    except Exception:
        log.exception("Fehler in der Geraeteverbindung %s", device_id)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await hub.unregister(conn)
        workflow.drop_session(device_id)
        await _mark_offline(device_id)
        await hub.notify_ui("devices")
        with contextlib.suppress(Exception):
            await ws.close()


async def _heartbeat(conn) -> None:
    while conn.alive:
        await asyncio.sleep(PING_INTERVAL)
        if not await conn.send(proto.ping()):
            return


async def _handle_message(device_id: str, sess, raw: str, client_ip: str) -> None:
    """Eine Geraetenachricht verarbeiten.

    Jede Nachricht bekommt ihre eigene Datenbanksitzung und ihren eigenen
    try/except: ein Fehler in einem Scan darf die Verbindung nicht killen -
    das Geraet sieht dann nur eine Fehlermeldung.
    """
    try:
        msg = json.loads(raw)
    except ValueError:
        log.warning("Ungueltiges JSON von %s: %.120s", device_id, raw)
        return
    if not isinstance(msg, dict):
        return

    kind = msg.get("t", "")
    try:
        async with session_scope() as session:
            if kind == "hello":
                # Der Bildschirm steht bereits (beim Verbindungsaufbau gesendet).
                # `hello` traegt nur Name, Version und Faehigkeiten nach - ein
                # zweiter Screen-Push wuerde die Anzeige unnoetig neu aufbauen.
                await _upsert_device(device_id, {**msg, "ip": client_ip})
                await hub.notify_ui("devices")

            elif kind == "scan":
                await workflow.on_scan(session, sess, str(msg.get("code", "")))

            elif kind == "tap":
                await workflow.on_tap(session, sess, str(msg.get("item", "")))

            elif kind == "input":
                await workflow.on_input(session, sess, msg.get("value"))

            elif kind == "back":
                sess.pop()
                await workflow.push_screen(session, sess)

            elif kind == "telemetry":
                await _store_telemetry(session, device_id, sess, msg)

            elif kind == "print_result":
                await workflow.on_print_result(
                    session,
                    int(msg.get("job", 0)),
                    bool(msg.get("ok")),
                    str(msg.get("error", "")),
                )

            elif kind == "pong":
                pass

            else:
                log.debug("Unbekannte Nachricht von %s: %s", device_id, kind)
    except Exception:
        log.exception("Verarbeitung von %r (%s) fehlgeschlagen", kind, device_id)
        await hub.send_to(device_id, proto.toast("Serverfehler", "error"))


async def _store_telemetry(session, device_id: str, sess, msg: dict) -> None:
    device = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device is None:
        return
    # Zusammenfuehren statt ersetzen - sonst loescht die naechste Telemetrie
    # (alle 30s) die statischen hello()-Felder (ssid, flash_mb, res, sd)
    # wieder, weil die dort schlicht nicht mitgeschickt werden.
    telemetry = dict(device.telemetry or {})
    telemetry.update(
        {
            k: msg[k]
            for k in ("heap", "min_heap", "psram", "rssi", "uptime", "wifi")
            if k in msg
        }
    )
    scanner = msg.get("scanner") or {}
    if scanner:
        telemetry["scanner"] = scanner
        previous = sess.scanner
        sess.scanner = scanner
        battery = scanner.get("battery", -1)
        # Warnung genau einmal beim Unterschreiten, nicht bei jedem Telemetriepaket.
        if 0 <= battery < 10 and previous.get("battery", 100) >= 10:
            await inv.log_event(
                session,
                "device",
                device=device_id,
                name="Scanner-Akku schwach",
                payload={"battery": battery},
            )
            await hub.send_to(device_id, proto.toast("Scanner-Akku schwach", "warn"))
    device.telemetry = telemetry
    device.online = True
    device.last_seen = utcnow()
    await session.commit()


@router.websocket("/ws/ui")
async def ui_socket(ws: WebSocket) -> None:
    """Live-Signale fuer das Web-Interface (nur Server -> Browser)."""
    await ws.accept()
    await hub.add_ui(ws)
    try:
        while True:
            # Der Browser sendet nichts ausser gelegentlichen Keepalives.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.remove_ui(ws)
