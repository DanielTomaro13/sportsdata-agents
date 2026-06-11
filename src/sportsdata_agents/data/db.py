"""Async database engine + session helpers (SQLAlchemy 2.0).

The app uses one lazily-created engine from ``Settings.database_url`` (async driver:
asyncpg for Postgres, aiosqlite for tests). Tests build their own engine/session, so the
module global stays out of their way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sportsdata_agents.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str) -> AsyncEngine:
    """Create an async engine for ``url`` (driver imported lazily on first connect).

    SQLite gets a busy timeout: the cron'd ingest writes every few minutes, and a
    single-writer database must make concurrent readers WAIT, not fail with
    'database is locked' (interim until the P3 Postgres move)."""
    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_engine() -> AsyncEngine:
    """The cached application engine (built from settings on first use)."""
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = make_sessionmaker(get_engine())
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session: commit on success, rollback on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def reset_engine() -> None:
    """Dispose + clear the cached engine (mainly for tests / config reloads)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
