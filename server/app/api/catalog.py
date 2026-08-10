"""Stammdaten: Kategorien, Lagerorte, Vorlagen, Produkte."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..device.hub import hub
from ..models import Category, Location, Product, Template
from ..schemas import (
    CategoryIn,
    CategoryOut,
    LocationIn,
    LocationOut,
    ProductIn,
    ProductOut,
    TemplateIn,
    TemplateOut,
)
from ..services import openfoodfacts

router = APIRouter(prefix="/api", tags=["stammdaten"])


# ------------------------------------------------------------------ Kategorien
@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(session: AsyncSession = Depends(get_session)):
    return (
        await session.execute(select(Category).order_by(Category.sort_order, Category.name))
    ).scalars().all()


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(body: CategoryIn, session: AsyncSession = Depends(get_session)):
    row = Category(**body.model_dump())
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Kategorie existiert bereits")
    await hub.notify_ui("catalog")
    return row


@router.put("/categories/{cat_id}", response_model=CategoryOut)
async def update_category(
    cat_id: int, body: CategoryIn, session: AsyncSession = Depends(get_session)
):
    row = await session.get(Category, cat_id)
    if row is None:
        raise HTTPException(404, "Kategorie unbekannt")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await hub.notify_ui("catalog")
    return row


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Category, cat_id)
    if row is None:
        raise HTTPException(404, "Kategorie unbekannt")
    await session.delete(row)
    await session.commit()
    await hub.notify_ui("catalog")
    return {"ok": True}


# ------------------------------------------------------------------- Lagerorte
@router.get("/locations", response_model=list[LocationOut])
async def list_locations(session: AsyncSession = Depends(get_session)):
    return (
        await session.execute(select(Location).order_by(Location.sort_order, Location.name))
    ).scalars().all()


@router.post("/locations", response_model=LocationOut, status_code=201)
async def create_location(body: LocationIn, session: AsyncSession = Depends(get_session)):
    row = Location(**body.model_dump())
    session.add(row)
    try:
        await session.flush()
        if row.is_default:
            await _clear_other_defaults(session, row.id)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Lagerort existiert bereits")
    await hub.notify_ui("catalog")
    return row


@router.put("/locations/{loc_id}", response_model=LocationOut)
async def update_location(
    loc_id: int, body: LocationIn, session: AsyncSession = Depends(get_session)
):
    row = await session.get(Location, loc_id)
    if row is None:
        raise HTTPException(404, "Lagerort unbekannt")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    if row.is_default:
        await _clear_other_defaults(session, row.id)
    await session.commit()
    await hub.notify_ui("catalog")
    return row


@router.delete("/locations/{loc_id}")
async def delete_location(loc_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Location, loc_id)
    if row is None:
        raise HTTPException(404, "Lagerort unbekannt")
    await session.delete(row)
    await session.commit()
    await hub.notify_ui("catalog")
    return {"ok": True}


async def _clear_other_defaults(session: AsyncSession, keep_id: int) -> None:
    rows = (
        await session.execute(select(Location).where(Location.is_default.is_(True)))
    ).scalars().all()
    for row in rows:
        if row.id != keep_id:
            row.is_default = False


# -------------------------------------------------------------------- Vorlagen
@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    session: AsyncSession = Depends(get_session), category: str = ""
):
    stmt = select(Template).order_by(Template.sort_order, Template.name)
    if category:
        stmt = stmt.where(Template.category == category)
    return (await session.execute(stmt)).scalars().all()


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(body: TemplateIn, session: AsyncSession = Depends(get_session)):
    row = Template(**body.model_dump())
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Vorlage existiert bereits")
    await hub.notify_ui("catalog")
    return row


@router.put("/templates/{tpl_id}", response_model=TemplateOut)
async def update_template(
    tpl_id: int, body: TemplateIn, session: AsyncSession = Depends(get_session)
):
    row = await session.get(Template, tpl_id)
    if row is None:
        raise HTTPException(404, "Vorlage unbekannt")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await hub.notify_ui("catalog")
    return row


@router.delete("/templates/{tpl_id}")
async def delete_template(tpl_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(Template, tpl_id)
    if row is None:
        raise HTTPException(404, "Vorlage unbekannt")
    await session.delete(row)
    await session.commit()
    await hub.notify_ui("catalog")
    return {"ok": True}


@router.post("/templates/{tpl_id}/sorten", response_model=TemplateOut)
async def add_sorte(
    tpl_id: int, sorte: str = Query(min_length=1), session: AsyncSession = Depends(get_session)
):
    row = await session.get(Template, tpl_id)
    if row is None:
        raise HTTPException(404, "Vorlage unbekannt")
    sorten = list(row.sorten or [])
    if sorte not in sorten:
        sorten.append(sorte)
        row.sorten = sorten  # neue Liste zuweisen, damit SQLAlchemy die Aenderung sieht
        row.use_sorten = True
        await session.commit()
    await hub.notify_ui("catalog")
    return row


@router.delete("/templates/{tpl_id}/sorten", response_model=TemplateOut)
async def delete_sorte(
    tpl_id: int, sorte: str = Query(min_length=1), session: AsyncSession = Depends(get_session)
):
    row = await session.get(Template, tpl_id)
    if row is None:
        raise HTTPException(404, "Vorlage unbekannt")
    row.sorten = [s for s in (row.sorten or []) if s != sorte]
    await session.commit()
    await hub.notify_ui("catalog")
    return row


# -------------------------------------------------------------------- Produkte
@router.get("/products", response_model=list[ProductOut])
async def list_products(
    session: AsyncSession = Depends(get_session),
    q: str = "",
    limit: int = Query(200, ge=1, le=2000),
):
    stmt = select(Product).order_by(Product.name)
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(Product.name.ilike(needle) | Product.barcode.ilike(needle))
    return (await session.execute(stmt.limit(limit))).scalars().all()


@router.get("/products/{barcode}", response_model=ProductOut)
async def get_product(
    barcode: str, refresh: bool = False, session: AsyncSession = Depends(get_session)
):
    row = await openfoodfacts.lookup(session, barcode, refresh=refresh)
    await session.commit()
    if row is None:
        raise HTTPException(404, "Produkt unbekannt")
    return row


@router.put("/products/{barcode}", response_model=ProductOut)
async def upsert_product(
    barcode: str, body: ProductIn, session: AsyncSession = Depends(get_session)
):
    """Eigener Produktdatensatz - ueberschreibt den OpenFoodFacts-Cache."""
    row = (
        await session.execute(select(Product).where(Product.barcode == barcode))
    ).scalar_one_or_none()
    if row is None:
        row = Product(barcode=barcode)
        session.add(row)
    data = body.model_dump()
    data.pop("barcode", None)
    for key, value in data.items():
        setattr(row, key, value)
    row.source = "manual"
    await session.commit()
    await hub.notify_ui("catalog")
    return row


@router.delete("/products/{barcode}")
async def delete_product(barcode: str, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(select(Product).where(Product.barcode == barcode))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Produkt unbekannt")
    await session.delete(row)
    await session.commit()
    return {"ok": True}
