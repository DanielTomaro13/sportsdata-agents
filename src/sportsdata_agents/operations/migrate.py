"""Warehouse migration (P3): copy every table from the current database to a
target URL — the SQLite → Postgres/Timescale move, as one deterministic command.

Tables copy in FK order (SQLAlchemy's sorted_tables), in batches, idempotently
enough for a fresh target (it refuses a non-empty target by default — a partial
copy on top of existing rows is how data gets silently doubled). The Timescale
hypertable + retention policy land via alembic migration 0009 once the target is
Postgres with the extension installed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Table, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sportsdata_agents.data.base import Base

logger = logging.getLogger(__name__)

BATCH = 5_000


def _alembic_head() -> str | None:
    """The repo's current migration head (best-effort — a packaged install
    without the alembic tree just skips stamping)."""
    try:
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        ini = Path(__file__).resolve().parents[3] / "alembic.ini"
        if not ini.is_file():
            return None
        return ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    except Exception:
        return None


def _idempotent_insert(target: AsyncEngine, table: Table) -> Any:
    """Skip rows whose PK already exists — resuming a partial copy must not
    collide (dialect-specific: OR IGNORE / ON CONFLICT DO NOTHING)."""
    if target.dialect.name == "sqlite":
        return insert(table).prefix_with("OR IGNORE")
    if target.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(table).on_conflict_do_nothing()
    return insert(table)


async def migrate_warehouse(
    source_url: str, target_url: str, *, allow_nonempty: bool = False
) -> dict[str, Any]:
    """Copy schema + every row from source to target. Returns per-table counts."""
    # registering the models on Base.metadata is a SIDE EFFECT of importing them —
    # without this, sorted_tables is empty in a fresh process and the copy is a no-op
    import sportsdata_agents.data.models  # noqa: F401

    source = create_async_engine(source_url)
    target = create_async_engine(target_url)
    report: dict[str, Any] = {}
    try:
        async with target.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # safety: a non-empty target means a previous/partial copy — refuse unless
        # told (with --allow-nonempty the copy RESUMES: existing PKs are skipped)
        if not allow_nonempty:
            async with target.connect() as conn:
                for table in Base.metadata.sorted_tables:
                    existing = (await conn.execute(select(func.count()).select_from(table))).scalar_one()
                    if existing:
                        raise RuntimeError(
                            f"target table {table.name!r} already has {existing} rows — "
                            "use --allow-nonempty to copy anyway (rows may double)"
                        )
        for table in Base.metadata.sorted_tables:  # FK order
            copied = 0
            async with source.connect() as src_conn:
                total = (await src_conn.execute(select(func.count()).select_from(table))).scalar_one()
                offset = 0
                while offset < total:
                    rows = (
                        await src_conn.execute(
                            # PK-ordered: OFFSET pagination is only stable with a
                            # deterministic order (and pause the ingest cron during
                            # the real move — a live writer shifts pages)
                            select(table)
                            .order_by(*table.primary_key.columns)
                            .offset(offset)
                            .limit(BATCH)
                        )
                    ).mappings().all()
                    if not rows:
                        break
                    async with target.begin() as tgt_conn:
                        await tgt_conn.execute(_idempotent_insert(target, table),
                                               [dict(r) for r in rows])
                    copied += len(rows)
                    offset += BATCH
            report[table.name] = copied
            if copied:
                logger.info("migrated %s: %d rows", table.name, copied)
        # stamp the migration head: the schema came from create_all, so alembic
        # must not re-run the chain on the target (the chain is guarded, but a
        # stamp is the clean contract — audit fix)
        head = _alembic_head()
        if head:
            from sqlalchemy import text

            async with target.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)"
                ))
                await conn.execute(text("DELETE FROM alembic_version"))
                await conn.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head}
                )
            report["stamped"] = head
    finally:
        await source.dispose()
        await target.dispose()
    report["total"] = sum(v for v in report.values() if isinstance(v, int) and v != report.get("stamped"))
    return report
