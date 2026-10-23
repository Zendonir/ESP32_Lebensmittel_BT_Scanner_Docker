"""Vorlagen und Stammdaten als Datei: exportieren und wieder einlesen.

Fuer den Umzug auf eine zweite Installation, zum Teilen von Vorlagen und als
Sicherung dessen, was man von Hand gepflegt hat. Der Bestand gehoert nicht
dazu - dafuer gibt es die Datenbank-Sicherung unter System.

Teile: Haushalte, Kategorien (mit Haltbarkeit), Lagerorte (mit Lagerart),
Vorlagen und der Produkt-Cache (Barcodes mit Namen, Haltbarkeit und dem
Haken "Im Original gefroren").

Zugeordnet wird ueber den Namen (Vorlagen: Name + Kategorie, Produkte: der
Barcode). Zwei Arten:
  ergaenzen       nur was fehlt, Vorhandenes bleibt unangetastet
  ueberschreiben  Fehlendes anlegen, Vorhandenes mit den Werten der Datei
Geloescht wird nie etwas. Mit `probe=True` wird nur gezaehlt.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Category, Household, Location, Product, Template
from . import haltbarkeit

ART = "lebensmittel-stammdaten"
VERSION = 1
TEILE = ("haushalte", "kategorien", "lagerorte", "vorlagen", "produkte")
MODI = ("ergaenzen", "ueberschreiben")


class Ungueltig(ValueError):
    """Die Datei ist keine Stammdaten-Datei dieses Programms."""


def _text(wert, laenge: int) -> str:
    return str(wert if wert is not None else "").strip()[:laenge]


def _zahl(wert, vorgabe: int = 0) -> int:
    try:
        return int(wert)
    except (TypeError, ValueError):
        return vorgabe


def _liste(wert) -> list[str]:
    if not isinstance(wert, list):
        return []
    aus: list[str] = []
    for w in wert:
        t = _text(w, 100)
        if t and t not in aus:
            aus.append(t)
    return aus


# Je Teil: Modell, Schluessel, Felder mit Bereinigung. Was in der Datei
# fehlt, bleibt beim Ueberschreiben, wie es ist.
FELDER = {
    "haushalte": (Household, ("name",), {
        "sort_order": _zahl,
    }),
    "kategorien": (Category, ("name",), {
        "color": lambda v: _text(v, 16) or "#1e88e5",
        "icon": lambda v: _text(v, 32),
        "sort_order": _zahl,
        "haltbarkeit": haltbarkeit.bereinigen,
    }),
    "lagerorte": (Location, ("name",), {
        "is_default": bool,
        "sort_order": _zahl,
        "color": lambda v: _text(v, 16),
        "kind": lambda v: v if v in haltbarkeit.ARTEN else "",
    }),
    "vorlagen": (Template, ("name", "category"), {
        "shelf_days": lambda v: _zahl(v, 7),
        "haltbarkeit": haltbarkeit.bereinigen,
        "unit": lambda v: _text(v, 16),
        "brands": _liste,
        "use_sorten": bool,
        "sorten": _liste,
        "sort_order": _zahl,
    }),
    "produkte": (Product, ("barcode",), {
        "name": lambda v: _text(v, 255),
        "brand": lambda v: _text(v, 255),
        "category": lambda v: _text(v, 100),
        "subcategory": lambda v: _text(v, 100),
        "amount": lambda v: _text(v, 64),
        "unit": lambda v: _text(v, 16),
        "default_shelf_days": _zahl,
        "haltbarkeit": haltbarkeit.bereinigen,
        "lagerart": lambda v: v if v in haltbarkeit.ARTEN else "",
        "lagerart_fest": bool,
        # Sonst fragte jedes umgezogene OFF-Produkt beim naechsten Scan erneut
        # (openfoodfacts._lagerart_fehlt) und koennte die Lagerart ueberschreiben.
        "lagerart_abgefragt": bool,
        "source": lambda v: v if v in ("off", "manual") else "manual",
    }),
}
def _schluessel(teil: str, eintrag: dict) -> tuple:
    return tuple(_text(eintrag.get(f), 255) for f in FELDER[teil][1])


async def exportieren(session: AsyncSession, teile=TEILE) -> dict:
    daten: dict = {"art": ART, "version": VERSION,
                   "exportiert": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for teil in teile:
        modell, schluessel, felder = FELDER[teil]
        zeilen = (await session.execute(select(modell))).scalars().all()
        eintraege = [{f: getattr(z, f) for f in (*schluessel, *felder)} for z in zeilen]
        daten[teil] = sorted(eintraege, key=lambda e: [str(e[f]) for f in schluessel])
    return daten


async def importieren(session: AsyncSession, daten, *, teile=TEILE, modus: str = "ergaenzen",
                      probe: bool = False) -> dict:
    """Liest eine Datei aus `exportieren()` ein. Ergebnis je Teil:
    {"neu", "geaendert", "gleich", "uebersprungen"}."""
    if not isinstance(daten, dict) or daten.get("art") != ART:
        raise Ungueltig("Keine Stammdaten-Datei (Export aus Stammdaten → Import/Export)")
    if _zahl(daten.get("version")) > VERSION:
        raise Ungueltig("Datei aus einer neueren Version – erst den Server aktualisieren")
    if modus not in MODI:
        raise Ungueltig(f"Unbekannte Art: {modus}")

    ergebnis: dict = {}
    for teil in teile:
        eintraege = daten.get(teil)
        if not isinstance(eintraege, list):
            continue
        modell, schluessel, felder = FELDER[teil]
        vorhanden = {
            _schluessel(teil, {f: getattr(z, f) for f in schluessel}): z
            for z in (await session.execute(select(modell))).scalars().all()
        }
        zaehler = {"neu": 0, "geaendert": 0, "gleich": 0, "uebersprungen": 0}
        for eintrag in eintraege:
            if not isinstance(eintrag, dict):
                zaehler["uebersprungen"] += 1
                continue
            key = _schluessel(teil, eintrag)
            if not key[0]:
                zaehler["uebersprungen"] += 1
                continue
            werte = {f: bereinigen(eintrag[f]) for f, bereinigen in felder.items() if f in eintrag}
            zeile = vorhanden.get(key)
            if zeile is None:
                zaehler["neu"] += 1
                if not probe:
                    zeile = modell(**dict(zip(schluessel, key)), **werte)
                    session.add(zeile)
                    vorhanden[key] = zeile
                continue
            anders = {f: w for f, w in werte.items() if getattr(zeile, f) != w}
            if modus == "ergaenzen" or not anders:
                zaehler["gleich" if not anders else "uebersprungen"] += 1
                continue
            zaehler["geaendert"] += 1
            if not probe:
                for f, w in anders.items():
                    setattr(zeile, f, w)       # neue Listen/dicts - SQLAlchemy merkt es
        ergebnis[teil] = zaehler

    if not probe:
        await session.flush()
        await _ein_standardort(session)
    return ergebnis


async def _ein_standardort(session: AsyncSession) -> None:
    """Hoechstens ein Lagerort ist Standard - der importierte gewinnt nicht
    blind, sondern der zuletzt geaenderte."""
    orte = (await session.execute(select(Location).where(Location.is_default.is_(True))
                                  .order_by(Location.updated_at.desc(), Location.id.desc()))).scalars().all()
    for ort in orte[1:]:
        ort.is_default = False
