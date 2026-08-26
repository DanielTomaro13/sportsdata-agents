"""Live match state — the column the in-play work was missing.

`prices` records every change-point but says nothing about the match: no way to tell
a live market from a pre-game one, and no scoreline stored anywhere. Three things
wanted that and none could have it — an in-play arbitrage watch (which must know an
event is running before it trusts a cross-book sum), the engines' fair cash-out (which
otherwise prices the pre-game position), and any honest answer to "what is happening
right now".

Change-point shaped like `prices`: a score that has not moved is not news, and a row
per poll would grow without bound saying nothing. Nothing writes to this table until
in-play capture is switched on, so the migration is safe to land ahead of it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0001 runs `Base.metadata.create_all` from the CURRENT model metadata, so a FRESH
    # database already has this table by the time the chain reaches here, while a
    # database created before this model existed does not. Both are real, so the create
    # is conditional rather than unconditional — without this, `alembic upgrade head`
    # on a clean DB dies with "table match_state already exists".
    if sa.inspect(op.get_bind()).has_table("match_state"):
        return
    op.create_table(
        "match_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("sport", sa.String(length=32), nullable=False),
        sa.Column("event_external_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("clock", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id", "captured_at"),
    )
    op.create_index("ix_match_state_captured_at", "match_state", ["captured_at"])
    op.create_index("ix_match_state_event_external_id", "match_state", ["event_external_id"])
    op.create_index("ix_match_state_status", "match_state", ["status"])
    op.create_index("ix_match_state_event", "match_state", ["event_external_id", "captured_at"])
    # Re-polling an unchanged match must be a no-op, not a duplicate row.
    op.create_index(
        "uq_match_state_change", "match_state",
        ["provider", "event_external_id", "captured_at"], unique=True,
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("match_state"):
        return
    op.drop_index("uq_match_state_change", table_name="match_state")
    op.drop_index("ix_match_state_event", table_name="match_state")
    op.drop_index("ix_match_state_status", table_name="match_state")
    op.drop_index("ix_match_state_event_external_id", table_name="match_state")
    op.drop_index("ix_match_state_captured_at", table_name="match_state")
    op.drop_table("match_state")
