"""ORM-Modelle.

Bewusster Unterschied zum Vorgaenger: das Inventar ist eine *Tabelle mit
Zustand* plus ein append-only Event-Log. Im ersten Projekt musste der aktuelle
Bestand ueber einen View aus dem Event-Strom rekonstruiert werden
(`current_inventory`), was bei jedem Sync-Aussetzer auseinanderlief.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------
# Stammdaten
# --------------------------------------------------------------------------
class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    color: Mapped[str] = mapped_column(String(16), default="#1e88e5")
    icon: Mapped[str] = mapped_column(String(32), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Location(Base, TimestampMixin):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Product(Base, TimestampMixin):
    """Produkt-Stammsatz je Barcode - zugleich der OpenFoodFacts-Cache.

    Im ersten Projekt lag dieser Cache als JSON auf LittleFS und ging bei jedem
    Format/OTA verloren. Hier ueberlebt er in der Datenbank.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    brand: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(100), default="")
    subcategory: Mapped[str] = mapped_column(String(100), default="")
    amount: Mapped[str] = mapped_column(String(64), default="")  # z.B. "500 g"
    unit: Mapped[str] = mapped_column(String(16), default="")
    nutriscore: Mapped[str] = mapped_column(String(4), default="")
    image_url: Mapped[str] = mapped_column(String(512), default="")
    default_shelf_days: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # off|manual
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Template(Base, TimestampMixin):
    """Vorlage fuer Produkte ohne Barcode (Eigenes/Abgefuelltes)."""

    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(100), default="")
    shelf_days: Mapped[int] = mapped_column(Integer, default=7)
    unit: Mapped[str] = mapped_column(String(16), default="")
    brands: Mapped[list] = mapped_column(JSON, default=list)
    use_sorten: Mapped[bool] = mapped_column(Boolean, default=False)
    sorten: Mapped[list] = mapped_column(JSON, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("name", "category", name="uq_template_name_cat"),)


# --------------------------------------------------------------------------
# Bestand
# --------------------------------------------------------------------------
class InventoryItem(Base, TimestampMixin):
    """Ein physisches Etikett = eine Zeile. `label` ist der Primaerschluessel
    der realen Welt (aufgedruckt als Code128 + QR)."""

    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    barcode: Mapped[str] = mapped_column(String(64), default="", index=True)
    name: Mapped[str] = mapped_column(String(255))
    brand: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(100), default="", index=True)
    subcategory: Mapped[str] = mapped_column(String(100), default="")
    expiry_date: Mapped[str] = mapped_column(String(10), default="", index=True)  # ISO
    added_date: Mapped[str] = mapped_column(String(10), default="")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit: Mapped[str] = mapped_column(String(16), default="")
    location: Mapped[str] = mapped_column(String(100), default="", index=True)
    household: Mapped[str] = mapped_column(String(100), default="Standard", index=True)
    note: Mapped[str] = mapped_column(Text, default="")

    # Lebenszyklus
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # active | removed
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    removed_reason: Mapped[str] = mapped_column(String(32), default="")
    source_device: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (Index("ix_inventory_status_expiry", "status", "expiry_date"),)


class Event(Base):
    """Append-only Protokoll. Dient der Nachvollziehbarkeit und dem Verbrauchs-
    Statistik-Report - nicht mehr der Bestandsrekonstruktion."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    # add | remove | restore | edit | print | scan_unknown | device | notify
    label: Mapped[str] = mapped_column(String(32), default="", index=True)
    barcode: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    location: Mapped[str] = mapped_column(String(100), default="")
    device: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ShoppingItem(Base, TimestampMixin):
    __tablename__ = "shopping_list"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_added: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------
# Geraete & Betrieb
# --------------------------------------------------------------------------
class Device(Base, TimestampMixin):
    """Ein registriertes ESP32-Terminal."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    firmware: Mapped[str] = mapped_column(String(32), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    active_location: Mapped[str] = mapped_column(String(100), default="")
    has_printer: Mapped[bool] = mapped_column(Boolean, default=False)
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Letzte Telemetrie: heap, psram, rssi, scanner_battery, uptime, ...
    telemetry: Mapped[dict] = mapped_column(JSON, default=dict)

    print_jobs: Mapped[list["PrintJob"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class PrintJob(Base):
    """Druckauftrag fuer ein Geraet.

    Der Server rendert das Etikett vollstaendig in ESC/POS-nahe Felder; das
    Geraet schiebt sie nur noch auf die UART. Faellt das Geraet mitten im Job
    aus, bleibt der Job `queued` und wird nach dem Reconnect erneut gesendet -
    im ersten Projekt lag die Queue im RAM und war nach jedem Reset weg.
    """

    __tablename__ = "print_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_pk: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(32), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued | sent | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    device: Mapped["Device"] = relationship(back_populates="print_jobs")


class Setting(Base):
    """Kleiner Key/Value-Speicher fuer zur Laufzeit aenderbare Einstellungen
    (Etikettenzaehler, UI-Optionen). Alles Sicherheitsrelevante bleibt in ENV."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
