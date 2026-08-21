"""Wiring the platform-agnostic write plane to the platforms.

`fantasy/` deliberately knows nothing about MCP: `execute_approved` takes a function
from a platform name to something that can call its tools. This module is the one place
that mapping lives, so the CLI and the scheduler cannot drift into using different ones —
which on a write path would mean an approval executed against the wrong catalogue.
"""

from __future__ import annotations

from typing import Any

from .approvals import Proposal
from .execute import Outcome, execute_approved


def call_for(platform: str):
    """Something that can call `platform`'s MCP tools."""
    if platform == "espn":
        from ..tools.espn_fantasy import _mcp_call
    else:
        from ..tools.fantasy import _mcp_call

    async def call(tool: str, **kwargs: Any) -> Any:
        return await _mcp_call(tool, kwargs)

    return call


def csrf_for(platform: str) -> str:
    """FPL writes need an X-CSRFToken header; ESPN's cookie pair is enough on its own."""
    if platform == "espn":
        return ""
    from ..tools.fantasy import _csrf

    return _csrf()


async def drain_approved(*, only: str | None = None) -> list[tuple[Proposal, Outcome]]:
    """Carry out everything the owner has approved and that has not expired."""
    return await execute_approved(call_for=call_for, csrf_for=csrf_for, only=only)
