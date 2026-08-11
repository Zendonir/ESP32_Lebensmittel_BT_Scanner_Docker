"""Firmware-Verteilung: Verwaltung im Web-Interface, Abholung durch das Geraet.

Bewusst zwei getrennte Router mit unterschiedlicher Anmeldung:

* `router`        - Hochladen, Holen, Ausloesen. Haengt wie das uebrige
                    Web-Interface hinter `UI_PASSWORD`.
* `public_router` - nur der Download des Abbilds, angemeldet ueber
                    `DEVICE_TOKEN`. Das Terminal kennt das Web-Passwort nicht
                    und soll es auch nicht kennen muessen.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..device import protocol as proto
from ..device.hub import hub
from ..models import Device
from ..services import firmware as fw

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/firmware", tags=["firmware"])
public_router = APIRouter(prefix="/firmware", tags=["firmware"])


@router.get("")
async def list_firmware():
    return {"boards": fw.BOARDS, "images": fw.available()}


@router.post("/upload")
async def upload_firmware(
    board: str = Query(...),
    version: str = Query("", description="z.B. v2.1.0 - leer: Datum des Uploads"),
    file: UploadFile = ...,
):
    """Ein selbst gebautes oder heruntergeladenes `firmware-<board>.bin` ablegen.

    Der Dateiname taugt nicht als Version - er heisst im Release immer
    `firmware-<board>.bin`. Ohne Angabe wird deshalb das Datum genommen, damit
    zumindest unterscheidbar bleibt, was wann hinterlegt wurde.
    """
    data = await file.read()
    label = version.strip() or f"upload-{datetime.now():%Y-%m-%d}"
    try:
        return fw.store(board, data, label, "upload")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/fetch")
async def fetch_firmware(tag: str = Query("", description="leer = neuestes Release")):
    """Abbilder aus dem GitHub-Release holen (Internetzugang des Servers noetig)."""
    try:
        return {"images": await fw.fetch_from_github(tag)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # httpx-Fehler, Zeitueberschreitung, ...
        log.warning("Firmware konnte nicht geholt werden: %s", exc)
        raise HTTPException(502, f"Release nicht erreichbar: {exc}") from exc


@router.post("/push/{device_id}")
async def push_firmware(device_id: str):
    """Das Terminal auffordern, sich das passende Abbild abzuholen."""
    async with session_scope() as session:
        device = (
            await session.execute(select(Device).where(Device.device_id == device_id))
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(404, "Geraet unbekannt")
        board = str((device.telemetry or {}).get("board") or "")

    if board not in fw.BOARDS:
        raise HTTPException(
            409,
            "Boardvariante des Geraets unbekannt - bitte einmal neu verbinden "
            "lassen, damit es sich neu anmeldet",
        )

    meta = fw.meta(board)
    if meta is None:
        raise HTTPException(404, f"Kein Abbild fuer Variante {board} hinterlegt")

    message = proto.ota(
        path=f"/firmware/{board}.bin",
        version=meta["version"],
        size=meta["size"],
        sha256=meta["sha256"],
    )
    if not await hub.send_to(device_id, message):
        raise HTTPException(503, "Geraet nicht erreichbar")
    return {"ok": True, **meta}


@public_router.get("/{board}.bin")
async def download_firmware(board: str, token: str = Query("")):
    # compare_digest statt == : verhindert, dass sich das Token ueber die
    # Antwortzeit erraten laesst.
    if not hmac.compare_digest(token, settings.device_token):
        raise HTTPException(401, "Ungueltiges Token")

    path = fw.path_for(board)
    if path is None:
        raise HTTPException(404, "Kein Abbild hinterlegt")
    return FileResponse(path, media_type="application/octet-stream")
