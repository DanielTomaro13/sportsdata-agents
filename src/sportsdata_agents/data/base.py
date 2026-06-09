"""Declarative base + shared mixins.

Types are kept **cross-dialect** (generic ``JSON``/``Uuid``/``Numeric``) so the same models
run on Postgres (prod) and SQLite (tests). Timescale-specific DDL (hypertables for
``odds_snapshots``/``prices``) is deferred to M2.1 and lives in its own migration.

Every *customer/operational* row is tenant-scoped (``TenantScopedModel``); the only
deliberate exception is public reference data (fixtures/events/selections), which is
global because it's the same for all tenants (§9).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Root declarative base for all ORM models."""


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TenantScopedModel(Base):
    """Abstract base for every tenant-owned table: UUID pk + tenant/workspace scope + created_at.

    The presence of ``tenant_id``/``workspace_id`` here is what the repository layer keys on to
    guarantee isolation (§12/§13).
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
