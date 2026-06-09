"""Alembic environment — async, URL from Settings (so it follows the env var)."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from sportsdata_agents.config import get_settings
from sportsdata_agents.data import models  # noqa: F401  (import to register all tables on the metadata)
from sportsdata_agents.data.base import Base
from sportsdata_agents.data.db import make_engine

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silently mute every
    # already-created application logger when migrations run in-process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite-friendly ALTERs for future migrations
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = make_engine(get_settings().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():  # pragma: no cover - not used (we always run online)
    raise SystemExit("offline migrations are not supported; use a database URL")
run_migrations_online()
