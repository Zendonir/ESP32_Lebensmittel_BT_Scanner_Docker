"""Übernahme des Bestands aus dem ersten Projekt (Version 1).

Zwei Quellen, zwei Funktionen:

* `import_csv` - der MySQL-View `current_inventory` als CSV. Deckt den
  aktiven Bestand ab.
* `import_sd_json` / `import_sd_zip` - die taegliche Sicherung von der
  SD-Karte (`/scanner_backup/inventory.json` und `removed_items.json`).
  `removed_items.json` gibt es in der MySQL-Tabelle nicht - das ist die
  einzige Quelle fuer den Auslager-Verlauf.

Nur `name` ist jeweils zwingend; alles andere darf fehlen. Ein Bezug zu
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
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

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

    await _finalize(session, existing_labels, dry_run, result)
    return result


async def _finalize(
    session: AsyncSession, all_labels: set[str], dry_run: bool, result: ImportResult
) -> None:
    """Etikettenzaehler nachziehen und committen.

    Der Zaehler muss ueber die importierten Nummern hinaus weiterlaufen,
    sonst vergibt next_labels() spaeter Nummern, die als importiertes
    Etikett schon vergeben sind.
    """
    if dry_run or not result.imported:
        return

    max_seen = 0
    prefix = settings.label_prefix.upper()
    for label in all_labels:
        upper = label.upper()
        if upper.startswith(prefix) and upper[len(prefix) :].isdigit():
            max_seen = max(max_seen, int(upper[len(prefix) :]))
    if max_seen:
        current = int(await settings_store.get(session, "label_counter", 0))
        if max_seen > current:
            await settings_store.put(session, "label_counter", max_seen)

    await session.commit()


# ---------------------------------------------------------------------------
# SD-Sicherung (inventory.json / removed_items.json)
# ---------------------------------------------------------------------------
def _sd_date(value: str) -> str:
    """SD-Backups speichern MHD/Eingang als DD.MM.YYYY (oder ISO nach einer
    fruehen Migration im ersten Projekt) - beides deckt to_iso_date() ab."""
    return to_iso_date(value)


def _looks_like_removed_items(rows: list[dict]) -> bool:
    return bool(rows) and "removedAt" in rows[0]


async def _import_rows(
    session: AsyncSession,
    rows: list[dict],
    *,
    status: str,
    dry_run: bool,
    result: ImportResult,
    existing_labels: set[str],
) -> None:
    added_date_fallback = today_iso()

    for row in rows:
        result.total_rows += 1
        name = str(row.get("name") or "").strip()
        if not name:
            result.skipped_invalid += 1
            result.errors.append(f"Zeile {result.total_rows}: kein Name")
            continue

        label = str(row.get("labelBarcode") or "").strip()
        if not label:
            barcode = str(row.get("barcode") or "")
            added = str(row.get("addedDate") or "")
            label = f"IMPORT{abs(hash((name, barcode, added))) % 10**8:08d}"
        if label in existing_labels:
            result.skipped_duplicate += 1
            continue

        try:
            quantity = float(row.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 1.0

        item = InventoryItem(
            label=label,
            barcode=str(row.get("barcode") or ""),
            name=name,
            brand=str(row.get("brand") or ""),
            category=str(row.get("category") or ""),
            subcategory=str(row.get("subcategory") or ""),
            expiry_date=_sd_date(row.get("expiryDate") or ""),
            added_date=_sd_date(row.get("addedDate") or "") or added_date_fallback,
            quantity=quantity,
            unit=str(row.get("unit") or ""),
            location=str(row.get("location") or "") or await inv.default_location(session),
            household=settings.household,
            status=status,
            source_device="import",
        )

        if status == "removed":
            removed_at = row.get("removedAt")
            if isinstance(removed_at, (int, float)) and removed_at > 0:
                item.removed_at = datetime.fromtimestamp(removed_at, tz=timezone.utc)
            item.removed_reason = "import"

        if not dry_run:
            session.add(item)
            existing_labels.add(label)
            await inv.log_event(
                session,
                "import",
                label=label,
                barcode=item.barcode,
                name=item.name,
                location=item.location,
                payload={"source": "v1-sd", "status": status},
            )
        result.imported += 1


async def import_sd_json(
    session: AsyncSession, content: bytes, *, dry_run: bool = False
) -> ImportResult:
    """Eine einzelne JSON-Datei aus `/scanner_backup/` einspielen.

    Erkennt anhand des ersten Eintrags, ob es sich um `inventory.json`
    (aktiver Bestand) oder `removed_items.json` (Verlauf, Feld `removedAt`)
    handelt - der Dateiname selbst wird nicht ausgewertet, falls er beim
    Herunterladen von der Karte veraendert wurde.
    """
    result = ImportResult()
    try:
        rows = json.loads(content.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        result.errors.append(f"Ungueltiges JSON: {exc}")
        return result

    if not isinstance(rows, list):
        result.errors.append("Erwartet wird ein JSON-Array (wie inventory.json)")
        return result

    status = "removed" if _looks_like_removed_items(rows) else "active"
    existing_labels = {
        row[0] for row in (await session.execute(select(InventoryItem.label))).all()
    }
    await _import_rows(
        session, rows, status=status, dry_run=dry_run,
        result=result, existing_labels=existing_labels,
    )
    await _finalize(session, existing_labels, dry_run, result)
    return result


async def import_sd_zip(
    session: AsyncSession, content: bytes, *, dry_run: bool = False
) -> ImportResult:
    """Das gesamte `/scanner_backup/`-Verzeichnis als ZIP einspielen.

    Verarbeitet `inventory.json` und `removed_items.json`, wo immer sie im
    Archiv liegen (direkt am Wurzelverzeichnis oder in einem Unterordner -
    ein Backup-Ordner wird oft 1:1 gezippt). Andere Dateien im Archiv
    (categories.json, locations.json, Einstellungen) werden ignoriert; die
    lassen sich schneller im Web-Interface neu anlegen als zuverlaessig
    zuzuordnen.
    """
    result = ImportResult()
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        result.errors.append("Keine gueltige ZIP-Datei")
        return result

    targets = {name.lower().rsplit("/", 1)[-1]: name for name in archive.namelist()}
    wanted = [n for key, n in targets.items() if key in ("inventory.json", "removed_items.json")]
    if not wanted:
        result.errors.append(
            "Weder inventory.json noch removed_items.json im Archiv gefunden"
        )
        return result

    existing_labels = {
        row[0] for row in (await session.execute(select(InventoryItem.label))).all()
    }

    for name in wanted:
        try:
            rows = json.loads(archive.read(name).decode("utf-8-sig", errors="replace"))
        except (json.JSONDecodeError, KeyError) as exc:
            result.errors.append(f"{name}: {exc}")
            continue
        if not isinstance(rows, list):
            result.errors.append(f"{name}: kein JSON-Array")
            continue
        status = "removed" if _looks_like_removed_items(rows) else "active"
        await _import_rows(
            session, rows, status=status, dry_run=dry_run,
            result=result, existing_labels=existing_labels,
        )

    await _finalize(session, existing_labels, dry_run, result)
    return result
