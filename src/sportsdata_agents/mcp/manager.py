"""MCP client manager — the agent plane's only door to the data plane.

Spawns ``sportsdata-mcp`` as a **stdio subprocess**, scoped per agent via the
``SPORTSDATA_MCP_GROUPS`` env var (least privilege, §13), and exposes the scoped tool
catalogue. Two structural guarantees live here:

1. **Scoping** — the subprocess only ever registers the groups it was started with;
   an agent cannot call a tool outside its scope because the server never has it.
2. **No-money deny-filter (defense in depth)** — the MCP has no placement/deposit/
   account tools at source (verified pre-flight), but the manager additionally hides
   and refuses any tool whose name matches money/placement verbs, so a compromised or
   future data plane still can't surface one to an agent.

The Pydantic AI toolset adapter over this manager lands at M0.6 with the agent runtime.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sportsdata_agents.config import get_settings

# Money/placement verbs an agent-facing tool name must never carry (§13). Deliberately
# strict: it also hides the read-only `betfair_cashout` availability feed — losing one
# harmless tool is the accepted cost of an airtight name-based deny.
DENY_PATTERN = re.compile(
    r"(place|deposit|withdraw|stake|wager|payout|wallet|balance|transfer|betslip|bet_slip|checkout|cashout)",
    re.IGNORECASE,
)


class ForbiddenToolError(PermissionError):
    """Raised when something asks for a tool the no-money invariant forbids."""

    def __init__(self, tool: str) -> None:
        super().__init__(f"tool {tool!r} is forbidden by the no-money invariant (§13); refusing to expose or call it")
        self.tool = tool


def is_denied(tool_name: str) -> bool:
    """True if a tool name trips the no-money deny-filter."""
    return bool(DENY_PATTERN.search(tool_name))


class MCPManager:
    """One scoped stdio session to the data plane. Use as an async context manager.

    Args:
        groups: MCP tool groups this session may register (``SPORTSDATA_MCP_GROUPS``).
            Empty = unscoped (local dev / the orchestrator's discovery session).
        command: argv to launch the server; defaults to ``Settings.mcp_command``.
        extra_env: extra env vars for the subprocess (e.g. a resolved ``DATAGOLF_KEY``).
    """

    def __init__(
        self,
        *,
        groups: Sequence[str] = (),
        command: Sequence[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.groups = list(groups)
        self._command = list(command or get_settings().mcp_command)
        self._extra_env = dict(extra_env or {})
        self._session: ClientSession | None = None
        self._stdio_cm: Any = None
        self._session_cm: Any = None
        self._tools_cache: list[Any] | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        env = {**os.environ, **self._extra_env}
        if self.groups:
            env["SPORTSDATA_MCP_GROUPS"] = ",".join(self.groups)
        params = StdioServerParameters(command=self._command[0], args=self._command[1:], env=env)
        # If anything after the spawn fails, Python will NOT call __aexit__ (CM protocol),
        # so we must tear down ourselves or the subprocess is orphaned.
        try:
            self._stdio_cm = stdio_client(params)
            read, write = await self._stdio_cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # finally-chained so a failing session close can never leak the subprocess.
        try:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(exc_type, exc, tb)
        finally:
            self._session_cm = None
            try:
                if self._stdio_cm is not None:
                    await self._stdio_cm.__aexit__(exc_type, exc, tb)
            finally:
                self._stdio_cm = None
                self._session = None
                self._tools_cache = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCPManager is not started; use `async with MCPManager(...)`")
        return self._session

    # ── catalogue ──────────────────────────────────────────────────────────

    async def list_tools(self) -> list[Any]:
        """The scoped tool catalogue, deny-filtered, cached for the session.

        Follows ``nextCursor`` pagination — an unscoped discovery session can exceed one
        page (342 tools), and silently truncating it would hide tools from the orchestrator.
        """
        if self._tools_cache is None:
            tools: list[Any] = []
            cursor: str | None = None
            while True:
                result = await self.session.list_tools(cursor=cursor)
                tools.extend(result.tools)
                cursor = getattr(result, "nextCursor", None)
                if not cursor:
                    break
            self._tools_cache = [t for t in tools if not is_denied(t.name)]
        return self._tools_cache

    async def tool_names(self) -> set[str]:
        return {t.name for t in await self.list_tools()}

    async def tools_for_capability(self, capability: str) -> list[str]:
        """Cross-provider tool names for a capability tag (via the MCP meta-tool)."""
        payload = await self.call_tool("list_tools_by_capability", {"capability": capability})
        tools = payload.get("tools", []) if isinstance(payload, dict) else []
        names = [t.get("tool") or t.get("name") for t in tools if isinstance(t, dict)]
        return [n for n in names if n and not is_denied(n)]

    # ── calls ──────────────────────────────────────────────────────────────

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 60.0,
    ) -> Any:
        """Call a tool and return its JSON payload. Denied names are refused up front.

        A default read timeout stops a wedged upstream from hanging the agent forever
        (the harness's own budgets layer on top at M0.7).
        """
        if is_denied(name):
            raise ForbiddenToolError(name)
        result = await self.session.call_tool(
            name, arguments or {}, read_timeout_seconds=dt.timedelta(seconds=timeout_seconds)
        )
        if getattr(result, "isError", False):
            text = _first_text(result)
            raise RuntimeError(f"tool {name} failed: {text or 'unknown error'}")
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        text = _first_text(result)
        if text is None:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text


def _first_text(result: Any) -> str | None:
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return str(text)
    return None
