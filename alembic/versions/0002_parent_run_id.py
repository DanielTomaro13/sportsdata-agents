"""agent_runs.parent_run_id (delegation audit tree, §16)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-11

Inspector-guarded: 0001 builds the schema from live metadata, so a FRESH database
already has this column when 0001 runs — only databases created before this revision
need the ALTER.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("agent_runs", "parent_run_id"):
        with op.batch_alter_table("agent_runs") as batch:
            batch.add_column(sa.Column("parent_run_id", sa.Uuid(), nullable=True))
            batch.create_index("ix_agent_runs_parent_run_id", ["parent_run_id"])


def downgrade() -> None:
    if _has_column("agent_runs", "parent_run_id"):
        with op.batch_alter_table("agent_runs") as batch:
            batch.drop_index("ix_agent_runs_parent_run_id")
            batch.drop_column("parent_run_id")
