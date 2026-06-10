"""models + predictions (M2.2 — calibrated models and what they predicted)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-10

Inspector-guarded like 0003-0005 (fresh databases get both from 0001's metadata).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.models import ModelArtifact, Prediction

    _ = Base
    for model in (ModelArtifact, Prediction):  # order matters: predictions FKs models
        if not insp.has_table(model.__table__.name):
            model.__table__.create(bind)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for table in ("predictions", "models"):
        if insp.has_table(table):
            op.drop_table(table)
