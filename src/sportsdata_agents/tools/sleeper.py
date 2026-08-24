"""Making Sleeper's player ids readable.

Every Sleeper tool speaks in opaque player ids — a roster is `["4046", "6794"]`, the
waiver-wire signal is `[{player_id: "13602", count: 148925}]` — and the only thing that
resolves them is a 14.6 MB file of 12,221 players. Projected to the four fields that
answer "who is this?" it is still 1.1 MB, which is an order of magnitude past any sane
context budget.

So the bulk file NEVER reaches the model. It is fetched at most once a day, cached slim
on disk, and the agent asks for the handful of ids it actually cares about. That is also
exactly what Sleeper asks callers to do.

WHY A LOCAL CACHE RATHER THAN A NARROWER TOOL: Sleeper's API has no filter. There is no
"give me these five players" endpoint — the whole table or nothing. A cache is the only
shape that fits, and it is cheap: one request a day, ~1 MB on disk.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sportsdata_agents.agents.harness import ToolDef

logger = logging.getLogger(__name__)

#: Sleeper asks for at most one call a day, and the table changes slowly — a signing
#: shows up within the day, which is the resolution the data has anyway.
CACHE_TTL_SECONDS = 24 * 3600

#: The bulk file is ~1.1 MB after projection, so this session gets its own ceiling. The
#: default 150 KB cap exists to protect a model's context, and nothing here goes near a
#: model's context — the payload is written to disk and the agent sees only the ids it
#: asked about.
FETCH_MAX_BYTES = 8_000_000


def cache_path(sport: str = "nfl"):
    from sportsdata_agents.paths import data_dir

    return data_dir() / f"sleeper-players-{sport}.json"


def _load_cache(sport: str) -> tuple[dict, float]:
    """(players, age_seconds). An unreadable cache is treated as absent rather than
    fatal — a corrupt file should cost one refetch, not the whole run."""
    path = cache_path(sport)
    if not path.exists():
        return {}, float("inf")
    try:
        return json.loads(path.read_text()), time.time() - path.stat().st_mtime
    except (OSError, ValueError):
        logger.warning("sleeper player cache unreadable, refetching: %s", path)
        return {}, float("inf")


async def refresh_players(sport: str = "nfl") -> dict:
    """Fetch the table and cache it slim. Returns the players."""
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.mcp.manager import MCPManager
    from sportsdata_agents.paths import data_dir

    async with MCPManager(
        groups=["sleeper.reference"],
        command=get_settings().mcp_command,
        extra_env={"SPORTSDATA_MCP_MAX_BYTES": str(FETCH_MAX_BYTES)},
    ) as manager:
        body = await manager.call_tool("sleeper_players", {"sport": sport})

    if not isinstance(body, dict) or not body:
        raise RuntimeError(
            f"Sleeper returned no player table for {sport}. Ids cannot be resolved to "
            "names, so any answer naming a player would be a guess."
        )
    data_dir().mkdir(parents=True, exist_ok=True)
    cache_path(sport).write_text(json.dumps(body))
    logger.info("cached %d sleeper %s players", len(body), sport)
    return body


async def _players(sport: str, *, force: bool = False) -> dict:
    players, age = _load_cache(sport)
    if force or not players or age > CACHE_TTL_SECONDS:
        return await refresh_players(sport)
    return players


def _describe(row: dict) -> dict:
    """One player, in the shape an answer needs. `team: null` means free agent — which is
    the single most useful fact about a player on a waiver wire, so it is spelled out
    rather than left as a null for the model to interpret."""
    team = row.get("team")
    return {
        "name": row.get("full_name"),
        "team": team or "FA",
        "position": row.get("position"),
        "status": row.get("status"),
        "is_free_agent": not team,
    }


async def sleeper_resolve_players(args: dict[str, Any]) -> Any:
    """{ids: [...], sport?} → {id: {name, team, position, status}} for those ids only."""
    ids = args.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids must be a non-empty list of Sleeper player ids")
    sport = str(args.get("sport") or "nfl")
    players = await _players(sport)

    out, unknown = {}, []
    for raw in ids:
        pid = str(raw)
        row = players.get(pid)
        if isinstance(row, dict):
            out[pid] = _describe(row)
        else:
            unknown.append(pid)
    result: dict[str, Any] = {"players": out, "resolved": len(out)}
    if unknown:
        # Named rather than silently dropped: an unresolvable id usually means a stale
        # cache or a defence/team id, and a model that does not know an id went missing
        # will quietly omit a player from a lineup.
        result["unresolved"] = unknown
        result["note"] = (
            "These ids are not in the cached table. They may be new signings (the cache "
            "refreshes daily) or team defences, which Sleeper keys by team abbreviation."
        )
    return result


async def sleeper_find_players(args: dict[str, Any]) -> Any:
    """{query, sport?, limit?} → ids for players whose name matches. The reverse lookup."""
    query = str(args.get("query") or "").strip().lower()
    if not query:
        raise ValueError("query must be a non-empty name fragment")
    sport = str(args.get("sport") or "nfl")
    limit = int(args.get("limit") or 10)
    players = await _players(sport)

    hits = []
    for pid, row in players.items():
        if not isinstance(row, dict):
            continue
        name = (row.get("full_name") or "").lower()
        if query in name:
            hits.append({"player_id": pid, **_describe(row)})
    # Rostered players first: a search for "Johnson" across 12,221 rows is mostly retired
    # and practice-squad names, and the one you meant is almost always on a team.
    hits.sort(key=lambda h: (h["is_free_agent"], h["name"] or ""))
    return {"matches": hits[:limit], "total_matches": len(hits)}


SLEEPER_TOOLS: dict[str, ToolDef] = {
    "sleeper_resolve_players": ToolDef(
        name="sleeper_resolve_players",
        description=(
            "Turn Sleeper player ids into names. EVERY Sleeper tool returns ids and nothing "
            "else — rosters, matchups, trending players, draft picks — so call this before "
            "naming anyone. Never guess a name from an id. Ask for only the ids you need; "
            "the underlying table is 12,000+ players and is cached locally, not fetched per "
            "call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "description": "Sleeper player ids to resolve.",
                        "items": {"type": "string"}},
                "sport": {"type": "string", "description": "Default nfl."},
            },
            "required": ["ids"],
        },
        execute=sleeper_resolve_players,
    ),
    "sleeper_find_players": ToolDef(
        name="sleeper_find_players",
        description=(
            "Find Sleeper player ids by name — the reverse of sleeper_resolve_players, for "
            "when the owner names someone and you need their id. Rostered players are "
            "returned before free agents, because a surname search across 12,000 rows is "
            "mostly retired and practice-squad players."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full or partial name."},
                "sport": {"type": "string", "description": "Default nfl."},
                "limit": {"type": "integer", "description": "Max matches (default 10)."},
            },
            "required": ["query"],
        },
        execute=sleeper_find_players,
    ),
}
