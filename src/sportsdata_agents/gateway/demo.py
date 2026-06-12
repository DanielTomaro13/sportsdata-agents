"""Public demo surface (M3.4, D22 hybrid): curated prompts → a real read-only,
budget-capped demo run with the tool calls shown live.

Abuse posture, by construction:
- free-form input does not exist — the API takes a CURATED prompt id only;
- every run opens a fresh single-agent session in a demo workspace with a tiny
  per-run budget and tight limits;
- per-IP rate limiting on top of the gateway's tenant limiter;
- the response carries tool NAMES and timings, never raw payloads or secrets.

The marketing site (site/) renders these; when the gateway is unreachable it
falls back to an animated canned transcript (the always-on fallback).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

DEMO_BUDGET_USD = 0.30
# Curated to what the TEAM can actually answer (odds + stats specialists);
# extend as specialists gain capabilities.
DEMO_PROMPTS: list[dict[str, str]] = [
    {
        "id": "nba-finals",
        "title": "NBA Finals snapshot",
        "prompt": "What's the latest NBA Finals game result, and who leads the series?",
    },
    {
        "id": "compare-books",
        "title": "Compare the books",
        "prompt": "Pick one upcoming AFL match and compare its head-to-head odds across the bookmakers you can see.",
    },
    {
        "id": "find-value",
        "title": "Find me value",
        "prompt": "Scan the registered models against current prices — is there any value? If so, set a watch on it.",
    },
    {
        "id": "arb-scan",
        "title": "Any arbitrage?",
        "prompt": "Scan for cross-book arbitrage right now — explain what you found, "
                  "or why a clean board is the honest norm.",
    },
    {
        "id": "platform-tour",
        "title": "What can it do?",
        "prompt": "Give me the tour — summarise what this platform can do, grounded in the "
                  "capability groups you can actually see.",
    },
]
# These ids mirror site/demo-fallback.json: the static page's chips come from
# /demo/prompts the moment window.GATEWAY_URL goes live, so drift breaks the demo.


class ToolTraceRecorder:
    """A RunRecorder that keeps tool NAMES + timings (the demo's 'watch it work'
    feed) and never the arguments or results — nothing sensitive can leak.
    Forwards to ``inner`` (the DbRecorder when the DB is up) so the session gets
    the DB-backed tools — without it the find-value/arb chips answer
    "database unavailable" (found live re-recording the demo)."""

    def __init__(self, inner: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.inner = inner

    def usage_sink(self, event: Any) -> None:
        sink = getattr(self.inner, "usage_sink", None)
        if sink is not None:
            sink(event)

    async def on_run_start(self, **kw: Any) -> None:
        if self.inner:
            await self.inner.on_run_start(**kw)

    async def on_tool_call(self, *, run_id: uuid.UUID, tool: str, arguments: dict[str, Any],
                           ok: bool, latency_ms: int) -> None:
        self.calls.append({"tool": tool, "ok": ok, "latency_ms": latency_ms})
        if self.inner:
            await self.inner.on_tool_call(run_id=run_id, tool=tool, arguments=arguments,
                                          ok=ok, latency_ms=latency_ms)

    async def on_run_end(self, **kw: Any) -> None:
        if self.inner:
            await self.inner.on_run_end(**kw)


def demo_prompt(prompt_id: str) -> dict[str, str]:
    for entry in DEMO_PROMPTS:
        if entry["id"] == prompt_id:
            return entry
    raise KeyError(prompt_id)


async def run_demo(prompt_id: str) -> dict[str, Any]:
    """One curated demo run in a fresh, budget-capped session."""
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.gateway.service import TeamSession, detect_tier_overrides, try_db_recorder
    from sportsdata_agents.workspace import Budgets, Workspace

    entry = demo_prompt(prompt_id)  # KeyError -> 404 at the route
    trace = ToolTraceRecorder(
        inner=await try_db_recorder(get_settings(), TenantScope("demo", "demo"))
    )
    workspace = Workspace(
        tenant_id="demo", workspace_id="demo",
        model_tiers=detect_tier_overrides(),
        budgets=Budgets(per_run_usd=DEMO_BUDGET_USD, max_tool_calls=12, max_steps=16,
                        max_tokens=60_000, timeout_seconds=120),
    )
    # the REAL team answers (that's the demo's point) — orchestrator routes to a
    # specialist; the trace shows the delegation + its tool calls
    session = TeamSession(workspace=workspace, recorder=trace, extra_tools=[])
    async with session:
        result = await session.run(entry["prompt"])
    return {
        "prompt_id": prompt_id,
        "prompt": entry["prompt"],
        "answer": result.output,
        "tool_calls": trace.calls,
        "cost_usd": round(result.cost_usd, 4),
        "verified": result.verified,
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }


async def demo_stats() -> dict[str, Any]:
    """Live capability counters from the data plane (cache at the route)."""
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.mcp.manager import MCPManager

    async with MCPManager(groups=["*"], command=get_settings().mcp_command) as manager:
        payload = await manager.call_tool("list_available_groups", {})
    # shape (verified live): {"enabled": [...], "available": {group: {provider, tools, ...}}}
    available = payload.get("available") or {}
    providers = {str(info.get("provider", group.split(".")[0]))
                 for group, info in available.items()}
    return {
        "providers": len(providers),
        "groups": len(available),
        "tools": sum(int(info.get("tools", 0)) for info in available.values()),
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }
