"""conversation archive + title (M4.5 workbench chat management)."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("conversations")}
    if "title" not in cols:
        op.add_column("conversations", sa.Column("title", sa.String(200), nullable=True))
    if "archived" not in cols:
        op.add_column(
            "conversations",
            sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("conversations")}
    for name in ("archived", "title"):
        if name in cols:
            op.drop_column("conversations", name)
