"""performance table (M1.4 — the §9 table M0.3 deferred here)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-11

Inspector-guarded: fresh databases get the table from 0001's live metadata.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("performance"):
        return
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.models import Performance

    Performance.__table__.create(op.get_bind())
    _ = Base  # imported for metadata side effects


def downgrade() -> None:
    if _has_table("performance"):
        op.drop_table("performance")
