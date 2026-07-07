"""Unique event mapping on events(provider, external_id).

An event belongs to ONE fixture, but nothing enforced it: two resolve passes
racing (the scheduler's tick beside a manual `agents resolve`) both read the
mapped-keys snapshot, both inserted, and 86 duplicate rows landed in one
evening — same fixture on every pair, so no data was wrong, but every
sibling scan double-counted. Dedup keeps one row per key (they agree by
construction; ties broken arbitrarily), then the unique index makes the
race an IntegrityError the next resolve pass heals instead of a silent
duplicate."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_events_provider_external"


def upgrade() -> None:
    # portable dedup (sqlite has no aliased DELETE): keep the smallest id
    # per key — duplicate rows agree on fixture by construction
    op.execute(
        """
        DELETE FROM events
        WHERE id NOT IN (
            SELECT MIN(id) FROM events GROUP BY provider, external_id
        )
        """
    )
    op.create_index(_INDEX, "events", ["provider", "external_id"], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="events")
