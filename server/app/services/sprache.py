"""Sprachsteuerung: das Lager fuer einen Sprachassistenten (Home Assistant).

Jede Funktion liefert eine **gesprochene Antwort** (`antwort`, kurz, ohne
Tabellen) und einen `status`:

* `info`        - Auskunft, nichts geaendert
* `frage`       - es fehlt etwas; `fehlt` sagt was, `optionen` was geht
* `bestaetigen` - alles beisammen, `werte` noch einmal mit `bestaetigt: true`
                  schicken (ein verhoertes Wort soll kein Etikett drucken)
* `erledigt`    - geaendert

Gefuehrt wird also von hier: der Assistent fragt, was `antwort` fragt, und
schickt die Antwort mit allen bisherigen Werten zurueck. Der Zustand liegt
damit beim Aufrufer, nicht im Server - kein Gespraech kann "haengen bleiben".

Angelegt wird ueber denselben Weg wie im Web (`api.labels.etiketten_anlegen`),
ausgelagert ueber `inventory.remove_item` - siehe CLAUDE.md.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import InventoryItem, Location, Template
from . import categories, haltbarkeit, haushalte, mengen
from . import inventory as inv
from .dates import mhd_ohne_jahr, to_display, to_iso_date
from .verbrauch import wochenplan

GRUND = "sprache"
_MONATE = {
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}
_ZAHLWORTE = {"ein": 1, "eine": 1, "einen": 1, "einem": 1, "einer": 1, "zwei": 2, "drei": 3,
              "vier": 4, "fünf": 5, "fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8,
              "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12}
_KEIN_MHD = {"kein", "keins", "keines", "ohne", "nein", "keine", "kein mhd", "ohne mhd"}
_VORSCHLAG = {"vorschlag", "passt", "ja", "ok", "okay", "genau", "stimmt", "übernehmen"}


# ------------------------------------------------------------------ Hilfen
def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _zahl(text: str) -> int | None:
    text = _norm(text)
    if text.isdigit():
        return int(text)
    return _ZAHLWORTE.get(text)


def mhd_lesen(text: str | None, heute: date | None = None) -> str | None:
    """Gesprochenes Datum -> ISO. "" = ausdruecklich kein MHD, None = unlesbar.

    Versteht "morgen", "in 5 Tagen", "in zwei Wochen", "8.11.", "8. November",
    "8. November 2027", ISO und alles, was `to_iso_date` kennt.
    """
    heute = heute or date.today()
    t = _norm(text).rstrip(".")
    if not t:
        return None
    if t in _KEIN_MHD:
        return ""
    fest = {"heute": 0, "morgen": 1, "übermorgen": 2, "uebermorgen": 2}
    if t in fest:
        return (heute + timedelta(days=fest[t])).isoformat()
    t = t.replace("nächste woche", "in 1 woche").replace("naechste woche", "in 1 woche")
    m = re.fullmatch(r"(?:in\s+)?(\w+)\s+(tag|tagen|tage|woche|wochen|monat|monaten|monate|jahr|jahren|jahre)", t)
    if m and (n := _zahl(m.group(1))) is not None:
        einheit = m.group(2)
        if einheit.startswith("tag"):
            return (heute + timedelta(days=n)).isoformat()
        if einheit.startswith("woche"):
            return (heute + timedelta(weeks=n)).isoformat()
        monat = heute.month - 1 + (n if einheit.startswith("monat") else 12 * n)
        jahr, monat = heute.year + monat // 12, monat % 12 + 1
        tag = min(heute.day, [31, 29 if jahr % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][monat - 1])
        return date(jahr, monat, tag).isoformat()
    # "8. november 2027" / "8 november"
    m = re.fullmatch(r"(\d{1,2})\.?\s*([a-zäöü]+)\s*(\d{2,4})?", t)
    if m and m.group(2) in _MONATE:
        tag, monat = int(m.group(1)), _MONATE[m.group(2)]
        if m.group(3):
            jahr = int(m.group(3))
            return to_iso_date(f"{tag:02d}.{monat:02d}.{jahr + 2000 if jahr < 100 else jahr}") or None
        return mhd_ohne_jahr(tag, monat, heute) or None
    # "8.11." ohne Jahr
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", t)
    if m:
        return mhd_ohne_jahr(int(m.group(1)), int(m.group(2)), heute) or None
    return to_iso_date(t) or None


def menge_lesen(menge, einheit: str | None = "") -> tuple[float, str] | None:
    """ "500 g", "1,5 kg", 3 + "Stück" -> (Menge, Einheit) in g/ml/St."""
    if menge in (None, ""):
        return None
    text = str(menge).strip()
    if einheit:
        text = f"{text} {einheit}"
    gelesen = mengen.aus_angabe(text)
    if gelesen:
        return gelesen
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?|\w+)\s*(stück|stk|st\.?|packung(?:en)?|x)?\s*$", text, re.I)
    if m:
        zahl = m.group(1).replace(",", ".")
        wert = float(zahl) if re.fullmatch(r"\d+(?:\.\d+)?", zahl) else _zahl(zahl)
        if wert:
            return float(wert), "St."
    return None


def _menge_text(menge: float, einheit: str) -> str:
    text = mengen.text(menge, einheit)
    return text.replace("St.", "Stück")


def _datum(iso: str, am: bool = False) -> str:
    """Zum Vorlesen: "morgen", "heute", sonst "8.11.2026" (mit am=True "am 8.11.2026")."""
    if not iso:
        return "ohne MHD"
    tage = (date.fromisoformat(iso) - date.today()).days
    nah = {0: "heute", 1: "morgen", 2: "übermorgen", -1: "gestern"}.get(tage)
    return nah or (f"am {to_display(iso)}" if am else to_display(iso))


def _aufzaehlen(teile: list[str]) -> str:
    teile = [t for t in teile if t]
    if len(teile) <= 1:
        return "".join(teile)
    return ", ".join(teile[:-1]) + " und " + teile[-1]


async def _haushalt(session: AsyncSession, haushalt: str | None) -> str | None:
    return await haushalte.aufloesen(session, haushalt) if haushalt else None


async def _orte(session: AsyncSession) -> list[Location]:
    return list((await session.execute(
        select(Location).order_by(Location.sort_order, Location.name))).scalars())


def _passend(name: str, kandidaten: list[str]) -> list[str]:
    """Gleich (ohne Gross/Klein) vor enthalten - fuer gesprochene Namen."""
    n = _norm(name)
    gleich = [k for k in kandidaten if _norm(k) == n]
    if gleich:
        return gleich
    return [k for k in kandidaten if n and (n in _norm(k) or _norm(k) in n)]


def antwort(status: str, text: str, **mehr) -> dict:
    return {"status": status, "antwort": text, **mehr}


# --------------------------------------------------------------- Bestand
async def _aktive(session: AsyncSession, haushalt: str | None, name: str = "",
                  ort: str = "") -> list[InventoryItem]:
    stmt = select(InventoryItem).where(InventoryItem.status == "active")
    if haushalt:
        stmt = stmt.where(InventoryItem.household == haushalt)
    if name:
        nadel = f"%{name.strip()}%"
        stmt = stmt.where(or_(InventoryItem.name.ilike(nadel),
                              InventoryItem.subcategory.ilike(nadel),
                              InventoryItem.brand.ilike(nadel)))
    if ort:
        stmt = stmt.where(InventoryItem.location == ort)
    stmt = stmt.order_by(InventoryItem.expiry_date == "", InventoryItem.expiry_date)
    return list((await session.execute(stmt)).scalars())


def _gruppen(items: list[InventoryItem]) -> dict[str, list[InventoryItem]]:
    gruppen: dict[str, list[InventoryItem]] = {}
    for i in items:
        gruppen.setdefault(categories.display_name(i.name, i.subcategory), []).append(i)
    return gruppen


async def _ort_finden(session: AsyncSession, ort: str) -> tuple[str | None, list[str]]:
    namen = [o.name for o in await _orte(session)]
    treffer = _passend(ort, namen)
    return (treffer[0] if len(treffer) == 1 else None), namen


async def bestand(session: AsyncSession, *, name: str = "", ort: str = "",
                  haushalt: str | None = None) -> dict:
    """"Haben wir noch Milch?" / "Was ist im Gefrierschrank?" """
    hh = await _haushalt(session, haushalt)
    ort_name = ""
    if ort:
        ort_name, namen = await _ort_finden(session, ort)
        if not ort_name:
            return antwort("frage", f"Einen Lagerort „{ort}“ kenne ich nicht. Es gibt "
                           f"{_aufzaehlen(namen)}.", fehlt="ort", optionen=namen)
    items = await _aktive(session, hh, name, ort_name)
    wo = f" im {ort_name}" if ort_name else ""
    lager = f"Im {ort_name}" if ort_name else "Im Lager"
    if not items:
        was = f"„{name}“" if name else "etwas"
        return antwort("info", f"Ich finde {was}{wo} nicht im Lager." if name
                       else f"{lager} ist nichts.", anzahl=0)
    gruppen = _gruppen(items)
    if name:
        saetze = []
        for anzeige, liste in list(gruppen.items())[:4]:
            orte: dict[str, int] = {}
            for i in liste:
                orte[i.location or "ohne Ort"] = orte.get(i.location or "ohne Ort", 0) + 1
            ortstext = _aufzaehlen([f"{n} im {o}" if len(orte) > 1 else f"im {o}"
                                    for o, n in orte.items()])
            erstes = next((i.expiry_date for i in liste if i.expiry_date), "")
            mhd = f", das erste läuft {_datum(erstes, am=True)} ab" if erstes and len(liste) > 1 else (
                f", MHD {_datum(erstes)}" if erstes else "")
            saetze.append(f"{len(liste)} × {anzeige} {ortstext}{mhd}")
        mehr = f" Dazu noch {len(gruppen) - 4} weitere Sorten." if len(gruppen) > 4 else ""
        text = "Du hast " + "; ".join(saetze) + "." + mehr
    else:
        top = sorted(gruppen.items(), key=lambda g: -len(g[1]))[:8]
        packungen = "eine Packung" if len(items) == 1 else f"{len(items)} Packungen"
        sorten = "" if len(gruppen) == 1 else f", {len(gruppen)} verschiedene"
        text = (f"{lager} {'ist' if len(items) == 1 else 'sind'} {packungen}{sorten}: "
                + _aufzaehlen([f"{len(liste)} × {n}" if len(liste) > 1 else n for n, liste in top])
                + (" und weitere." if len(gruppen) > 8 else "."))
    return antwort("info", text, anzahl=len(items), sorten=len(gruppen))


async def ablauf(session: AsyncSession, *, haushalt: str | None = None) -> dict:
    """"Was muss weg?" - der Wochenplan als Satz."""
    plan = await wochenplan(session, await _haushalt(session, haushalt))
    if not plan:
        return antwort("info", "Bis Ende nächster Woche läuft nichts ab.", anzahl=0)
    saetze = []
    for g in plan[:4]:
        namen = list(dict.fromkeys(i["display_name"] for i in g["items"]))
        teile = _aufzaehlen(namen[:5]) + (f" und {len(namen) - 5} weitere" if len(namen) > 5 else "")
        titel = {"abgelaufen": "Schon abgelaufen", "naechste": "Nächste Woche"}.get(g["key"], g["titel"])
        saetze.append(f"{titel}: {teile}")
    return antwort("info", ". ".join(saetze) + ".",
                   anzahl=sum(len(g["items"]) for g in plan))


# -------------------------------------------------------------- Auslagern
async def auslagern(session: AsyncSession, *, name: str, ort: str = "",
                    weggeworfen: bool = False, haushalt: str | None = None) -> dict:
    """Die Packung mit dem fruehesten MHD auslagern."""
    if not (name or "").strip():
        return antwort("frage", "Was soll ich auslagern?", fehlt="name")
    hh = await _haushalt(session, haushalt)
    ort_name = ""
    if ort:
        ort_name, namen = await _ort_finden(session, ort)
        if not ort_name:
            return antwort("frage", f"Einen Lagerort „{ort}“ kenne ich nicht. Es gibt "
                           f"{_aufzaehlen(namen)}.", fehlt="ort", optionen=namen)
    items = await _aktive(session, hh, name, ort_name)
    if not items:
        return antwort("info", f"Ich finde „{name}“ nicht im Lager.", anzahl=0)
    gruppen = _gruppen(items)
    treffer = _passend(name, list(gruppen))
    if len(treffer) != 1 and len(gruppen) > 1:
        optionen = list(gruppen)[:6]
        return antwort("frage", f"Welches meinst du: {_aufzaehlen(optionen)}?",
                       fehlt="name", optionen=optionen)
    anzeige = treffer[0] if treffer else next(iter(gruppen))
    item = gruppen[anzeige][0]               # fruehestes MHD zuerst
    await inv.remove_item(session, item, reason="weggeworfen" if weggeworfen else GRUND,
                          household=hh)
    await session.commit()
    from ..device.hub import hub
    await hub.notify_ui("inventory")
    rest = len(gruppen[anzeige]) - 1
    was = "als weggeworfen ausgelagert" if weggeworfen else "ausgelagert"
    mhd = f" mit MHD {_datum(item.expiry_date)}" if item.expiry_date else ""
    noch = f" Noch {rest} übrig." if rest else " Das war die letzte."
    return antwort("erledigt", f"{anzeige}{mhd} aus dem {item.location or 'Lager'} {was}.{noch}",
                   label=item.label, rest=rest)


# --------------------------------------------------------- Etikett drucken
async def _vorlage(session: AsyncSession, name: str) -> Template | None:
    vorlagen = list((await session.execute(select(Template))).scalars())
    treffer = _passend(name, [v.name for v in vorlagen])
    if len(treffer) != 1:
        return None
    return next(v for v in vorlagen if v.name == treffer[0])


async def etikett(session: AsyncSession, *, name: str = "", menge=None, einheit: str = "",
                  mhd: str | None = None, ort: str = "", anzahl: int | None = None,
                  haushalt: str | None = None, terminal: str = "",
                  bestaetigt: bool = False, drucken: bool = True) -> dict:
    """Gefuehrt ein Etikett anlegen und drucken.

    Fragt der Reihe nach: Name, Lagerort, MHD (mit Vorschlag); Menge und
    Anzahl sind freiwillig. Erst mit `bestaetigt` wird angelegt.
    """
    name = (name or "").strip()
    if not name:
        return antwort("frage", "Was möchtest du einlagern?", fehlt="name")
    hh = await _haushalt(session, haushalt)
    if haushalt and not hh:
        namen = [h.name for h in await haushalte.alle(session)]
        return antwort("frage", f"Den Haushalt „{haushalt}“ gibt es nicht. Es gibt "
                       f"{_aufzaehlen(namen)}.", fehlt="haushalt", optionen=namen)

    vorlage = await _vorlage(session, name)
    if vorlage is not None:
        name = vorlage.name
    kategorie = vorlage.category if vorlage else ""

    # Lagerort: gefragt, sobald es mehr als einen gibt.
    orte = await _orte(session)
    if ort:
        ort_name, namen = await _ort_finden(session, ort)
        if not ort_name:
            return antwort("frage", f"Einen Lagerort „{ort}“ kenne ich nicht. Wohin kommt "
                           f"{name}: {_aufzaehlen(namen)}?", fehlt="ort", optionen=namen,
                           werte={"name": name})
    elif len(orte) == 1:
        ort_name = orte[0].name
    elif not orte:
        ort_name = ""
    else:
        namen = [o.name for o in orte]
        return antwort("frage", f"Wohin kommt {name}: {_aufzaehlen(namen)}?",
                       fehlt="ort", optionen=namen, werte={"name": name})

    # MHD: eigenes Datum, "kein", oder der Vorschlag aus Vorlage/Kategorie.
    vorschlag = await haltbarkeit.vorschlag(
        session, ort=ort_name, kategorie=kategorie,
        eigene_tage=vorlage.shelf_days if vorlage else 0,
        vorlage=vorlage.haltbarkeit if vorlage else None, herkunft="Vorlage")
    if mhd is None or _norm(mhd) in _VORSCHLAG or _norm(mhd) == "":
        if vorschlag["datum"]:
            mhd_iso = vorschlag["datum"]          # in der Bestaetigung genannt
        else:
            return antwort("frage", f"Welches MHD hat {name}? Du kannst auch „kein“ sagen.",
                           fehlt="mhd", werte={"name": name, "ort": ort_name})
    else:
        gelesen = mhd_lesen(mhd)
        if gelesen is None:
            return antwort("frage", f"Das Datum „{mhd}“ habe ich nicht verstanden. "
                           "Sag es zum Beispiel als „8. November“ oder „in 5 Tagen“.",
                           fehlt="mhd", werte={"name": name, "ort": ort_name})
        mhd_iso = gelesen
    # Gefrierschrank: ein aufgedrucktes MHD wird verlaengert (wie am Terminal).
    hinweis = ""
    if mhd_iso and mhd_iso != vorschlag["datum"]:
        neu = await haltbarkeit.eingefroren(session, mhd=mhd_iso, ort=ort_name, kategorie=kategorie)
        if neu.get("verlaengert"):
            hinweis = f" Aufgedruckt war {to_display(mhd_iso)}, im Gefrierschrank verlängert."
            mhd_iso = neu["datum"]

    gelesen_menge = menge_lesen(menge, einheit)
    if gelesen_menge:
        q, unit = gelesen_menge
    else:
        q, unit = 1.0, (vorlage.unit if vorlage else "")
    anzahl = max(1, min(20, int(anzahl or 1)))

    werte = {"name": name, "ort": ort_name, "mhd": mhd_iso or "kein", "menge": q,
             "einheit": unit, "anzahl": anzahl, "haushalt": hh or "", "drucken": drucken}
    mengentext = _menge_text(q, unit) if gelesen_menge or unit not in ("", "St.") else ""
    beschreibung = ", ".join(t for t in (
        f"{anzahl} Etiketten" if anzahl > 1 else "",
        name, mengentext, f"MHD {_datum(mhd_iso)}" if mhd_iso else "ohne MHD",
        f"in den {ort_name}" if ort_name else "") if t)
    if not bestaetigt:
        quelle = (f" Das MHD ist ein Vorschlag ({vorschlag['quelle']})."
                  if not hinweis and mhd_iso and mhd_iso == vorschlag["datum"]
                  and vorschlag["quelle"] else "")
        frage = "Soll ich das drucken?" if drucken else "Soll ich das einlagern?"
        return antwort("bestaetigen", f"{beschreibung}.{hinweis}{quelle} {frage}", werte=werte)

    from ..api.labels import etiketten_anlegen
    from ..schemas import LabelCreate

    device_id = await _terminal_id(session, hh, terminal) if terminal else None
    body = LabelCreate(name=name, category=kategorie, expiry_date=mhd_iso, quantity=q,
                       unit=unit, location=ort_name, count=anzahl, print=drucken,
                       device_id=device_id)
    try:
        ergebnis = await etiketten_anlegen(session, body, hh)
    except HTTPException as fehler:
        if fehler.status_code == 409 and isinstance(fehler.detail, dict):
            liste = fehler.detail.get("terminals", [])
            namen = [t["name"] for t in liste]
            return antwort("frage", f"An welchem Terminal soll ich drucken: {_aufzaehlen(namen)}?",
                           fehlt="terminal", optionen=namen, werte=werte,
                           terminals=liste)
        return antwort("fehler", f"Das ging nicht: {fehler.detail}")
    etiketten = _aufzaehlen(ergebnis.labels)
    if not drucken:
        text = f"{name} ist eingelagert, Etikett {etiketten}."
    elif ergebnis.printed:
        text = f"Gedruckt: {beschreibung}. Etikett {etiketten}."
    else:
        text = (f"{name} ist eingelagert. Das Etikett {etiketten} wartet, "
                "bis ein Drucker erreichbar ist.")
    return antwort("erledigt", text, labels=ergebnis.labels, gedruckt=ergebnis.printed)


async def _terminal_id(session: AsyncSession, haushalt: str | None, wunsch: str) -> str | None:
    """Gesprochener Terminalname -> device_id; unbekannt -> None (Standardweg)."""
    from ..device import workflow
    from ..models import Device

    ids = workflow.terminals_fuer(haushalt) if haushalt else workflow.drucker_online()
    if wunsch in ids:
        return wunsch
    namen = {d: (await session.execute(select(Device.name).where(Device.device_id == d)))
             .scalar_one_or_none() or d for d in ids}
    treffer = _passend(wunsch, list(namen.values()))
    return next((d for d, n in namen.items() if n == treffer[0]), None) if len(treffer) == 1 else None
