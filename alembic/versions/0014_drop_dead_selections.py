"""Drop the dead `selections` table. It was never written (only fixture→event of the
fixture→event→selection hierarchy is populated; selections live denormalized as strings on
odds_snapshots/prices). Removing it so the schema matches the running system. Idempotent."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("selections"):
        op.drop_table("selections")


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("selections"):
        op.create_table(
            "selections",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=True),
            sa.Column("market", sa.String(128), index=True),
            sa.Column("name", sa.String(400)),
            sa.Column("meta", sa.JSON(), nullable=True),
        )
