"""Tenant-scoped repository.

The single chokepoint that enforces isolation (§12/§13): **every** query is filtered by
``tenant_id`` + ``workspace_id``, and every insert stamps them. A repository bound to one
workspace can never read or write another's rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from .base import TenantScopedModel


@dataclass(frozen=True)
class TenantScope:
    tenant_id: str
    workspace_id: str


class Repository[T: TenantScopedModel]:
    """CRUD scoped to a single (tenant, workspace). Construct one per model per request."""

    def __init__(self, model: type[T], session: AsyncSession, scope: TenantScope) -> None:
        self.model = model
        self.session = session
        self.scope = scope

    def _scoped(self, stmt: Select[tuple[T]]) -> Select[tuple[T]]:
        return stmt.where(
            self.model.tenant_id == self.scope.tenant_id,
            self.model.workspace_id == self.scope.workspace_id,
        )

    async def add(self, **values: Any) -> T:
        obj = self.model(
            tenant_id=self.scope.tenant_id,
            workspace_id=self.scope.workspace_id,
            **values,
        )
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def get(self, id_: uuid.UUID) -> T | None:
        stmt = self._scoped(select(self.model).where(self.model.id == id_))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self) -> Sequence[T]:
        return (await self.session.execute(self._scoped(select(self.model)))).scalars().all()

    async def count(self) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.tenant_id == self.scope.tenant_id,
                self.model.workspace_id == self.scope.workspace_id,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())
