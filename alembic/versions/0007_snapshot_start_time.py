"""odds_snapshots.start_time — the advertised event start, parsed at write time.

The resolver windows fixtures on the event's REAL start day; before this column it
fell back to first-capture day, which breaks futures (captured months before they
run) and any event first seen across a UTC midnight.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("odds_snapshots"):
        return  # fresh DBs get the column from create_all
    columns = {c["name"] for c in insp.get_columns("odds_snapshots")}
    if "start_time" not in columns:
        op.add_column(
            "odds_snapshots",
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("odds_snapshots"):
        columns = {c["name"] for c in insp.get_columns("odds_snapshots")}
        if "start_time" in columns:
            op.drop_column("odds_snapshots", "start_time")
