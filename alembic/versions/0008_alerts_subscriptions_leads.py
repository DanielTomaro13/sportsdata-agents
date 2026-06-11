"""subscriptions + alerts (M3.2 line monitor) and leads (M3.4 site capture)."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.models import Alert, Lead, Subscription

    _ = Base
    for model in (Subscription, Alert, Lead):  # order: alerts FK subscriptions
        if not insp.has_table(model.__table__.name):
            model.__table__.create(bind)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for table in ("alerts", "subscriptions", "leads"):
        if insp.has_table(table):
            op.drop_table(table)
