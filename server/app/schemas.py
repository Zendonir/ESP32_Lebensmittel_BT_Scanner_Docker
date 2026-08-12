"""Pydantic-Schemas fuer die REST-API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .services.dates import to_iso_date


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- Stammdaten
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = "#1e88e5"
    icon: str = ""
    sort_order: int = 0


class CategoryOut(ORMModel):
    id: int
    name: str
    color: str
    icon: str
    sort_order: int


class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    is_default: bool = False
    sort_order: int = 0


class LocationOut(ORMModel):
    id: int
    name: str
    is_default: bool
    sort_order: int


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
    source: str
    fetched_at: datetime | None


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str = ""
    shelf_days: int = 7
    unit: str = ""
    brands: list[str] = Field(default_factory=list)
    use_sorten: bool = False
    sorten: list[str] = Field(default_factory=list)
    sort_order: int = 0


class TemplateOut(ORMModel):
    id: int
    name: str
    category: str
    shelf_days: int
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


# --------------------------------------------------------------------- Einkaufsliste
class ShoppingIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    quantity: float = 1.0
    unit: str = ""
    note: str = ""
    done: bool = False


class ShoppingOut(ORMModel):
    id: int
    name: str
    quantity: float
    unit: str
    note: str
    done: bool
    auto_added: bool


# --------------------------------------------------------------------- Geraete
class DeviceOut(ORMModel):
    id: int
    device_id: str
    name: str
    firmware: str
    ip: str
    active_location: str
    has_printer: bool
    online: bool
    last_seen: datetime | None
    telemetry: dict


class DevicePatch(BaseModel):
    name: str | None = None
    active_location: str | None = None


class PrintJobOut(ORMModel):
    id: int
    label: str
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
    payload: dict


class Stats(BaseModel):
    total: int
    expiring: int
    expired: int
    by_category: dict[str, int]
    by_location: dict[str, int]
    added_30d: int
    removed_30d: int
