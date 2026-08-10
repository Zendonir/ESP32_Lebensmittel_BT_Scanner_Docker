"""Übernahme des Bestands aus dem ersten Projekt (Version 1).

Dort lag das Inventar als Event-Strom in einer MySQL-Tabelle
(`inventory_events`), aktueller Stand über den View `current_inventory`.
Dieser Importer nimmt genau dessen Spalten als CSV entgegen:

    label_barcode, barcode, name, brand, category, expiry_date,
    added_date, quantity, household, device_name, stored_at

Nur `name` ist zwingend; alles andere darf fehlen. Ein Bezug zu
Kategorien/Lagerorten wird nicht erzwungen - Zeichenketten werden
uebernommen, wie sie sind. Etiketten mit einem `label_barcode`, der schon
existiert, werden uebersprungen statt dupliziert (idempotent bei
Wiederholung nach einem Abbruch).

Was bewusst NICHT übernommen wird, weil es sich schneller neu anlegen
lässt als korrekt zuordnen: Vorlagen, Kategorien, Lagerorte. Die
Erstbefüllung (`services/seed.py`) liefert dafür ohnehin einen brauchbaren
Ausgangssatz.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import InventoryItem
from . import inventory as inv
from . import settings_store
from .dates import to_iso_date, today_iso

# Aliasnamen, damit sowohl der rohe current_inventory-Export als auch von
# Hand nachbearbeitete CSVs (z.B. mit deutschen Kopfzeilen) funktionieren.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "label": ("label_barcode", "label", "etikett", "lebnummer"),
    "barcode": ("barcode",),
    "name": ("name", "artikel"),
    "brand": ("brand", "marke"),
    "category": ("category", "kategorie"),
    "subcategory": ("subcategory", "sorte"),
    "expiry_date": ("expiry_date", "mhd"),
    "added_date": ("added_date", "stored_at", "eingang"),
    "quantity": ("quantity", "menge"),
    "unit": ("unit", "einheit"),
    "location": ("location", "lagerort", "ort"),
    "household": ("household", "haushalt"),
}


@dataclass
class ImportResult:
    imported: int = 0
    skipped_duplicate: int = 0
    skipped_invalid: int = 0
    errors: list[str] = field(default_factory=list)
    total_rows: int = 0


def _pick(row: dict[str, str], key: str) -> str:
    for alias in _COLUMN_ALIASES[key]:
        if alias in row and row[alias] is not None:
            value = row[alias].strip()
            if value:
                return value
    return ""


def _normalize_header(fieldnames: list[str]) -> dict[str, str]:
    """Kopfzeile robust gegen Groß-/Kleinschreibung und Leerraum machen."""
    return {name: (name or "").strip().lower() for name in fieldnames}


async def import_csv(
    session: AsyncSession, content: bytes, *, dry_run: bool = False
) -> ImportResult:
    """CSV aus Version 1 einspielen.

    `dry_run=True` prüft nur (Spaltenerkennung, Duplikate, Datumsformate),
    ohne etwas zu schreiben - sinnvoll, um eine Exportdatei vor dem
    eigentlichen Import zu kontrollieren.
    """
    result = ImportResult()

    text = content.decode("utf-8-sig", errors="replace")
    # Sowohl Komma- als auch Semikolon-getrennte Exporte akzeptieren
    # (mysql -B liefert Tabs, ein Tabellenkalkulations-Export oft Semikolon).
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        result.errors.append("Keine Kopfzeile gefunden")
        return result

    header_map = _normalize_header(reader.fieldnames)
    known = {alias for aliases in _COLUMN_ALIASES.values() for alias in aliases}
    if not (set(header_map.values()) & known):
        result.errors.append(
            "Keine bekannten Spalten erkannt - erwartet werden u.a. "
            "label_barcode, name, expiry_date, quantity"
        )
        return result

    existing_labels = {
        row[0]
        for row in (await session.execute(select(InventoryItem.label))).all()
    }
    default_location = await inv.default_location(session)
    added_date = today_iso()

    for raw_row in reader:
        result.total_rows += 1
        row = {header_map.get(k, k): (v or "") for k, v in raw_row.items() if k}

        name = _pick(row, "name")
        if not name:
            result.skipped_invalid += 1
            result.errors.append(f"Zeile {result.total_rows}: kein Name")
            continue

        label = _pick(row, "label")
        if not label:
            # Ohne Etikettennummer im Export einen stabilen Ersatz bilden,
            # damit ein wiederholter Import (nach Abbruch) dieselbe Zeile
            # wiedererkennt statt sie erneut anzulegen.
            label = f"IMPORT{abs(hash((name, _pick(row, 'barcode'), _pick(row, 'added_date')))) % 10**8:08d}"
        if label in existing_labels:
            result.skipped_duplicate += 1
            continue

        quantity_raw = _pick(row, "quantity")
        try:
            quantity = float(quantity_raw) if quantity_raw else 1.0
        except ValueError:
            quantity = 1.0

        item = InventoryItem(
            label=label,
            barcode=_pick(row, "barcode"),
            name=name,
            brand=_pick(row, "brand"),
            category=_pick(row, "category"),
            subcategory=_pick(row, "subcategory"),
            expiry_date=to_iso_date(_pick(row, "expiry_date")),
            added_date=to_iso_date(_pick(row, "added_date")) or added_date,
            quantity=quantity,
            unit=_pick(row, "unit"),
            location=_pick(row, "location") or default_location,
            household=_pick(row, "household") or settings.household,
            status="active",
            source_device="import",
        )

        if not dry_run:
            session.add(item)
            existing_labels.add(item.label)
            await inv.log_event(
                session,
                "import",
                label=item.label,
                barcode=item.barcode,
                name=item.name,
                location=item.location,
                payload={"source": "v1-csv"},
            )
        result.imported += 1

    if not dry_run and result.imported:
        # Der Etikettenzaehler muss ueber die importierten Nummern hinaus
        # weiterlaufen, sonst vergibt next_labels() Nummern, die als
        # IMPORT-Etikett schon vergeben sind.
        max_seen = 0
        prefix = settings.label_prefix.upper()
        for label in existing_labels:
            upper = label.upper()
            if upper.startswith(prefix) and upper[len(prefix) :].isdigit():
                max_seen = max(max_seen, int(upper[len(prefix) :]))
        if max_seen:
            current = int(await settings_store.get(session, "label_counter", 0))
            if max_seen > current:
                await settings_store.put(session, "label_counter", max_seen)

        await session.commit()

    return result
