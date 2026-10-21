"""Brother-Etikettendrucker am WLAN-Printserver: Zustand, Treiberdatei, Testdruck.

Die Einstellungen selbst laufen wie alle anderen ueber
`PATCH /api/settings/brother` (Felder mit `data-setting="brother.…"`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..device.hub import hub
from ..models import PrintJob
from ..services import brother, settings_store

router = APIRouter(prefix="/api/brother", tags=["etiketten"])

# Eine Treiberdatei fuer ein Etikett hat wenige KB; alles darueber ist keine.
MAX_TREIBERDATEI = 2 * 1024 * 1024


def _profil_kurz(profil: dict) -> dict:
    return {k: v for k, v in profil.items() if k not in ("kopf",)}


@router.get("")
async def zustand(session: AsyncSession = Depends(get_session)):
    """Einstellungen, was der Printserver meldet, das Profil und die letzten Auftraege."""
    gespeichert = await settings_store.get(session, "brother", {}) or {}
    printserver = await brother.printserver_status(gespeichert)
    cfg = brother.mit_medium(gespeichert, printserver.get("medium"))
    printer_cfg = await settings_store.get(session, "printer", {}) or {}
    jobs = (await session.execute(
        select(PrintJob).where(PrintJob.ziel == brother.ZIEL)
        .order_by(PrintJob.id.desc()).limit(10))).scalars().all()
    profil = brother.profil_aus(cfg)
    try:
        vorschau = brother.vorschau_png(cfg, printer_cfg)
    except Exception as e:      # ein unpassendes Mass darf die Seite nicht leeren
        vorschau, fehler = "", str(e)
    else:
        fehler = ""
    breite, zeilen = brother.masse(profil, cfg)
    soll = brother.erwartet(profil, cfg)
    return {
        "einstellungen": brother.oeffentlich(gespeichert),
        "printserver": printserver,
        "profil": _profil_kurz(profil),
        "masse_mm": {"breite": round(breite / brother.DOTS_PER_MM, 1),
                     "laenge": round(zeilen / brother.DOTS_PER_MM, 1)},
        "vorschau": vorschau,
        "vorschau_fehler": fehler,
        # Positioniert der Drucker per Lueckensensor (gestanzte Etiketten)?
        "sensor": soll["typ"] == "die_cut_labels",
        "medium_hinweis": brother.medium_abweichung(soll, printserver.get("medium")),
        "auftraege": [{"id": j.id, "label": j.label, "status": j.status, "error": j.error,
                       "created_at": j.created_at.isoformat() if j.created_at else ""}
                      for j in jobs],
    }


@router.post("/treiberdatei")
async def treiberdatei(datei: UploadFile = File(...),
                       session: AsyncSession = Depends(get_session)):
    """Druckdatei des Brother-Treibers (.prn/.bin) einlesen und als Profil speichern."""
    daten = await datei.read(MAX_TREIBERDATEI + 1)
    if len(daten) > MAX_TREIBERDATEI:
        raise HTTPException(413, "Datei zu groß für ein Etikett")
    try:
        profil = brother.analysieren(daten)
    except brother.BrotherFehler as e:
        raise HTTPException(422, str(e)) from e
    profil["datei"] = (datei.filename or "")[:100]
    await settings_store.merge(session, "brother", {"profil": profil})
    await session.commit()
    await hub.notify_ui("settings")
    return _profil_kurz(profil)


@router.delete("/treiberdatei")
async def treiberdatei_vergessen(session: AsyncSession = Depends(get_session)):
    """Zurueck zum Standardprofil."""
    await settings_store.merge(session, "brother", {"profil": {}})
    await session.commit()
    await hub.notify_ui("settings")
    return {"ok": True}


@router.post("/test")
async def testdruck(session: AsyncSession = Depends(get_session)):
    """Testetikett mit Rahmen um das ganze Etikett - zeigt Versatz und Drehung."""
    cfg = await settings_store.get(session, "brother", {}) or {}
    if not brother.basis_url(cfg):
        raise HTTPException(409, "Erst die Adresse des Printservers eintragen")
    job = PrintJob(label="TEST", payload={"test": True}, status="queued", ziel=brother.ZIEL)
    session.add(job)
    await session.commit()
    brother.anstossen()
    return {"ok": True, "job": job.id}
