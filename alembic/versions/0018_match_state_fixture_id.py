"""match_state.fixture_id — the join the in-play arb watch was missing.

`match_state` rows carry the provider's own event id; `scan_arbs` results carry the
warehouse fixture UUID and nothing else. The watch compared one against a set of the
other, so once capture started writing real rows it would still never have matched —
the second way that watch shipped dead, one layer deeper than the min_lead bug.
Storing the fixture id at capture time (the fetcher starts FROM fixtures, so it always
knows it) lets `live_event_ids` answer in both dialects.

Conditional like 0017, and for the same reason: 0001 runs create_all from current
model metadata, so a fresh database already has the column when the chain gets here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "fixture_id" in {c["name"] for c in inspector.get_columns("match_state")}:
        return
    # No FK on purpose: SQLite cannot ADD COLUMN with a constraint, and a batch-mode
    # table rebuild buys referential niceness at the cost of rewriting a table that
    # Timescale may own as a hypertable.
    op.add_column("match_state", sa.Column("fixture_id", sa.Uuid(), nullable=True))
    op.create_index("ix_match_state_fixture_id", "match_state", ["fixture_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "fixture_id" not in {c["name"] for c in inspector.get_columns("match_state")}:
        return
    op.drop_index("ix_match_state_fixture_id", table_name="match_state")
    op.drop_column("match_state", "fixture_id")
