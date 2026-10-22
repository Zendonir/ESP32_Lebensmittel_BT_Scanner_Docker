"""Pydantic-Schemas fuer die REST-API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from .services import mengen
from .services.dates import to_iso_date


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- Stammdaten
Lagerart = Literal["", "raum", "kuehl", "tiefkuehl"]


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = "#1e88e5"
    icon: str = ""
    sort_order: int = 0
    # None = unveraendert lassen (aelterer Browser schickt das Feld nicht).
    haltbarkeit: dict | None = None   # bereinigt in services/haltbarkeit.bereinigen()


class CategoryOut(ORMModel):
    id: int
    name: str
    color: str
    icon: str
    sort_order: int
    haltbarkeit: dict | None = None

    # Fest hinterlegt, nicht gespeichert: die Oberflaeche soll die Sorten
    # anbieten koennen, ohne die Liste ein zweites Mal zu fuehren.
    @computed_field
    @property
    def subcategories(self) -> list[str]:
        from .services.categories import subcategories_for

        return list(subcategories_for(self.name))


class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    is_default: bool = False
    sort_order: int = 0
    # Fehlt sie (aelterer Browser), bleibt die Farbe, wie sie ist - bzw. beim
    # Anlegen kommt die naechste aus seed.ORT_FARBEN.
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    kind: Lagerart | None = None


class LocationOut(ORMModel):
    id: int
    name: str
    is_default: bool
    sort_order: int
    color: str
    kind: str = ""


class ProductIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)
    name: str = ""
    brand: str = ""
    category: str = ""
    amount: str = ""
    unit: str = ""
    default_shelf_days: int = 0


class ProductOut(ORMModel):
    id: int
    barcode: str
    name: str
    brand: str
    category: str
    amount: str
    unit: str
    nutriscore: str
    image_url: str
    default_shelf_days: int
    haltbarkeit: dict | None = None
    lagerart: str = ""
    source: str
    fetched_at: datetime | None


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str = ""
    shelf_days: int = 7
    haltbarkeit: dict | None = None   # Tage je Lagerart, None = unveraendert
    unit: str = ""
    brands: list[str] = Field(default_factory=list)
    use_sorten: bool = False
    sorten: list[str] = Field(default_factory=list)
    sort_order: int = 0

    # Vorlagen kennen nur g/ml/St. - aus "kg" wird "g" (services/mengen.py).
    @field_validator("unit")
    @classmethod
    def _einheit(cls, v: str) -> str:
        return mengen.einheit(v)


class TemplateOut(ORMModel):
    id: int
    name: str
    category: str
    shelf_days: int
    haltbarkeit: dict | None = None
    unit: str
    brands: list[str]
    use_sorten: bool
    sorten: list[str]
    sort_order: int


# --------------------------------------------------------------------- Inventar
class InventoryIn(BaseModel):
    """Anlage bzw. Aenderung eines Inventar-Eintrags."""

    name: str = Field(min_length=1, max_length=255)
    barcode: str = ""
    brand: str = ""
    category: str = ""
    subcategory: str = ""
    expiry_date: str = ""
    quantity: float = 1.0
    unit: str = ""
    location: str = ""
    note: str = ""

    @field_validator("expiry_date")
    @classmethod
    def _norm_date(cls, v: str) -> str:
        return to_iso_date(v)


class InventoryPatch(BaseModel):
    name: str | None = None
    brand: str | None = None
    category: str | None = None
    subcategory: str | None = None
    expiry_date: str | None = None
    quantity: float | None = None
    unit: str | None = None
    location: str | None = None
    note: str | None = None

    @field_validator("expiry_date")
    @classmethod
    def _norm_date(cls, v: str | None) -> str | None:
        return None if v is None else to_iso_date(v)


class InventoryOut(ORMModel):
    id: int
    label: str
    barcode: str
    name: str
    # Name mit Unterkategorie ("Filet - Schwein"). Abgeleitet, damit
    # Web-Interface, Geraetebildschirm und Etikett nicht auseinanderlaufen.
    display_name: str = ""
    brand: str
    category: str
    subcategory: str
    expiry_date: str
    added_date: str
    quantity: float
    unit: str
    location: str
    household: str
    note: str
    status: str
    removed_at: datetime | None
    removed_reason: str
    days_left: int | None = None


class LabelCreate(BaseModel):
    """Etikett(en) anlegen - der Web-Gegenpart zum Scan-Workflow am Geraet."""

    name: str = Field(min_length=1, max_length=255)
    barcode: str = ""
    brand: str = ""
    category: str = ""
    subcategory: str = ""
    expiry_date: str = ""
    quantity: float = 1.0
    unit: str = ""
    location: str = ""
    count: int = Field(default=1, ge=1, le=20)
    print: bool = True
    device_id: str | None = None
    # Druckziel: "brother", "terminal" oder leer = Standardziel.
    ziel: str = Field(default="", pattern="^(|brother|terminal)$")

    @field_validator("expiry_date")
    @classmethod
    def _norm_date(cls, v: str) -> str:
        return to_iso_date(v)


class LabelCreateResult(BaseModel):
    labels: list[str]
    printed: bool
    print_jobs: list[int] = Field(default_factory=list)


class RemoveRequest(BaseModel):
    label: str = ""
    barcode: str = ""
    reason: str = "manual"


class ScanRequest(BaseModel):
    code: str = Field(min_length=1)
    device_id: str | None = None


# --------------------------------------------------------------------- Geraete
class DeviceOut(ORMModel):
    id: int
    device_id: str
    name: str
    firmware: str
    ip: str
    active_location: str
    household: str = ""
    has_printer: bool
    online: bool
    last_seen: datetime | None
    telemetry: dict


class DevicePatch(BaseModel):
    name: str | None = None
    active_location: str | None = None
    household: str | None = None


class PrintJobOut(ORMModel):
    id: int
    label: str
    ziel: str = ""
    status: str
    attempts: int
    error: str
    created_at: datetime


class EventOut(ORMModel):
    id: int
    ts: datetime
    type: str
    label: str
    barcode: str
    name: str
    location: str
    device: str
    household: str = ""
    payload: dict


class Stats(BaseModel):
    total: int
    expiring: int
    expired: int
    by_category: dict[str, int]
    by_location: dict[str, int]
    added_30d: int
    removed_30d: int
