"""per-conversation model + provider scope (workbench B2)."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("conversations")}
    if "model_tier" not in cols:
        op.add_column("conversations", sa.Column("model_tier", sa.String(64), nullable=True))
    if "mcp_providers" not in cols:
        op.add_column("conversations", sa.Column("mcp_providers", sa.JSON(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("conversations")}
    if "mcp_providers" in cols:
        op.drop_column("conversations", "mcp_providers")
    if "model_tier" in cols:
        op.drop_column("conversations", "model_tier")
