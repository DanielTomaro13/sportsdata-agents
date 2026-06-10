"""Bridge MCP tools into harness ``ToolDef``s, filtered by capability (§7/§8.2).

The spec's `mcp_capabilities` are resolved against the live catalogue via the MCP's
own `list_tools_by_capability` meta-tool — the deferred M0.6 check happens here: a
capability that resolves to **zero tools** is a loud error (the spec granted data the
deployment doesn't carry), not a silently tool-less agent.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.mcp.manager import MCPManager


class CapabilityResolutionError(ValueError):
    """A granted capability matched no tools in the live catalogue."""

    def __init__(self, capability: str) -> None:
        super().__init__(
            f"capability {capability!r} resolved to zero tools in the live MCP catalogue — "
            f"either the tag is wrong or the enabled groups don't carry it"
        )
        self.capability = capability


def _executor(manager: MCPManager, tool_name: str) -> Any:
    async def execute(args: dict[str, Any]) -> Any:
        payload = await manager.call_tool(tool_name, args)
        # Provenance envelope (§13.1): the model can cite tool + fetch time per figure,
        # and the renderer/verifier can trace every number to its source.
        return {
            "_source": {"tool": tool_name, "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")},
            "data": payload,
        }

    return execute


async def bridge_mcp_tools(manager: MCPManager, capabilities: list[str] | None = None) -> list[ToolDef]:
    """ToolDefs for the manager's (deny-filtered) catalogue, optionally capability-scoped."""
    catalogue = await manager.list_tools()

    allowed: set[str] | None = None
    if capabilities:
        allowed = set()
        for cap in capabilities:
            names = await manager.tools_for_capability(cap)
            if not names:
                raise CapabilityResolutionError(cap)
            allowed.update(names)

    defs: list[ToolDef] = []
    for tool in catalogue:
        if allowed is not None and tool.name not in allowed:
            continue
        defs.append(
            ToolDef(
                name=tool.name,
                description=getattr(tool, "description", "") or "",
                parameters=getattr(tool, "inputSchema", None) or {"type": "object"},
                execute=_executor(manager, tool.name),
            )
        )
    return defs
