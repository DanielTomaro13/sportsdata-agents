"""run transcripts + agent_runs.input_task (M4.5 workbench observability)."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "input_task" not in {c["name"] for c in insp.get_columns("agent_runs")}:
        op.add_column("agent_runs", sa.Column("input_task", sa.Text(), nullable=True))
    from sportsdata_agents.data.base import Base
    from sportsdata_agents.data.models import RunTranscript

    _ = Base
    if not insp.has_table(RunTranscript.__table__.name):
        RunTranscript.__table__.create(bind)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("run_transcripts"):
        op.drop_table("run_transcripts")
    if "input_task" in {c["name"] for c in insp.get_columns("agent_runs")}:
        op.drop_column("agent_runs", "input_task")
