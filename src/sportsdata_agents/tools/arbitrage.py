"""Arbitrage session tool: the deterministic cross-book scan, exposed to agents.

The math lives in quant.arbitrage (orientation-translated, per-book-complete
outcome frames, same-line totals, exchange NO-folds); the agent's job is to
present findings with the verification caveats intact.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.quant.arbitrage import DEFAULT_MARKETS, find_arbs

ARBITRAGE_TOOL_NAMES = {"find_arbs"}


def arbitrage_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[ToolDef]:
    async def _find_arbs(args: dict[str, Any]) -> Any:
        """{threshold_pct?, hours?, markets?, limit?} → cross-book arbitrage
        opportunities: complete outcome boards whose best prices sum under 1,
        with per-leg books, odds and the equalised stake split. Margins are
        GROSS (fees/limits/timing not priced in)."""
        markets = args.get("markets") or list(DEFAULT_MARKETS)
        return await find_arbs(
            session_factory,
            hours=float(args.get("hours", 6.0)),
            threshold_pct=float(args.get("threshold_pct", 1.0)),
            markets=tuple(str(m) for m in markets),
            limit=min(int(args.get("limit", 20)), 50),
        )

    return [
        ToolDef(
            name="find_arbs",
            description=(_find_arbs.__doc__ or "find_arbs").strip().splitlines()[0],
            parameters={
                "type": "object",
                "properties": {
                    "threshold_pct": {"type": "number",
                                      "description": "Minimum gross margin %, default 1.0."},
                    "hours": {"type": "number",
                              "description": "Price freshness window in hours, default 6."},
                    "markets": {"type": "array", "items": {"type": "string"},
                                "description": "Market families to scan, default h2h+total."},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            execute=_find_arbs,
        )
    ]
