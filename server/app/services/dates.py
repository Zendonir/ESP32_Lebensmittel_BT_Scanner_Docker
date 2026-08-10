"""Datums-Normalisierung.

Im ersten Projekt lagen MHD-Werte gemischt als `YYYY-MM-DD` (Vorlagen-Workflow)
und `DD.MM.YYYY` (manuelle Eingabe) in derselben Spalte; das Web-UI musste beim
Anzeigen raten. Hier gilt: *intern ist alles ISO*, konvertiert wird genau an
dieser Stelle.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

ISO = "%Y-%m-%d"
_FORMATS = (ISO, "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y")


def to_iso_date(value: str | date | datetime | None) -> str:
    """Alles, was nach Datum aussieht, nach `YYYY-MM-DD`. Sonst ""."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    raw = str(value).strip()
    if not raw:
        return ""
    for fmt in _FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    # Manche Scanner liefern MHD als kompakte Ziffernfolge (DDMMYYYY/YYYYMMDD).
    digits = "".join(c for c in raw if c.isdigit())
    for fmt in ("%d%m%Y", "%Y%m%d", "%d%m%y"):
        if len(digits) == len(datetime(2000, 1, 1).strftime(fmt)):
            try:
                return datetime.strptime(digits, fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def to_display(iso: str) -> str:
    """ISO -> `DD.MM.YYYY` fuer Etikett und Display."""
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, ISO).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def today_iso() -> str:
    return date.today().isoformat()


def shift_iso(days: int, base: date | None = None) -> str:
    return ((base or date.today()) + timedelta(days=days)).isoformat()


def days_left(iso: str) -> int | None:
    """Tage bis MHD; negativ = abgelaufen; None = kein Datum gesetzt."""
    if not iso:
        return None
    try:
        target = datetime.strptime(iso, ISO).date()
    except ValueError:
        return None
    return (target - date.today()).days
