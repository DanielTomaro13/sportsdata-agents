"""Resolution-backed session tools (B4): the fixtures/events join, exposed to agents.

`agents resolve` maps every book's event ids onto shared fixtures; these tools let a
value scout (or any session) USE that join — find the fixture, then rank every
mapped book's latest price per selection, best first. No LLM in the lookup path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.data.models import Event, Fixture
from sportsdata_agents.operations.resolution.resolver import cross_book_prices

RESOLUTION_TOOL_NAMES = {"find_fixture", "best_prices"}


def resolution_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[ToolDef]:
    async def find_fixture(args: dict[str, Any]) -> Any:
        """{query, sport?, limit?} → fixtures whose name matches (case-insensitive
        substring; tokens may appear in any order), with how many books map to each."""
        query = str(args.get("query", "")).strip().lower()
        if not query:
            raise ValueError("query is required, e.g. 'bulldogs adelaide'")
        sport = str(args.get("sport", "")).strip().lower()
        limit = min(int(args.get("limit", 10)), 25)
        async with session_factory() as session:
            stmt = select(Fixture, func.count(Event.id).label("books")).join(
                Event, Event.fixture_id == Fixture.id, isouter=True
            )
            for token in query.split():
                stmt = stmt.where(func.lower(Fixture.name).like(f"%{token}%"))
            if sport:
                stmt = stmt.where(Fixture.sport == sport)
            stmt = stmt.group_by(Fixture.id).order_by(
                func.count(Event.id).desc(), Fixture.start_time.desc()
            ).limit(limit)
            rows = (await session.execute(stmt)).all()
        return {
            "fixtures": [
                {
                    "fixture_id": str(fx.id),
                    "name": fx.name,
                    "sport": fx.sport,
                    "start_time": fx.start_time.isoformat() if fx.start_time else None,
                    "books": int(books),
                }
                for fx, books in rows
            ],
            "note": "feed a fixture_id to best_prices for the cross-book board",
        }

    async def best_prices(args: dict[str, Any]) -> Any:
        """{fixture_id, market?} → every mapped book's latest price per selection,
        best odds first — the cross-book board event resolution exists to power."""
        return await cross_book_prices(
            session_factory,
            fixture_id=str(args["fixture_id"]),
            market=str(args.get("market", "h2h")),
        )

    def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> ToolDef:
        return ToolDef(
            name=name,
            description=(fn.__doc__ or name).strip().splitlines()[0],
            parameters={"type": "object", "properties": props, "required": required},
            execute=fn,
        )

    return [
        _tool("find_fixture", find_fixture,
              {"query": {"type": "string"}, "sport": {"type": "string"},
               "limit": {"type": "integer"}}, ["query"]),
        _tool("best_prices", best_prices,
              {"fixture_id": {"type": "string"}, "market": {"type": "string"}},
              ["fixture_id"]),
    ]
