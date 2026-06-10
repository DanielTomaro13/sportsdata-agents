"""memory: unique (tenant_id, workspace_id, key) — remember() upsert integrity

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-10

Inspector-guarded (fresh databases get the constraint from the model's metadata).
Existing duplicates are collapsed to the newest row before the index lands.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_memory_tenant_workspace_key"


def _existing_names() -> set[str]:
    insp = sa.inspect(op.get_bind())
    names = {ix["name"] for ix in insp.get_indexes("memory")}
    names |= {uc["name"] for uc in insp.get_unique_constraints("memory")}
    return {n for n in names if n}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("memory") or INDEX in _existing_names():
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, tenant_id, workspace_id, key FROM memory ORDER BY created_at")
    ).fetchall()
    keep: dict[tuple[str, str, str], str] = {}
    for row in rows:  # last (newest) row per key wins
        keep[(row.tenant_id, row.workspace_id, row.key)] = str(row.id)
    doomed = [str(r.id) for r in rows if str(r.id) != keep[(r.tenant_id, r.workspace_id, r.key)]]
    for rid in doomed:
        bind.execute(sa.text("DELETE FROM memory WHERE id = :id"), {"id": rid})
    op.create_index(INDEX, "memory", ["tenant_id", "workspace_id", "key"], unique=True)


def downgrade() -> None:
    if INDEX in _existing_names():
        op.drop_index(INDEX, table_name="memory")
