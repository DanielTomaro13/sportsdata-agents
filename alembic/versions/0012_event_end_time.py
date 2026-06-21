"""end_time on fixtures + odds_snapshots — exchange contracts key on resolution/expiry,
not kickoff, so that time is stored as an END (a day-window proxy) and never mistaken for
a real start (the arb in-play gate reads start_time only). Nullable + idempotent → safe."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add(insp: sa.Inspector, table: str) -> None:
    if "end_time" not in {c["name"] for c in insp.get_columns(table)}:
        op.add_column(table, sa.Column("end_time", sa.DateTime(timezone=True), nullable=True))


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    _add(insp, "fixtures")
    _add(insp, "odds_snapshots")


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for table in ("odds_snapshots", "fixtures"):
        if "end_time" in {c["name"] for c in insp.get_columns(table)}:
            op.drop_column(table, "end_time")
