"""odds-history warehouse: odds_snapshots + prices + event_results (M2.1, §9.1)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-10

Inspector-guarded like 0003/0004. TimescaleDB is attempted, not required: when the
extension is available the two time-series tables become hypertables with a 90-day
retention policy on raw snapshots (change-points in `prices` are kept). On plain
Postgres/SQLite they are ordinary tables — a documented deviation, not a failure
(`prune_snapshots` covers retention manually).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

TABLES = ("odds_snapshots", "prices", "event_results")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.models import EventResult, OddsSnapshot, Price

    _ = Base  # imported for metadata side effects
    for model in (OddsSnapshot, Price, EventResult):
        if not insp.has_table(model.__table__.name):
            model.__table__.create(bind)

    if bind.dialect.name != "postgresql":
        return
    try:  # Timescale is optional infrastructure, never a migration failure
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        for table, time_col in (("odds_snapshots", "captured_at"), ("prices", "changed_at")):
            bind.execute(
                sa.text(
                    f"SELECT create_hypertable('{table}', '{time_col}', "
                    f"if_not_exists => TRUE, migrate_data => TRUE)"
                )
            )
        bind.execute(
            sa.text("SELECT add_retention_policy('odds_snapshots', INTERVAL '90 days', if_not_exists => TRUE)")
        )
    except Exception as e:
        logger.info("timescaledb unavailable — plain tables (retention via prune_snapshots): %s", e)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for table in TABLES:
        if insp.has_table(table):
            op.drop_table(table)
