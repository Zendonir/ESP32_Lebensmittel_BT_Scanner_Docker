"""Brother-Etikettendrucker am WLAN-Printserver: Zustand, Treiberdatei, Testdruck.

Die Einstellungen selbst laufen wie alle anderen ueber
`PATCH /api/settings/brother` (Felder mit `data-setting="brother.…"`).
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..device.hub import hub
from ..models import PrintJob
from ..services import brother, haushalte, settings_store

router = APIRouter(prefix="/api/brother", tags=["etiketten"])

# Eine Treiberdatei fuer ein Etikett hat wenige KB; alles darueber ist keine.
MAX_TREIBERDATEI = 2 * 1024 * 1024

# Pruefen und Anlegen des Testetiketts am Stueck - zwei gleichzeitige Klicks
# saehen sonst beide noch keins wartend.
_testdruck_sperre = asyncio.Lock()


def _profil_kurz(profil: dict) -> dict:
    return {k: v for k, v in profil.items() if k not in ("kopf",)}


async def _drucker_gewaehlt(session: AsyncSession, request: Request,
                           drucker_id: str | None) -> dict:
    """Der Drucker, um den es geht: der genannte (wenn diese Ansicht ihn sieht),
    sonst der des Haushalts. 409, wenn es keinen gibt."""
    cfg = await settings_store.get(session, "brother", {}) or {}
    haushalt = await haushalte.aus_anfrage(request, session)
    if drucker_id:
        d = brother.drucker_nach_id(cfg, drucker_id)
        if d is None or not brother.gehoert_zu(d, haushalt):
            raise HTTPException(404, "Diesen Brother gibt es hier nicht")
    else:
        d = brother.drucker_fuer(cfg, haushalt or "") or next(
            iter(brother.sichtbare_drucker(cfg, haushalt)), None)
    if d is None or not brother.basis_url(d):
        raise HTTPException(409, "Erst einen Brother-Drucker mit Adresse anlegen")
    return d


async def _drucker_zustand(d: dict, printer_cfg: dict) -> dict:
    """Ein Drucker mit dem, was sein Printserver meldet - fuer Karte und Liste."""
    printserver = await brother.printserver_status(d)
    cfg = brother.mit_medium(d, printserver.get("medium"))
    profil = brother.profil_aus(cfg)
    soll = brother.erwartet(profil, cfg)
    return {
        "id": d["drucker_id"], "name": d.get("name", ""), "url": d["url"],
        "haushalt": ", ".join(brother.haushalte_von(d)), "etikett": brother.etikett_wahl(d),
        "aktiv": d["aktiv"], "printserver": printserver,
        "medium_hinweis": brother.medium_abweichung(soll, printserver.get("medium")),
        "sorte": _profil_kurz(profil),
    }


@router.get("/geraet")
async def geraet(request: Request, session: AsyncSession = Depends(get_session)):
    """Die Brother als Geraete (Seite Terminals) - nur die, die diese Ansicht sieht."""
    cfg = await settings_store.get(session, "brother", {}) or {}
    haushalt = await haushalte.aus_anfrage(request, session)
    printer_cfg = await settings_store.get(session, "printer", {}) or {}
    drucker = brother.sichtbare_drucker(cfg, haushalt)
    return {"drucker": list(await asyncio.gather(*(_drucker_zustand(d, printer_cfg)
                                                    for d in drucker)))}


@router.get("/sorten")
async def sorten(session: AsyncSession = Depends(get_session)):
    """Die waehlbaren Etikettensorten (Terminalkarte: USB-Brother)."""
    return brother.etikettensorten(await settings_store.get(session, "brother", {}) or {})


@router.get("")
async def zustand(request: Request, sorte: str = "",
                  session: AsyncSession = Depends(get_session)):
    """Das Brother-Etikett (gilt fuer WLAN und USB), die Drucker dieser Ansicht
    und eine Vorschau - fuer `sorte` oder die des ersten Druckers."""
    cfg = await settings_store.get(session, "brother", {}) or {}
    haushalt = await haushalte.aus_anfrage(request, session)
    printer_cfg = await settings_store.get(session, "printer", {}) or {}
    drucker = brother.sichtbare_drucker(cfg, haushalt)
    zustaende = list(await asyncio.gather(*(_drucker_zustand(d, printer_cfg) for d in drucker)))
    sorten = brother.etikettensorten(cfg)
    if sorte not in {e["key"] for e in sorten}:
        # Die gespeicherte Standardsorte (brother.etikett): gilt fuer jeden
        # Drucker und jedes USB-Terminal ohne eigene Wahl.
        sorte = brother.etikett_wahl(cfg)
    ansicht = {**cfg, "etikett": sorte}
    profil = brother.profil_aus(ansicht)
    try:
        vorschau, fehler = brother.vorschau_png(ansicht, printer_cfg), ""
    except Exception as e:      # ein unpassendes Mass darf die Seite nicht leeren
        vorschau, fehler = "", str(e)
    breite, zeilen = brother.masse(profil, ansicht)
    stmt = select(PrintJob).where(PrintJob.ziel == brother.ZIEL)
    if haushalt:
        stmt = stmt.where(PrintJob.household == haushalt)
    jobs = (await session.execute(stmt.order_by(PrintJob.id.desc()).limit(10))).scalars().all()
    return {
        "sichtbar": True,
        "einstellungen": brother.oeffentlich(cfg),
        "etikettensorten": sorten,
        "drucker": zustaende,
        "vorschau_sorte": sorte,
        "profil": _profil_kurz(profil),
        "masse_mm": {"breite": round(breite / brother.DOTS_PER_MM, 1),
                     "laenge": round(zeilen / brother.DOTS_PER_MM, 1)},
        "vorschau": vorschau,
        "vorschau_fehler": fehler,
        "sensor": brother.erwartet(profil, ansicht)["typ"] == "die_cut_labels",
        "auftraege": [{"id": j.id, "label": j.label, "status": j.status, "error": j.error,
                       "created_at": j.created_at.isoformat() if j.created_at else ""}
                      for j in jobs],
    }


# ------------------------------------------------------------ Druckerliste
def _drucker_pruefen(body: dict, cfg: dict) -> dict:
    """Nur die bekannten Felder, Etikettensorte aus der Auswahl."""
    werte = {k: body[k] for k in brother.DRUCKER_FELDER if k in body}
    if "etikett" in werte and werte["etikett"] not in (
            {e["key"] for e in brother.etikettensorten(cfg)} | {""}):
        raise HTTPException(422, f"Unbekannte Etikettensorte: {werte['etikett']}")
    return werte


async def _liste_speichern(session: AsyncSession, liste: list[dict]) -> None:
    # url leeren: sonst kaeme der alte Einzel-Drucker (Altbestand) zurueck,
    # sobald die Liste wieder leer ist.
    await settings_store.merge(session, "brother", {"drucker": liste, "url": ""})
    await session.commit()
    await hub.notify_ui("settings")
    await hub.notify_ui("devices")
    brother.anstossen()


@router.post("/drucker")
async def drucker_anlegen(body: dict, request: Request,
                          session: AsyncSession = Depends(get_session)):
    """Einen Brother am WLAN-Printserver hinzufuegen: Adresse, Haushalt, Etikettensorte."""
    cfg = await settings_store.get(session, "brother", {}) or {}
    werte = _drucker_pruefen(body, cfg)
    if not str(werte.get("url", "")).strip():
        raise HTTPException(422, "Adresse des Printservers fehlt")
    # In der Ansicht eines Haushalts gehoert ein neuer Drucker ihm.
    haushalt = await haushalte.aus_anfrage(request, session)
    if haushalt and "haushalt" not in werte:
        werte["haushalt"] = haushalt
    neu = brother._drucker_sauber({"aktiv": True, **werte, "id": uuid.uuid4().hex[:8]})
    await _liste_speichern(session, [*brother.drucker_liste(cfg), neu])
    return neu


@router.patch("/drucker/{drucker_id}")
async def drucker_aendern(drucker_id: str, body: dict,
                          session: AsyncSession = Depends(get_session)):
    cfg = await settings_store.get(session, "brother", {}) or {}
    liste = brother.drucker_liste(cfg)
    if not any(d["id"] == drucker_id for d in liste):
        raise HTTPException(404, "Diesen Brother gibt es nicht")
    werte = _drucker_pruefen(body, cfg)
    liste = [brother._drucker_sauber({**d, **werte}) if d["id"] == drucker_id else d
             for d in liste]
    await _liste_speichern(session, liste)
    return next(d for d in liste if d["id"] == drucker_id)


@router.delete("/drucker/{drucker_id}")
async def drucker_entfernen(drucker_id: str, session: AsyncSession = Depends(get_session)):
    cfg = await settings_store.get(session, "brother", {}) or {}
    liste = brother.drucker_liste(cfg)
    if not any(d["id"] == drucker_id for d in liste):
        raise HTTPException(404, "Diesen Brother gibt es nicht")
    await _liste_speichern(session, [d for d in liste if d["id"] != drucker_id])
    return {"ok": True}


@router.get("/druckereinstellungen")
async def druckereinstellungen_lesen(request: Request, drucker: str = "",
                                     session: AsyncSession = Depends(get_session)):
    """Geraeteeinstellungen, die im Drucker selbst gespeichert sind."""
    d = await _drucker_gewaehlt(session, request, drucker)
    try:
        return await brother.druckereinstellungen(d)
    except brother.BrotherFehler as e:
        raise HTTPException(409, str(e)) from e


@router.patch("/druckereinstellungen")
async def druckereinstellungen_setzen(body: dict[str, int], request: Request, drucker: str = "",
                                      session: AsyncSession = Depends(get_session)):
    """Einstellungen im Drucker setzen, z. B. {"command_mode": 1}."""
    unbekannt = set(body) - set(brother.EINSTELLUNG_NAMEN)
    if unbekannt or not body:
        raise HTTPException(422, f"Unbekannte Einstellung: {', '.join(sorted(unbekannt)) or '-'}")
    d = await _drucker_gewaehlt(session, request, drucker)
    try:
        return await brother.druckereinstellungen(d, body)
    except brother.BrotherFehler as e:
        raise HTTPException(409, str(e)) from e


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
    await settings_store.merge(session, "brother", {"profil": profil, "etikett": "treiberdatei"})
    await session.commit()
    await hub.notify_ui("settings")
    return _profil_kurz(profil)


@router.delete("/treiberdatei")
async def treiberdatei_vergessen(session: AsyncSession = Depends(get_session)):
    """Zurueck zum Standardprofil."""
    cfg = await settings_store.get(session, "brother", {}) or {}
    await settings_store.merge(session, "brother", {
        "profil": {}, "etikett": brother.KEINE_VORLAGE if cfg.get("etikett") == "treiberdatei" else cfg.get("etikett", "")})
    await session.commit()
    await hub.notify_ui("settings")
    return {"ok": True}


@router.post("/test")
async def testdruck(request: Request, drucker: str = "",
                    session: AsyncSession = Depends(get_session)):
    """Testetikett mit Rahmen um das ganze Etikett - zeigt Versatz und Drehung."""
    # Ein Testetikett je Drucker genuegt: wer bei ausgeschaltetem Drucker
    # mehrmals drueckt, bekam sonst alle auf einmal, sobald der Drucker da war.
    # Alles unter der Sperre, auch das erste Lesen - sonst haelt die
    # Lesetransaktion einen Stand von vor dem Anlegen durch die andere Anfrage.
    async with _testdruck_sperre:
        d = await _drucker_gewaehlt(session, request, drucker)
        offen = [j for j in await _offene_tests(session)
                 if (j.payload or {}).get("drucker") == d["drucker_id"]
                 and not (j.payload or {}).get("layout")]
        if offen:
            return {"ok": True, "job": offen[0].id, "wartet": True}
        job = PrintJob(label="TEST", payload={"test": True, "drucker": d["drucker_id"]},
                       status="queued", ziel=brother.ZIEL)
        session.add(job)
        await session.commit()
    brother.anstossen()
    return {"ok": True, "job": job.id}


async def _offene_tests(session: AsyncSession) -> list[PrintJob]:
    return list((await session.execute(select(PrintJob).where(
        PrintJob.ziel == brother.ZIEL, PrintJob.label == "TEST",
        PrintJob.status.in_(("queued", "sent"))))).scalars().all())


@router.post("/test/layouts")
async def layouts_testen(request: Request, drucker: str = "",
                         session: AsyncSession = Depends(get_session)):
    """Je Layout ein Musteretikett auf der Etikettensorte dieses Druckers -
    zum Vergleichen und Aussuchen. Wie beim Testetikett hoechstens ein Satz
    auf einmal."""
    async with _testdruck_sperre:
        d = await _drucker_gewaehlt(session, request, drucker)
        if any((j.payload or {}).get("layout") and (j.payload or {}).get("drucker") == d["drucker_id"]
               for j in await _offene_tests(session)):
            return {"ok": True, "anzahl": 0, "wartet": True}
        layouts = brother.layouts_zum_testen(d)
        for layout in layouts:
            session.add(PrintJob(label="TEST", status="queued", ziel=brother.ZIEL,
                                 payload={"test": True, "layout": layout,
                                          "drucker": d["drucker_id"]}))
        await session.commit()
    brother.anstossen()
    return {"ok": True, "anzahl": len(layouts), "layouts": layouts}
