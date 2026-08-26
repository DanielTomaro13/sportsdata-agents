"""Reach the data plane without carrying it (§8.2).

THE PROBLEM. Tool schemas ride EVERY model call. `bridge_mcp_tools` attaches every
tool a granted capability resolves to, and one capability can fan out a long way —
`sport.fixtures_by_date` alone is 74 tools across 45 providers. The generalist carries
260 schemas, ~12,500 tokens, before the question is read. Attaching all 827 would cost
~40,000 tokens per call, and would make tool choice *harder*: picking between 827
near-synonyms is a worse discrimination problem than picking between forty.

So reach is not attachment. An agent declares `mcp_discover` capabilities it may reach
but does not carry, and gets two small tools instead of a hundred large ones:

    find_data_tools(query)          which tools answer this, and how to call them
    call_data_tool(tool, args)      call one of them

The catalogue is searched at turn time rather than serialised into every request.

WHY THE TOOL LIST STAYS FIXED. `Harness._loop` computes `tool_schemas` once, before its
loop, so a toolset cannot grow mid-run — and making it grow would mean surgery on the
path the deny-filter lives on. It is also unnecessary: keep the tool *list* constant and
let the *arguments* vary. `call_data_tool` is one ToolDef whose schema never changes.

The trade is real and worth stating: a discovered tool arrives as prose (name, summary,
required args) rather than a validated JSON schema, so malformed calls are the expected
failure mode. `call_data_tool` answers them with the tool's argument list rather than a
bare error, so the next attempt has what it needs.

SCOPE IS STILL LEAST-PRIVILEGE. Discovery searches only the capabilities the spec
granted, and `call_data_tool` refuses anything outside that resolved set. Without that
it would be a privilege escalation: one tool that reaches the whole catalogue regardless
of what the agent was granted. The no-money deny-filter is inherited rather than
re-implemented — `MCPManager.call_tool` raises `ForbiddenToolError` on a denied name at
call time, so it holds however the name arrives.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.mcp.manager import MCPManager, is_denied

#: Summaries are reference-doc verbose; a search result is a shortlist, not documentation.
SUMMARY_LIMIT = 160

#: Enough to compare providers for one question without pasting the catalogue back in.
DEFAULT_LIMIT = 8
MAX_LIMIT = 25


def _score(query: str, tool: dict[str, Any]) -> int:
    """Token-overlap rank. Deliberately not fuzzy: the eval measures recall@N, and a
    scorer nobody can predict makes a failing case impossible to reason about."""
    terms = {t for t in query.lower().replace("-", " ").replace("_", " ").split() if len(t) > 2}
    if not terms:
        return 0
    name = tool["tool"].lower().replace("_", " ")
    summary = (tool.get("summary") or "").lower()
    provider = tool["provider"].lower()
    score = 0
    for term in terms:
        if term in name:
            score += 4          # the name is the strongest signal a tool answers this
        if term in provider:
            score += 3          # "pinnacle odds" should surface Pinnacle
        if term in summary:
            score += 1
    return score


def score_tools(query: str, tools: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The ranked shortlist for a query. Pure: no manager, no network, no model.

    Exposed because `evals.runner.eval_retrieval` scores THIS function. An eval that
    re-implemented the ranking would pass while the shipped search regressed, which is
    the failure mode a gate exists to prevent.
    """
    ranked = sorted(tools, key=lambda t: (-_score(query, t), t["tool"]))
    return [t for t in ranked if _score(query, t) > 0][:limit]


