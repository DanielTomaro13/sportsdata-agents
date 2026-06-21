"""Unique change-point on prices(provider,book,event,market,selection,changed_at).

The Price PK is (random id, changed_at), so nothing stops a re-run or a same-timestamp
concurrent writer from appending a SECOND row for the identical change-point, inflating
line-move / steam counts. This dedups any existing dups then adds a unique index so the
ingest path's ON CONFLICT DO NOTHING makes re-runs idempotent.

NOTE for a Timescale/postgres warehouse: the index includes the partition column
(changed_at), which Timescale requires for unique indexes on hypertables. The dedup DELETE
is portable (sqlite + postgres). Validate against your warehouse before upgrading; downgrade
just drops the index."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_prices_change"
_COLS = ["provider", "book", "event_external_id", "market", "selection", "changed_at"]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _INDEX in {ix["name"] for ix in insp.get_indexes("prices")}:
        return
    # Drop duplicate change-points, keeping the lowest id per logical key.
    cols = ", ".join(_COLS)
    op.execute(
        f"DELETE FROM prices WHERE id NOT IN "
        f"(SELECT MIN(id) FROM prices GROUP BY {cols})"
    )
    op.create_index(_INDEX, "prices", _COLS, unique=True)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if _INDEX in {ix["name"] for ix in insp.get_indexes("prices")}:
        op.drop_index(_INDEX, table_name="prices")
