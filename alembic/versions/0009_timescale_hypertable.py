"""Timescale hypertable + retention for odds_snapshots (P3 warehouse move).

Guarded three ways: no-op on SQLite, no-op on Postgres without the timescaledb
extension, idempotent when re-run (if_not_exists). The composite PK including
``captured_at`` was designed for exactly this partitioning back in M2.1.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETENTION = "90 days"  # raw snapshots; the change-point series in `prices` is kept


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    has_extension = bind.execute(
        sa.text("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
    ).scalar()
    if not has_extension:
        return  # plain Postgres works fine — Timescale is an optimisation, not a need
    bind.execute(sa.text(
        "SELECT create_hypertable('odds_snapshots', 'captured_at', "
        "if_not_exists => TRUE, migrate_data => TRUE)"
    ))
    bind.execute(sa.text(
        f"SELECT add_retention_policy('odds_snapshots', INTERVAL '{RETENTION}', "
        "if_not_exists => TRUE)"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text(
        "SELECT remove_retention_policy('odds_snapshots', if_exists => TRUE)"
    ))
    # hypertables don't cleanly revert to plain tables in place; leave as-is
