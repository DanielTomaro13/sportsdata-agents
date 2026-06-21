"""Async database engine + session helpers (SQLAlchemy 2.0).

The app uses one lazily-created engine from ``Settings.database_url`` (async driver:
asyncpg for Postgres, aiosqlite for tests). Tests build their own engine/session, so the
module global stays out of their way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import Connection
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


def _add_missing_columns(conn: Connection) -> None:
    """Additively reconcile EXISTING SQLite tables with the ORM models: for every
    mapped column a table is missing, ``ALTER TABLE … ADD COLUMN``. SQLite's
    ``create_all`` only adds whole tables, so without this a model that grew a column
    (e.g. ``conversations.archived``) would break queries against an older warehouse.
    Only additive, only nullable or constant-default'd columns — never drops, retypes,
    or touches keys. Each ALTER is independent; one failure can't block the rest."""
    import logging

    from sqlalchemy import inspect

    from sportsdata_agents.data.base import Base

    log = logging.getLogger(__name__)
    insp = inspect(conn)
    existing = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue  # create_all just made it complete
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(dialect=conn.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            sd = getattr(col.server_default, "arg", None)
            if sd is not None:  # a constant default lets SQLite back-fill existing rows
                default_sql = str(getattr(sd, "text", sd))
                ddl += (" NOT NULL" if not col.nullable else "") + f" DEFAULT {default_sql}"
            try:
                conn.exec_driver_sql(ddl)
                log.info("warehouse: added missing column %s.%s", table.name, col.name)
            except Exception as e:  # a partial/odd column must not abort startup
                log.warning("warehouse: could not add %s.%s (%s)", table.name, col.name, e)


def _create_missing_indexes(conn: Connection) -> None:
    """Create any model-defined Index missing from an EXISTING SQLite table. ``create_all``
    only builds indexes for tables it creates *fresh*, so a warehouse that predates an index
    (e.g. ``uq_prices_change``, migration 0013) never gets it — which silently degrades the
    ingest's ON CONFLICT idempotency on the desktop. For a UNIQUE index that fails on existing
    duplicate rows, dedup (keep the lowest id) then retry — mirrors the alembic migration.
    Each attempt is isolated in a SAVEPOINT so one failure can't poison the rest."""
    import logging

    from sqlalchemy import inspect
    from sqlalchemy.exc import IntegrityError, OperationalError

    from sportsdata_agents.data.base import Base

    log = logging.getLogger(__name__)
    insp = inspect(conn)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {ix["name"] for ix in insp.get_indexes(table.name)}
        cols = {c["name"] for c in insp.get_columns(table.name)}
        for index in table.indexes:
            if index.name in have:
                continue
            idx_cols = [c.name for c in index.columns]
            if not all(c in cols for c in idx_cols):
                continue  # the columns themselves are missing — _add_missing_columns handles that
            sp = conn.begin_nested()
            try:
                index.create(conn)
                sp.commit()
                log.info("warehouse: created missing index %s on %s", index.name, table.name)
            except (IntegrityError, OperationalError):
                sp.rollback()
                if not (index.unique and "id" in cols):
                    log.warning("warehouse: could not create index %s on %s", index.name, table.name)
                    continue
                grp = ", ".join(f'"{c}"' for c in idx_cols)
                sp2 = conn.begin_nested()
                try:
                    conn.exec_driver_sql(
                        f'DELETE FROM "{table.name}" WHERE id NOT IN '
                        f'(SELECT MIN(id) FROM "{table.name}" GROUP BY {grp})'
                    )
                    index.create(conn)
                    sp2.commit()
                    log.info("warehouse: deduped + created unique index %s on %s", index.name, table.name)
                except Exception as e:
                    sp2.rollback()
                    log.warning("warehouse: could not create unique index %s (%s)", index.name, e)


async def ensure_schema(engine: AsyncEngine | None = None) -> None:
    """Bring the desktop's self-contained SQLite warehouse up to the current ORM schema at
    launch (no runtime alembic). ``create_all`` adds missing TABLES; ``_add_missing_columns``
    adds missing COLUMNS to existing tables; ``_create_missing_indexes`` adds missing INDEXES
    (incl. unique ones the Postgres path gets via alembic). Gated to SQLite so the
    Postgres/server path stays alembic-managed and untouched."""
    engine = engine or get_engine()
    if not str(engine.url).startswith("sqlite"):
        return  # server/Postgres schema is alembic's job, not create_all's
    from sportsdata_agents.data import models  # noqa: F401 — registers tables on Base.metadata
    from sportsdata_agents.data.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
    # Indexes in their OWN transaction: a dedup/constraint hiccup can't undo the schema above.
    async with engine.begin() as conn:
        await conn.run_sync(_create_missing_indexes)


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
