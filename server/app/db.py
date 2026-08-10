"""Datenbank-Anbindung (async SQLAlchemy)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .config import settings
from .models import Base

log = logging.getLogger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = {"echo": False, "future": True}
if _is_sqlite:
    # SQLite im Container: eine Datei, StaticPool damit sich alle Tasks
    # dieselbe Verbindung teilen und WAL wirksam wird.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    if ":memory:" in settings.database_url:
        _engine_kwargs["poolclass"] = StaticPool
else:
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_recycle"] = 300

engine = create_async_engine(settings.database_url, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Schema anlegen und SQLite auf WAL stellen."""
    async with engine.begin() as conn:
        if _is_sqlite:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)
    log.info("Datenbank bereit (%s)", settings.database_url.split("://", 1)[0])


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-Dependency."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session fuer Hintergrund-Tasks (Geraete-Hub, Scheduler)."""
    async with SessionLocal() as session:
        yield session