async def _capability_index(
    manager: MCPManager, capabilities: list[str]
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """(capability -> tool entries, every reachable tool name) for the granted set."""
    index: dict[str, list[dict[str, Any]]] = {}
    reachable: set[str] = set()
    for capability in capabilities:
        payload = await manager.call_tool("list_tools_by_capability", {"capability": capability})
        entries = (payload or {}).get("tools") or []
        kept: list[dict[str, Any]] = []
        for entry in entries:
            name = entry.get("tool")
            # A denied tool is never listed OR callable: the model cannot choose what it
            # was never shown, and cannot call what it hallucinates.
            if not name or is_denied(name):
                continue
            kept.append(
                {
                    "tool": name,
                    "provider": entry.get("provider", ""),
                    "summary": (entry.get("summary") or "")[:SUMMARY_LIMIT],
                    "args_required": entry.get("args_required") or [],
                    "capability": capability,
                }
            )
            reachable.add(name)
        index[capability] = kept
    return index, reachable


def discovery_tools(manager: MCPManager, capabilities: list[str]) -> list[ToolDef]:
    """`find_data_tools` + `call_data_tool`, scoped to `capabilities`.

    Two ToolDefs regardless of how many tools the capabilities resolve to — that is the
    whole point. The index is resolved once per run and cached, so a second search in the
    same conversation costs nothing.
    """
    cache: dict[str, Any] = {}

    async def _index() -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        if "index" not in cache:
            cache["index"] = await _capability_index(manager, capabilities)
        return cache["index"]

    async def find_data_tools(args: dict[str, Any]) -> Any:
        query = str(args.get("query") or "").strip()
        capability = args.get("capability")
        limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
        index, _ = await _index()

        if capability and capability not in index:
            return {
                "error": f"capability {capability!r} was not granted to this agent",
                "granted": sorted(index),
            }
        pool = index[capability] if capability else [t for v in index.values() for t in v]

        if query:
            hits = score_tools(query, pool, limit)
            # A query that matches nothing is a dead end unless the alternatives are
            # shown — an empty list reads as "no data exists", which is rarely true.
            if not hits:
                return {
                    "query": query,
                    "matches": [],
                    "hint": "nothing matched those words; these are the capabilities you can search",
                    "granted": {c: len(v) for c, v in index.items()},
                }
        else:
            hits = sorted(pool, key=lambda t: t["tool"])[:limit]

        return {
            "query": query or None,
            "matches": hits,
            "hint": "call one with call_data_tool(tool_name=..., args={...}); "
                    "args_required lists its mandatory arguments",
        }

    async def call_data_tool(args: dict[str, Any]) -> Any:
        name = str(args.get("tool_name") or "").strip()
        payload = args.get("args") or {}
        if not isinstance(payload, dict):
            return {"error": "args must be an object mapping argument names to values"}

        index, reachable = await _index()
        if name not in reachable:
            # Out of scope, not merely unknown: say so, and show what IS in scope rather
            # than leaving the model to guess at names.
            return {
                "error": f"{name!r} is not reachable from this agent's granted capabilities",
                "granted_capabilities": sorted(index),
                "hint": "use find_data_tools to get an exact tool name",
            }
        try:
            result = await manager.call_tool(name, payload)
        except Exception as exc:  # surfaced to the model as data, not swallowed
            entry = next((t for v in index.values() for t in v if t["tool"] == name), None)
            return {
                "error": f"{name} failed: {exc}",
                "args_required": (entry or {}).get("args_required", []),
                "hint": "check the required arguments and try again",
            }
        # Same provenance envelope bridge_mcp_tools puts on a directly-attached tool, so
        # a figure can be cited to its source whichever route reached it.
        return {
            "_source": {
                "tool": name,
                "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "via": "call_data_tool",
            },
            "data": result,
        }

    return [
        ToolDef(
            name="find_data_tools",
            description=(
                "Search the data plane for tools that answer a question, across every provider. "
                "Returns tool names, providers and required arguments — call one with call_data_tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you need, in a few words (e.g. 'closing odds', 'starting lineups').",
                    },
                    "capability": {
                        "type": "string",
                        "description": f"Optional: restrict to one capability. Granted: {', '.join(capabilities)}",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Max results (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
                    },
                },
                "required": ["query"],
            },
            execute=find_data_tools,
        ),
        ToolDef(
            name="call_data_tool",
            description=(
                "Call a data-plane tool found via find_data_tools. Read-only. "
                "Pass its exact name and an object of arguments."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "Exact name from find_data_tools."},
                    "args": {"type": "object", "description": "Arguments for that tool."},
                },
                "required": ["tool_name"],
            },
            execute=call_data_tool,
        ),
    ]
