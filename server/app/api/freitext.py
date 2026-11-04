"""Freitext-Etiketten: Text eingeben, Stil waehlen, am Brother drucken.

Losgeloest vom Bestand - kein Artikel, keine Etikettnummer. Gedruckt wird
ueber die Druckauftraege (`print_jobs`) wie jedes andere Etikett: am Brother
des WLAN-Printservers (`ziel = "brother"`, Drucker in `payload.drucker`) oder
am Brother per USB eines Terminals (fest an dessen `device_pk`). Der Auftrag
traegt den Text in `payload.freitext`; gezeichnet wird erst beim Senden
(`brother.auftrag_bauen`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..device import workflow
from ..device.hub import hub
from ..models import Device, PrintJob
from ..services import brother, freitext, haushalte, settings_store

router = APIRouter(prefix="/api/freitext", tags=["freitext"])

LABEL = "FREITEXT"


class Freitext(BaseModel):
    text: str = Field(default="", max_length=freitext.MAX_ZEICHEN)
    stil: str = "einfach"
    ausrichtung: str = "mitte"
    # Leer = wie unter Etiketten -> Brother-Etikett eingestellt
    drehung: str = ""
    ziel: str = ""
    anzahl: int = Field(default=1, ge=1, le=50)

    def daten(self) -> dict:
        return {"text": self.text,
                "stil": self.stil if self.stil in freitext.STILE else "einfach",
                "ausrichtung": self.ausrichtung if self.ausrichtung in freitext.AUSRICHTUNGEN
                else "mitte",
                "drehung": self.drehung if self.drehung in brother.DREHUNGEN else ""}


async def _ziele(session: AsyncSession, haushalt: str | None) -> list[dict]:
    """Wohin gedruckt werden kann: Brother am WLAN-Printserver und Terminals
    mit Brother am USB - je mit der Etikettensorte, die dort liegt."""
    cfg = await settings_store.get(session, "brother", {}) or {}
    sorten = {s["key"]: s["name"] for s in brother.etikettensorten(cfg)}
    ziele = []
    for d in brother.sichtbare_drucker(cfg, haushalt):
        if not d.get("aktiv", True) or not brother.basis_url(d):
            continue
        # sichtbare_drucker liefert schon die Einstellungen je Drucker.
        ziele.append({"ziel": f"brother:{d['drucker_id']}", "art": "wlan",
                      "name": d.get("name") or "Brother (WLAN)", "online": True,
                      "sorte": sorten.get(brother.etikett_wahl(d), "")})
    stmt = select(Device).where(Device.drucker == "usb")
    if haushalt:
        stmt = stmt.where(Device.household == haushalt)
    online = set(workflow.drucker_online())
    for g in (await session.execute(stmt.order_by(Device.name))).scalars():
        tcfg = {**cfg, "etikett": g.brother_etikett} if g.brother_etikett else cfg
        ziele.append({"ziel": f"terminal:{g.device_id}", "art": "usb",
                      "name": f"{g.name or g.device_id} (USB)", "online": g.device_id in online,
                      "sorte": sorten.get(brother.etikett_wahl(tcfg), "")})
    return ziele


async def _ziel_cfg(session: AsyncSession, haushalt: str | None, ziel: str) -> tuple[dict, dict]:
    """(Einstellungen fuer genau dieses Ziel, das Ziel aus `_ziele`). 404 unbekannt."""
    treffer = next((z for z in await _ziele(session, haushalt) if z["ziel"] == ziel), None)
    if treffer is None:
        raise HTTPException(404, "Diesen Drucker gibt es hier nicht")
    cfg = await settings_store.get(session, "brother", {}) or {}
    art, kennung = ziel.split(":", 1)
    if art == "brother":
        return brother.fuer_drucker(cfg, brother.drucker_nach_id(cfg, kennung)), treffer
    sorte = (await session.execute(select(Device.brother_etikett).where(
        Device.device_id == kennung))).scalar_one_or_none()
    return ({**cfg, "etikett": sorte} if sorte else cfg), treffer


@router.get("")
async def uebersicht(request: Request, session: AsyncSession = Depends(get_session)):
    """Ziele, Stile, Ausrichtungen - alles, was die Seite zum Aufbauen braucht."""
    haushalt = await haushalte.aus_anfrage(request, session)
    return {"ziele": await _ziele(session, haushalt),
            "stile": [{"key": k, "name": v} for k, v in freitext.STILE.items()],
            "ausrichtungen": list(freitext.AUSRICHTUNGEN)}


@router.post("/vorschau")
async def vorschau(body: Freitext, request: Request, session: AsyncSession = Depends(get_session)):
    haushalt = await haushalte.aus_anfrage(request, session)
    if body.ziel:
        cfg, _ = await _ziel_cfg(session, haushalt, body.ziel)
    else:
        cfg = await settings_store.get(session, "brother", {}) or {}
    return brother.freitext_vorschau(body.daten(), cfg)


@router.post("/drucken")
async def drucken(body: Freitext, request: Request, session: AsyncSession = Depends(get_session)):
    haushalt = await haushalte.aus_anfrage(request, session)
    if not body.text.strip():
        raise HTTPException(422, "Erst einen Text eingeben")
    if not body.ziel:
        raise HTTPException(422, "Erst einen Drucker wählen")
    _, ziel = await _ziel_cfg(session, haushalt, body.ziel)
    art, kennung = body.ziel.split(":", 1)
    daten = body.daten()
    jobs = []
    if art == "brother":
        for _ in range(body.anzahl):
            jobs.append(PrintJob(label=LABEL, status="queued", ziel=brother.ZIEL,
                                 household=haushalt or "",
                                 payload={"freitext": daten, "drucker": kennung}))
    else:
        geraet = (await session.execute(select(Device).where(
            Device.device_id == kennung))).scalar_one()
        for _ in range(body.anzahl):
            jobs.append(PrintJob(label=LABEL, status="queued", ziel="", device_pk=geraet.id,
                                 household=geraet.household or "",
                                 payload={"freitext": daten}))
    session.add_all(jobs)
    await session.commit()
    if art == "brother":
        brother.anstossen()
    elif ziel["online"]:
        await workflow.flush_print_queue(session, kennung)
    await hub.notify_ui("labels")
    return {"auftraege": [j.id for j in jobs], "ziel": ziel["name"], "online": ziel["online"]}
