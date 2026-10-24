"""KI-Vorschlaege fuer neue Produkte: Kategorie, Sorte, Haltbarkeit je Lagerart.

OpenFoodFacts kennt Name und Marke, aber nicht, wie lange ein Produkt im
Kuehlschrank oder im Gefrierschrank haelt. Ist ANTHROPIC_API_KEY gesetzt (und
die KI unter System nicht abgeschaltet), fragt der Server Claude danach und
merkt sich die Antwort am Produkt - beim naechsten Scan steht sie ohne
Anfrage da. Ohne Schluessel laeuft alles wie bisher ueber die Tabelle der
Kategorien (services/haltbarkeit.py).

Die Antwort ist ein Vorschlag, kein Befehl: was der Mensch eintraegt, gilt.
Gefragt wird nur mit Produktname und Marke - nie mit Bestand oder Haushalt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Category, Product
from . import haltbarkeit, settings_store
from .dates import mhd_aus_teilen

log = logging.getLogger(__name__)

MODELL = os.getenv("KI_MODEL", "claude-opus-5-5")
ZEITGRENZE_S = 45.0

_SYSTEM = (
    "Du bist ein Helfer in einem Lebensmittel-Inventar fuer einen Privathaushalt in "
    "Deutschland. Zu einem Produkt (Name, ggf. Marke) schaetzt du: die passende Kategorie "
    "aus der vorgegebenen Liste, eine kurze Sorte (z. B. 'Rind', 'Vollmilch', leer wenn "
    "unklar) und die uebliche Haltbarkeit des UNGEOEFFNETEN Produkts in Tagen je Lagerart: "
    "raum (Raumtemperatur), kuehl (Kuehlschrank), tiefkuehl (Gefrierschrank). 0 heisst: "
    "so lagert man es nicht (z. B. Frischmilch bei Raumtemperatur, Getraenke im "
    "Gefrierschrank). Bei Fleisch, Fisch und Gefluegel im Kuehlschrank vorsichtig "
    "schaetzen. 'hinweis' ist ein kurzer deutscher Satz zur Lagerung oder leer."
)

_aktiv_aufgaben: set[int] = set()


def verfuegbar() -> bool:
    """Ein Schluessel ist da - ob die KI genutzt wird, sagt zusaetzlich ki.aktiv."""
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


async def aktiv(session: AsyncSession) -> bool:
    if not verfuegbar():
        return False
    cfg = await settings_store.get(session, "ki", {}) or {}
    return bool(cfg.get("aktiv", True))


def _schema(kategorien: list[str]) -> dict:
    # Ohne minimum/maximum: Zahlengrenzen kennen strukturierte Ausgaben nicht
    # zuverlaessig - begrenzt wird danach in haltbarkeit.bereinigen().
    tage = {"type": "integer"}
    return {
        "type": "object",
        "properties": {
            "kategorie": {"type": "string", "enum": kategorien or ["Sonstiges"]},
            "sorte": {"type": "string"},
            "haltbarkeit": {
                "type": "object",
                "properties": {"raum": tage, "kuehl": tage, "tiefkuehl": tage},
                "required": ["raum", "kuehl", "tiefkuehl"],
                "additionalProperties": False,
            },
            "hinweis": {"type": "string"},
        },
        "required": ["kategorie", "sorte", "haltbarkeit", "hinweis"],
        "additionalProperties": False,
    }


async def vorschlagen(name: str, marke: str, kategorien: list[str]) -> dict | None:
    """Ein Vorschlag von Claude, oder None (kein Schluessel, Fehler, abgelehnt)."""
    name = (name or "").strip()
    if not name or not verfuegbar():
        return None
    try:
        import anthropic
    except ImportError:          # Abbild ohne SDK - dann eben ohne KI
        log.warning("KI: Paket 'anthropic' fehlt")
        return None

    produkt = f"Produkt: {name}" + (f"\nMarke: {marke.strip()}" if marke and marke.strip() else "")
    try:
        async with anthropic.AsyncAnthropic(timeout=ZEITGRENZE_S, max_retries=1) as client:
            antwort = await client.beta.messages.create(
                model=MODELL,
                max_tokens=2000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": produkt}],
                # Kurze Einordnung - geringe Tiefe reicht und haelt es schnell.
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": _schema(kategorien)}},
                # Lehnt ein Sicherheitsfilter ab, uebernimmt der empfohlene Ersatz.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
    except anthropic.APIStatusError as e:
        log.warning("KI: Anfrage abgelehnt (%s): %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("KI: keine Verbindung: %s", e)
        return None

    if antwort.stop_reason == "refusal":
        log.info("KI: kein Vorschlag fuer %r (abgelehnt)", name)
        return None
    text = next((b.text for b in antwort.content if b.type == "text"), "")
    try:
        daten = json.loads(text)
    except json.JSONDecodeError:
        log.warning("KI: Antwort ist kein JSON: %r", text[:200])
        return None
    return {
        "kategorie": daten.get("kategorie", ""),
        "sorte": (daten.get("sorte") or "").strip()[:100],
        "haltbarkeit": haltbarkeit.bereinigen(daten.get("haltbarkeit")),
        "hinweis": (daten.get("hinweis") or "").strip()[:300],
    }


# ------------------------------------------------------------ MHD vom Foto
# Aufdrucke auf Verpackungen sind Nadeldrucker-Ziffern auf Folie, gemischt mit
# Chargennummern und Uhrzeiten ("61520335 07:08", "L.2814"). Eine reine
# Texterkennung im Browser liest davon zu wenig zuverlaessig; Claude liest
# das Foto und sagt, welche Zahl das MHD ist. Umgerechnet wird hier - die KI
# liefert nur Tag, Monat und Jahr, wie sie dastehen.

_SYSTEM_MHD = (
    "Du liest auf dem Foto einer Lebensmittelverpackung das Mindesthaltbarkeitsdatum "
    "(MHD, 'mindestens haltbar bis', 'zu verbrauchen bis', 'best before', 'EXP'). "
    "Ignoriere Chargen-/Losnummern (L..., lange Ziffernfolgen), Uhrzeiten und "
    "Herstell-, Abpack- und Einfrierdaten (EFD, 'eingefroren am'). Steht nur ein "
    "solches Datum im Bild, gilt: kein MHD gefunden. Gib Tag, Monat und Jahr so an, wie sie aufgedruckt sind: Jahr "
    "zwei- oder vierstellig, Tag 0 wenn nur Monat und Jahr dastehen (z. B. '01.2027'). "
    "Deutsche Reihenfolge Tag/Monat/Jahr, ausser der Aufdruck zeigt eindeutig etwas "
    "anderes. 'gelesen' ist der Aufdruck woertlich. Ist kein Datum zu erkennen: "
    "gefunden false und alle Zahlen 0."
)

_SCHEMA_MHD = {
    "type": "object",
    "properties": {
        "gefunden": {"type": "boolean"},
        "tag": {"type": "integer"},
        "monat": {"type": "integer"},
        "jahr": {"type": "integer"},
        "gelesen": {"type": "string"},
    },
    "required": ["gefunden", "tag", "monat", "jahr", "gelesen"],
    "additionalProperties": False,
}

BILDTYPEN = ("image/jpeg", "image/png", "image/webp")


async def mhd_lesen(bild_b64: str, typ: str = "image/jpeg") -> dict | None:
    """MHD vom Foto: {"datum": ISO oder "", "gelesen": Aufdruck}, None bei Fehler."""
    if not bild_b64 or not verfuegbar() or typ not in BILDTYPEN:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("KI: Paket 'anthropic' fehlt")
        return None
    try:
        async with anthropic.AsyncAnthropic(timeout=ZEITGRENZE_S, max_retries=1) as client:
            antwort = await client.beta.messages.create(
                model=MODELL,
                max_tokens=2000,
                system=_SYSTEM_MHD,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": typ, "data": bild_b64}},
                    {"type": "text", "text": "Welches MHD steht hier?"},
                ]}],
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": _SCHEMA_MHD}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
    except anthropic.APIStatusError as e:
        log.warning("KI: MHD-Anfrage abgelehnt (%s): %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("KI: keine Verbindung: %s", e)
        return None
    if antwort.stop_reason == "refusal":
        return None
    text = next((b.text for b in antwort.content if b.type == "text"), "")
    try:
        daten = json.loads(text)
    except json.JSONDecodeError:
        log.warning("KI: MHD-Antwort ist kein JSON: %r", text[:200])
        return None
    gelesen = (daten.get("gelesen") or "").strip()[:60]
    if not daten.get("gefunden"):
        return {"datum": "", "gelesen": gelesen}
    return {"datum": mhd_aus_teilen(daten.get("tag"), daten.get("monat"), daten.get("jahr")),
            "gelesen": gelesen}


async def _kategorien(session: AsyncSession) -> list[str]:
    return list((await session.execute(select(Category.name))).scalars().all())


async def produkt_anreichern(session: AsyncSession, produkt: Product) -> dict | None:
    """Vorschlag holen und am Produkt merken - Leeres wird gefuellt, nichts ueberschrieben."""
    if not await aktiv(session):
        return None
    vorschlag = await vorschlagen(produkt.name, produkt.brand, await _kategorien(session))
    if vorschlag is None:
        return None
    if not produkt.haltbarkeit and vorschlag["haltbarkeit"]:
        produkt.haltbarkeit = vorschlag["haltbarkeit"]
    if not produkt.category:
        produkt.category = vorschlag["kategorie"]
    if not produkt.subcategory and vorschlag["sorte"]:
        produkt.subcategory = vorschlag["sorte"]
    await session.flush()
    log.info("KI: %s -> %s, %s", produkt.name, vorschlag["kategorie"], vorschlag["haltbarkeit"])
    return vorschlag


def im_hintergrund(produkt_id: int | None) -> None:
    """Fuer das Terminal: nicht auf die Antwort warten, beim naechsten Mal ist sie da."""
    if not produkt_id or not verfuegbar() or produkt_id in _aktiv_aufgaben:
        return
    _aktiv_aufgaben.add(produkt_id)

    async def los():
        from ..db import session_scope
        try:
            async with session_scope() as session:
                produkt = await session.get(Product, produkt_id)
                if produkt is None or not produkt.name or produkt.haltbarkeit:
                    return
                await produkt_anreichern(session, produkt)
                await session.commit()
        except Exception:        # Hintergrund: nie den Server stoeren
            log.exception("KI: Hintergrundabfrage fehlgeschlagen")
        finally:
            _aktiv_aufgaben.discard(produkt_id)

    try:
        asyncio.get_running_loop().create_task(los())
    except RuntimeError:
        _aktiv_aufgaben.discard(produkt_id)
