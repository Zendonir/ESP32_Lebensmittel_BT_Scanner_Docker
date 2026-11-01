"""Sprachsteuerung (Home Assistant Assist) - siehe services/sprache.py.

Angemeldet wird mit `Authorization: Bearer <SPRACHE_TOKEN>` oder wie die
Oberflaeche (Cookie/Basic). Ein eigener Schluessel, damit Home Assistant
weder das Web-Passwort noch den Geraete-Token kennen muss.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..services import anmeldung, sprache


async def anmeldung_pruefen(request: Request) -> None:
    kopf = request.headers.get("authorization", "")
    if settings.sprache_token and kopf.lower().startswith("bearer "):
        if hmac.compare_digest(kopf[7:].strip(), settings.sprache_token):
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "SPRACHE_TOKEN falsch")
    if not anmeldung.angemeldet(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Anmeldung erforderlich")


router = APIRouter(prefix="/api/sprache", tags=["sprache"],
                   dependencies=[Depends(anmeldung_pruefen)])


class Bestand(BaseModel):
    name: str = ""
    ort: str = ""
    haushalt: str = ""


class Ablauf(BaseModel):
    haushalt: str = ""


class Auslagern(BaseModel):
    name: str = ""
    ort: str = ""
    weggeworfen: bool = False
    haushalt: str = ""


class Etikett(BaseModel):
    name: str = ""
    # Menge als Text ("500 g", "1,5 kg", "3") oder Zahl mit Einheit.
    menge: str | float | None = None
    einheit: str = ""
    # Datum, "morgen", "in 5 Tagen", "kein" - leer = Vorschlag
    mhd: str | None = None
    ort: str = ""
    anzahl: int | None = Field(default=None, ge=1, le=20)
    haushalt: str = ""
    terminal: str = ""
    drucken: bool = True
    bestaetigt: bool = False


def _leer_zu_none(v):
    return None if v in ("", None) else v


@router.post("/bestand")
async def bestand(body: Bestand, session: AsyncSession = Depends(get_session)):
    return await sprache.bestand(session, name=body.name, ort=body.ort, haushalt=body.haushalt)


@router.post("/ablauf")
async def ablauf(body: Ablauf, session: AsyncSession = Depends(get_session)):
    return await sprache.ablauf(session, haushalt=body.haushalt)


@router.post("/auslagern")
async def auslagern(body: Auslagern, session: AsyncSession = Depends(get_session)):
    return await sprache.auslagern(session, name=body.name, ort=body.ort,
                                   weggeworfen=body.weggeworfen, haushalt=body.haushalt)


@router.post("/etikett")
async def etikett(body: Etikett, session: AsyncSession = Depends(get_session)):
    return await sprache.etikett(
        session, name=body.name, menge=_leer_zu_none(body.menge), einheit=body.einheit,
        mhd=_leer_zu_none(body.mhd), ort=body.ort, anzahl=body.anzahl,
        haushalt=body.haushalt, terminal=body.terminal, bestaetigt=body.bestaetigt,
        drucken=body.drucken)
