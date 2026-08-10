"""Verbindungsverwaltung fuer Geraete und Web-Clients.

Der Hub kennt zwei Sorten Abonnenten:
  * Geraete (ESP32) an `/ws/device` - bidirektional
  * Browser an `/ws/ui`             - nur Server->Client (Live-Aktualisierung)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class DeviceConnection:
    def __init__(self, device_id: str, ws: WebSocket):
        self.device_id = device_id
        self.ws = ws
        self.send_lock = asyncio.Lock()
        self.info: dict[str, Any] = {}
        self.alive = True

    async def send(self, message: dict) -> bool:
        """Nachricht senden; bei Fehler wird die Verbindung als tot markiert.

        Das Lock verhindert verschraenkte Frames, wenn Web-Request und
        Workflow-Task gleichzeitig an dasselbe Geraet schreiben.
        """
        if not self.alive:
            return False
        try:
            async with self.send_lock:
                await self.ws.send_text(json.dumps(message, ensure_ascii=False))
            return True
        except Exception as exc:
            log.info("Senden an %s fehlgeschlagen: %s", self.device_id, exc)
            self.alive = False
            return False


class Hub:
    def __init__(self) -> None:
        self._devices: dict[str, DeviceConnection] = {}
        self._ui: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- Geraete
    async def register(self, device_id: str, ws: WebSocket) -> DeviceConnection:
        async with self._lock:
            previous = self._devices.get(device_id)
            if previous is not None:
                # Reconnect nach einem Reset: die alte Verbindung ist meist ein
                # halboffener Socket, den nur ein Schreibversuch entlarvt.
                previous.alive = False
                with contextlib.suppress(Exception):
                    await previous.ws.close(code=4000, reason="ersetzt")
            conn = DeviceConnection(device_id, ws)
            self._devices[device_id] = conn
        return conn

    async def unregister(self, conn: DeviceConnection) -> None:
        async with self._lock:
            if self._devices.get(conn.device_id) is conn:
                del self._devices[conn.device_id]
        conn.alive = False

    def get(self, device_id: str) -> DeviceConnection | None:
        conn = self._devices.get(device_id)
        return conn if conn and conn.alive else None

    def any_device(self) -> DeviceConnection | None:
        for conn in self._devices.values():
            if conn.alive:
                return conn
        return None

    def online_ids(self) -> list[str]:
        return [cid for cid, c in self._devices.items() if c.alive]

    async def send_to(self, device_id: str | None, message: dict) -> bool:
        conn = self.get(device_id) if device_id else self.any_device()
        if conn is None:
            return False
        return await conn.send(message)

    async def broadcast_devices(self, message: dict) -> int:
        conns = [c for c in self._devices.values() if c.alive]
        results = await asyncio.gather(
            *(c.send(message) for c in conns), return_exceptions=True
        )
        return sum(1 for r in results if r is True)

    # ------------------------------------------------------------ Web-Clients
    async def add_ui(self, ws: WebSocket) -> None:
        async with self._lock:
            self._ui.add(ws)

    async def remove_ui(self, ws: WebSocket) -> None:
        async with self._lock:
            self._ui.discard(ws)

    async def notify_ui(self, event: str, data: dict | None = None) -> None:
        """Browser ueber eine Aenderung informieren; sie laden dann neu.

        Bewusst nur ein Signal statt eines Datenpakets - so kann kein
        halbaktueller Zustand im Browser haengenbleiben.
        """
        if not self._ui:
            return
        payload = json.dumps({"event": event, "data": data or {}}, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self._ui):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._ui.discard(ws)

    def ui_count(self) -> int:
        return len(self._ui)


hub = Hub()
