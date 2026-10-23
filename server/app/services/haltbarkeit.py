"""Wie lange etwas haelt - je nachdem, wo es liegt.

Ein Lagerort hat eine Lagerart (Raumtemperatur, Kuehlschrank, Gefrier-
schrank). Jede Kategorie traegt eine Haltbarkeit in Tagen je Lagerart, ein
Produkt kann eine eigene haben (von Hand oder als KI-Vorschlag). Daraus
entsteht der MHD-Vorschlag beim Einlagern - am Terminal, am Handy und in der
Verwaltung derselbe, weil alle hier rechnen.

Reihenfolge, was gilt:
  1. Produkt bzw. Vorlage, Lagerart passend   (Hackfleisch im Gefrierschrank: 90)
  2. eigene Tage von Produkt/Vorlage     - nur ausserhalb des Gefrierschranks:
                                           "7 Tage" meint gekuehlt, nicht gefroren
  3. Kategorie, Lagerart passend
0 heisst ueberall "keine Angabe"; ist am Ende nichts da, bleibt das MHD leer.

Das aufgedruckte MHD und der Gefrierschrank (`eingefroren()`): Wer frische
Ware einfriert, verlaengert sie - das Datum wird auf den Gefrier-Vorschlag
angehoben, nie verkuerzt. Ware, die schon tiefgekuehlt verkauft wird
(`Product.lagerart` aus OpenFoodFacts, sonst Kategorie "Tiefkühl"), behaelt
ihr aufgedrucktes MHD 1:1 - der Hersteller hat es fuer den Gefrierschrank
berechnet.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Category, Location, Product
from .dates import shift_iso

ARTEN: dict[str, str] = {
    "raum": "Raumtemperatur",
    "kuehl": "Kühlschrank",
    "tiefkuehl": "Gefrierschrank",
}

# Vorgaben fuer die mitgelieferten Kategorien, in Tagen (ungeoeffnet, grob
# nach den ueblichen Empfehlungen). 0 = dort lagert man das nicht.
STANDARD: dict[str, dict[str, int]] = {
    "Getränke":        {"raum": 365, "kuehl": 365, "tiefkuehl": 0},
    "Milchprodukte":   {"raum": 0,   "kuehl": 7,   "tiefkuehl": 90},
    "Fleisch & Fisch": {"raum": 0,   "kuehl": 2,   "tiefkuehl": 180},
    "Obst & Gemüse":   {"raum": 5,   "kuehl": 7,   "tiefkuehl": 240},
    "Backwaren":       {"raum": 3,   "kuehl": 5,   "tiefkuehl": 90},
    "Tiefkühl":        {"raum": 0,   "kuehl": 1,   "tiefkuehl": 180},
    "Konserven":       {"raum": 730, "kuehl": 730, "tiefkuehl": 0},
    "Trockenware":     {"raum": 365, "kuehl": 365, "tiefkuehl": 0},
    "Süßes & Snacks":  {"raum": 180, "kuehl": 180, "tiefkuehl": 180},
    "Sonstiges":       {"raum": 30,  "kuehl": 14,  "tiefkuehl": 90},
}


def bereinigen(werte) -> dict[str, int]:
    """Nur bekannte Lagerarten, nur ganze Tage zwischen 0 und 10 Jahren."""
    aus: dict[str, int] = {}
    if not isinstance(werte, dict):
        return aus
    for art in ARTEN:
        try:
            tage = int(werte.get(art) or 0)
        except (TypeError, ValueError):
            continue
        if tage > 0:
            aus[art] = min(tage, 3650)
    return aus


def art_raten(name: str) -> str:
    """Lagerart aus dem Namen eines Orts - fuer alte Bestaende und Vorschlaege."""
    n = (name or "").lower()
    if any(w in n for w in ("gefrier", "truhe", "tiefkühl", "tiefkuehl", "frost", "eis")):
        return "tiefkuehl"
    if any(w in n for w in ("kühl", "kuehl", "fridge")):
        return "kuehl"
    return "raum"


async def art_von_ort(session: AsyncSession, ort: str) -> str:
    if not ort:
        return ""
    row = (await session.execute(select(Location).where(Location.name == ort))).scalar_one_or_none()
    if row is None:
        return art_raten(ort)
    return row.kind or art_raten(row.name)


async def vorschlag(
    session: AsyncSession,
    *,
    ort: str,
    kategorie: str = "",
    produkt: Product | None = None,
    eigene_tage: int = 0,
    vorlage: dict | None = None,
    herkunft: str = "Produkt",       # wie die eigenen Tage heissen: Produkt/Vorlage
) -> dict:
    """MHD-Vorschlag: {"tage", "datum", "quelle", "art"}. datum "" = keiner."""
    art = await art_von_ort(session, ort)
    tage, quelle = 0, ""

    if vorlage and art:
        tage = int(vorlage.get(art) or 0)
        quelle = f"Vorlage · {ARTEN[art]}" if tage else ""
    if not tage and produkt is not None and art:
        tage = int((produkt.haltbarkeit or {}).get(art) or 0)
        quelle = "Produkt" if tage else ""
    if not tage and art != "tiefkuehl":
        tage = int(eigene_tage or (produkt.default_shelf_days if produkt else 0) or 0)
        quelle = herkunft if tage else ""
    if not tage and kategorie and art:
        row = (await session.execute(
            select(Category).where(Category.name == kategorie))).scalar_one_or_none()
        werte = (row.haltbarkeit if row is not None and row.haltbarkeit else
                 STANDARD.get(kategorie, {}))
        tage = int(werte.get(art) or 0)
        quelle = f"{kategorie} · {ARTEN[art]}" if tage else ""

    return {"tage": tage, "datum": shift_iso(tage) if tage else "",
            "quelle": quelle, "art": art}


GEFROREN_KATEGORIE = "Tiefkühl"


def schon_gefroren(produkt: Product | None, kategorie: str = "") -> bool:
    """Wird es tiefgekuehlt verkauft? Die Angabe am Produkt geht vor."""
    if produkt is not None and (produkt.lagerart or produkt.lagerart_fest):
        return produkt.lagerart == "tiefkuehl"
    return (kategorie or (produkt.category if produkt else "")) == GEFROREN_KATEGORIE


def gefroren_setzen(produkt: Product, gefroren: bool) -> bool:
    """Haken "Im Original gefroren" ans Produkt schreiben - nur wenn er von
    dem abweicht, was ohnehin gilt. Dann ist es von Hand festgelegt.

    Verglichen wird wie beim Scan, der den Haken vorbelegt: mit dem Produkt
    und seiner eigenen Kategorie, nicht mit der in der Maske gewaehlten.
    """
    if schon_gefroren(produkt) == gefroren:
        return False
    if gefroren:
        produkt.lagerart = "tiefkuehl"
    elif produkt.lagerart == "tiefkuehl":
        produkt.lagerart = ""
    produkt.lagerart_fest = True
    return True


async def eingefroren(session: AsyncSession, *, mhd: str, ort: str, kategorie: str = "",
                      produkt: Product | None = None, gefroren: bool | None = None,
                      **vorschlag_kw) -> dict:
    """Aufgedrucktes MHD fuer den Lagerort: {"datum", "verlaengert", "hinweis"}.

    Ausserhalb des Gefrierschranks und bei schon gefrorener Ware bleibt es,
    wie es ist. Sonst gilt das spaetere von aufgedrucktem MHD und
    Gefrier-Vorschlag (`vorschlag()`, gleiche Reihenfolge wie dort).
    `gefroren` ueberstimmt, was am Produkt steht (Haken in der Maske).
    """
    gleich = {"datum": mhd, "verlaengert": False, "hinweis": ""}
    if not mhd or await art_von_ort(session, ort) != "tiefkuehl":
        return gleich
    if schon_gefroren(produkt, kategorie) if gefroren is None else gefroren:
        return {**gleich, "hinweis": "schon tiefgekühlt – MHD bleibt"}
    v = await vorschlag(session, ort=ort, kategorie=kategorie, produkt=produkt, **vorschlag_kw)
    if v["datum"] and v["datum"] > mhd:
        return {"datum": v["datum"], "verlaengert": True,
                "hinweis": f"eingefroren – {v['quelle']} · {v['tage']} Tage"}
    return gleich
