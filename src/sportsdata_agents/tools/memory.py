"""Memory tools (M1.5, §8.2): durable user/workspace facts, notes and preferences.

Session-bound like the tracking tools (DB-backed, tenant-scoped). v1 recall is
keyword search over keys + values — pgvector semantic recall (D11) plugs in behind
the same tool signature when needed. Notes survive context resets because they live
in the database, not the window.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.data.models import Memory
from sportsdata_agents.data.repository import TenantScope

MEMORY_TOOL_NAMES = {"remember", "recall"}


def memory_tools(session_factory: async_sessionmaker[AsyncSession], scope: TenantScope) -> list[ToolDef]:
    async def remember(args: dict[str, Any]) -> Any:
        """{key, value, kind?} → upsert a durable fact/preference/note."""
        key = str(args["key"]).strip().lower()
        if not key:
            raise ValueError("key must be non-empty")
        value = {"text": str(args["value"]), "kind": str(args.get("kind", "fact"))}
        async with session_factory() as session:
            existing = (
                await session.execute(
                    select(Memory).where(
                        Memory.tenant_id == scope.tenant_id,
                        Memory.workspace_id == scope.workspace_id,
                        Memory.key == key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.value = value
            else:
                session.add(
                    Memory(
                        tenant_id=scope.tenant_id,
                        workspace_id=scope.workspace_id,
                        scope="workspace",
                        key=key,
                        value=value,
                    )
                )
            await session.commit()
        return {"remembered": key}

    async def recall(args: dict[str, Any]) -> Any:
        """{query} → matching memories (keyword v1; semantic recall lands with D11)."""
        query = str(args["query"]).strip().lower()
        if not query:
            raise ValueError("query must be non-empty")
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(Memory).where(
                            Memory.tenant_id == scope.tenant_id,
                            Memory.workspace_id == scope.workspace_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        # keyword match across key + JSON value, in Python (cross-dialect JSON column)
        hits = [r for r in rows if query in r.key or query in str(r.value).lower()][:20]
        return {
            "query": args["query"],
            "memories": [
                {"key": r.key, **(r.value if isinstance(r.value, dict) else {"text": str(r.value)})} for r in hits
            ],
        }

    return [
        ToolDef(
            name="remember",
            description="Store a durable fact, preference or note for this workspace "
            "(survives sessions and context resets).",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short slug, e.g. 'favourite_team'"},
                    "value": {"type": "string"},
                    "kind": {"type": "string", "enum": ["fact", "preference", "note"]},
                },
                "required": ["key", "value"],
            },
            execute=remember,
        ),
        ToolDef(
            name="recall",
            description="Search stored memories (facts/preferences/notes) by keyword.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            execute=recall,
        ),
    ]
