"""OpenFoodFacts-Abfrage mit Datenbank-Cache.

Das ESP32 macht keine TLS-Verbindungen mehr. Genau diese Aufrufe waren im
ersten Projekt die haeufigste Ursache fuer scheinbares Einfrieren der UI: ein
HTTPS-Handshake im falschen Task blockiert die Loop mehrere Sekunden.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Category, Product, utcnow
from . import categories as cat

log = logging.getLogger(__name__)

_FIELDS = ("code,product_name,product_name_de,brands,quantity,categories_tags,nutriscore_grade,"
           "image_front_small_url,storage_conditions_tags,conservation_conditions_de,"
           "conservation_conditions")
_inflight: dict[str, asyncio.Task] = {}


# storage_conditions_tags kennt genau drei Werte (Taxonomie
# storage_conditions.txt) - sie passen auf unsere Lagerarten.
_LAGER_TAGS = {"en:frozen": "tiefkuehl", "en:refrigerated": "kuehl", "en:ambient": "raum"}
# Was nach dem Oeffnen gilt, sagt nichts darueber, wie es verkauft wird.
_NACH_OEFFNEN = re.compile(r"nach (dem )?(öffnen|oeffnen|anbruch)|geöffnet|geoeffnet|after opening|once opened")
_GEFROREN = re.compile(r"[-−–]\s*1[58]\s*°|tiefgekühlt|tiefgekuehlt|tiefgefroren|nicht wieder einfrieren"
                       r"|aufgetaut|frozen|keep frozen")
_GEKUEHLT = re.compile(r"gekühlt|gekuehlt|kühlschrank|kuehlschrank|\+?\s*[2-8]\s*°\s*c|refrigerat")


def lagerart(product: dict) -> str:
    """raum/kuehl/tiefkuehl aus einem OpenFoodFacts-Datensatz, "" = unbekannt.

    Zuerst das eigene Feld, dann die Kategorie "frozen-foods", zuletzt der
    Text auf der Packung ("bei -18 °C lagern", "gekühlt lagern"). Dort zaehlt
    nur, was vor "nach dem Öffnen" steht.
    """
    for tag in product.get("storage_conditions_tags") or []:
        if tag in _LAGER_TAGS:
            return _LAGER_TAGS[tag]
    if any(t == "en:frozen-foods" or t.startswith("en:frozen-")
           for t in product.get("categories_tags") or []):
        return "tiefkuehl"
    text = (product.get("conservation_conditions_de")
            or product.get("conservation_conditions") or "").lower()
    text = _NACH_OEFFNEN.split(text, maxsplit=1)[0]
    if _GEFROREN.search(text):
        return "tiefkuehl"
    if _GEKUEHLT.search(text):
        return "kuehl"
    return ""


async def _fetch_remote(barcode: str, available: list[str]) -> dict | None:
    url = f"{settings.openfoodfacts_url}/{barcode}.json?fields={_FIELDS}"
    headers = {"User-Agent": "Lebensmittel-Scanner/2.0 (self-hosted)"}
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("OpenFoodFacts-Abfrage fehlgeschlagen (%s): %s", barcode, exc)
        return None

    if data.get("status") != 1 and "product" not in data:
        return None
    product = data.get("product") or {}
    if not product:
        return None

    tags = product.get("categories_tags") or []
    # Nicht die Rohkategorie uebernehmen: OpenFoodFacts liefert Dinge wie
    # "Beverages And Beverages Preparations". Unsortiert im Bestand macht das
    # jede Auswertung nach Kategorie wertlos.
    matched = cat.match_off(tags, available)
    return {
        "name": (product.get("product_name_de") or product.get("product_name") or "").strip(),
        "brand": (product.get("brands") or "").split(",")[0].strip(),
        "amount": (product.get("quantity") or "").strip(),
        "category": matched,
        "subcategory": cat.match_subcategory(matched, tags),
        "nutriscore": (product.get("nutriscore_grade") or "").upper()[:1],
        "image_url": product.get("image_front_small_url") or "",
        "lagerart": lagerart(product),
    }


async def lookup(session: AsyncSession, barcode: str, refresh: bool = False) -> Product | None:
    """Produkt aus dem Cache holen, sonst online nachschlagen.

    Gibt auch dann einen Datensatz zurueck, wenn OpenFoodFacts nichts kennt -
    dann als leerer Platzhalter mit `source="unknown"`, damit die Oberflaeche
    "unbekanntes Produkt" von "noch nie gescannt" unterscheiden kann.
    """
    barcode = barcode.strip()
    if not barcode:
        return None

    cached = (
        await session.execute(select(Product).where(Product.barcode == barcode))
    ).scalar_one_or_none()
    if cached is not None and not refresh:
        if cached.source != "unknown" or cached.fetched_at is not None:
            return cached

    if not settings.openfoodfacts_enabled:
        return cached

    # Mehrfache gleichzeitige Scans desselben Codes teilen sich eine Abfrage.
    task = _inflight.get(barcode)
    if task is None:
        # Die vorhandenen Kategorien vor dem Abruf holen: waehrend der Abfrage
        # laeuft, darf die Sitzung nicht mitten in einer Abfrage stecken.
        available = list(
            (await session.execute(select(Category.name))).scalars().all()
        )
        task = asyncio.create_task(_fetch_remote(barcode, available))
        _inflight[barcode] = task
    try:
        remote = await task
    finally:
        _inflight.pop(barcode, None)

    if cached is None:
        cached = Product(barcode=barcode)
        session.add(cached)

    if remote:
        cached.name = remote["name"] or cached.name
        cached.brand = remote["brand"] or cached.brand
        cached.amount = remote["amount"] or cached.amount
        cached.category = remote["category"] or cached.category
        cached.subcategory = remote["subcategory"] or cached.subcategory
        cached.nutriscore = remote["nutriscore"] or cached.nutriscore
        cached.image_url = remote["image_url"] or cached.image_url
        cached.lagerart = remote["lagerart"] or cached.lagerart
        cached.source = "off"
    elif not cached.name:
        cached.source = "unknown"

    cached.fetched_at = utcnow()
    await session.flush()
    return cached
