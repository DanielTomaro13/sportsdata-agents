"""MCP client manager — the agent plane's only door to the data plane.

Spawns ``sportsdata-mcp`` as a **stdio subprocess**, scoped per agent via the
``SPORTSDATA_MCP_GROUPS`` env var (least privilege, §13), and exposes the scoped tool
catalogue.

**Scoping** is the structural guarantee that remains: the subprocess only ever registers
the groups it was started with, so an agent cannot call a tool outside its scope because
the server never has it. Placement tools live in a `<provider>.write` group reachable
only by exact name — never via a preset, a glob or `all` — so a session that was not
deliberately started with that group has no money tool in it at all.

## The no-money deny-filter was removed (2026-08-27), deliberately

This module used to hide and refuse any tool whose NAME matched money verbs
(`place|stake|balance|…`), on the stated premise that "the MCP has no placement tools at
source". That premise stopped being true when the data plane gained
`sportsbet_place_bet` and its three siblings, and the ban was removed rather than
quietly worked around.

What replaced it is a real gate rather than a name match: `sportsdata_agents.betting`
decides — in deterministic code, on typed numeric fields — whether a bet may be placed,
at what size, and whether a human must approve first. See `betting/policy.py`.

**What this costs, stated plainly.** A name filter could not be talked out of anything;
a policy can only be as good as its own arithmetic. The scanner reads bookmaker pages
and API responses, which are attacker-controlled content, so the risk this filter used
to blunt — injected text steering an agent toward a placement — is now carried entirely
by that policy and by group scoping. The policy is therefore written to touch no free
text at all, and no prompt can widen one of its limits.

**What it buys.** The filter was blunt enough to hide read-only tools that matter:
account balance (which Kelly sizing needs), cash-out availability, and every
`*_price_slip` pre-placement quote. Those are back.

`moves_money()` survives as a CLASSIFIER, not a gate — it labels the tools that can move
real money so they can be logged, surfaced differently, and asserted about in tests.

The Pydantic AI toolset adapter over this manager lands at M0.6 with the agent runtime.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sportsdata_agents.config import get_settings

log = logging.getLogger(__name__)

# The minimum sportsdata-mcp the agents' runtime contracts assume (SPORTSDATA_MCP_GROUPS
# scoping, meta-tool names, the licence gate). The two repos version independently (the
# desktop DMG co-bundles a matched pair, but `uvx`/dev installs don't), so we read the
# server's version from the MCP `initialize` handshake and WARN on a mismatch rather than
# fail — a too-old data plane surfaces as a loud log line, not a silent tool error.
# 0.31.1 is the floor because of a CORRECTNESS difference, not a missing feature.
# 0.31.0 shipped Unibet's placement with the wrong auth (a `Cookie` header from
# UNIBET_KAMBI_COOKIE) and the wrong coupon strings ("COMBINATION" for both operation
# and type). Against that data plane the betting plane sends a credential Kambi never
# wanted, in a body it would not recognise, and gets a 401 that looks like a dead token.
# The tools EXIST at 0.31.0, so nothing errors on startup — which is exactly why the
# floor has to be a version rather than a capability check.
MIN_MCP_VERSION = (0, 31, 1)


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def _check_mcp_version(init_result: Any) -> None:
    info = getattr(init_result, "serverInfo", None)
    name = (getattr(info, "name", "") or "").lower()
    ver = getattr(info, "version", "") or ""
    if "sportsdata" not in name or not ver:
        return  # not our server, or it doesn't report a version — nothing to assert
    if _ver_tuple(ver) < MIN_MCP_VERSION:
        log.warning(
            "data plane sportsdata-mcp %s is older than the minimum %s the agents expect — "
            "tool contracts may mismatch; update the MCP (or rebuild the desktop app)",
            ver, ".".join(map(str, MIN_MCP_VERSION)),
        )


# Tools that can move REAL money. This is a classifier, not a gate — nothing here
# blocks a call. It exists so money-movers can be logged loudly, shown differently in a
# UI, and asserted about in tests.
#
# Narrower than the old deny-filter on purpose. That one matched `balance`, `cashout`
# and `betslip` too, which are READS: knowing an account balance is how Kelly sizing
# gets a bankroll, and a `*_price_slip` quote is the call you are supposed to make
# immediately BEFORE placing. Lumping reads in with writes is what made the old filter
# cost real functionality.
MONEY_PATTERN = re.compile(
    r"(place_bet|placebet|deposit|withdraw|_wager|payout_request|wallet_transfer)",
    re.IGNORECASE,
)

# EXACT names that match the pattern but move no real money. Listed by exact name on
# purpose: loosening the regex instead would silently re-admit anything else named that
# way, and the value of this classifier is that it is blunt.
MONEY_EXCEPTIONS = frozenset({
    "fpl_transfers",         # MCP: FPL squad transfers (in-game currency only)
    "fpl_propose_transfer",  # native: the policy-gated wrapper around it
})


class ForbiddenToolError(PermissionError):
    """Raised when something refuses a tool call on policy grounds.

    No longer raised by this module — the blanket name-ban is gone (see the module
    docstring). Kept because callers still catch it, and because the betting plane's
    own gate is the right place for a refusal to originate.
    """

    def __init__(self, tool: str) -> None:
        super().__init__(f"tool {tool!r} was refused by policy; refusing to expose or call it")
        self.tool = tool


class PollBudgetExceeded(RuntimeError):
    """One provider was called too many times in a single run."""

    def __init__(self, provider: str, cap: int) -> None:
        super().__init__(
            f"poll budget spent: {provider!r} has been called {cap} times in this run. "
            f"Do not keep polling it — use the data already fetched, or set a watch so "
            f"the monitor tracks it on its own cadence."
        )
        self.provider = provider
        self.cap = cap


class PollBudget:
    """Per-provider call cap for one run (§13).

    IN-PLAY IS WHY THIS EXISTS. Pre-game, an agent asks a book for a price once and
    moves on. Live, the tempting shape is a loop — fetch, look, fetch again — and the
    request rate that produces comes off the USER'S OWN IP. A bookmaker rate-limits or
    bans the user, not us, and they would have no idea why. `live_desk`'s prompt says
    not to loop, but a prompt is guidance; this is the limit.

    Counted per provider rather than in total because the failure is concentrated: forty
    calls spread over forty books is ordinary work, and forty calls to one book is the
    thing that gets noticed.
    """

    def __init__(self, per_provider: int) -> None:
        self.per_provider = per_provider
        self._spent: dict[str, int] = {}

    @staticmethod
    def provider_of(tool_name: str) -> str:
        # MCP tool names are "<provider>_<op>" and provider ids never contain "_" —
        # the same assumption CURRENT_MCP_DENY relies on.
        return tool_name.split("_", 1)[0]

    def charge(self, tool_name: str) -> None:
        provider = self.provider_of(tool_name)
        spent = self._spent.get(provider, 0)
        if spent >= self.per_provider:
            raise PollBudgetExceeded(provider, self.per_provider)
        self._spent[provider] = spent + 1

    def spent(self) -> dict[str, int]:
        return dict(self._spent)


#: The poll budget of the currently-executing run, async-safe and inherited by delegated
#: sub-runs — same pattern as the cost budget, and for the same reason: without it a
#: team run would get one full budget per agent, which is not a budget.
CURRENT_POLL_BUDGET: contextvars.ContextVar[PollBudget | None] = contextvars.ContextVar(
    "current_poll_budget", default=None
)


def moves_money(tool_name: str) -> bool:
    """True if calling this tool can move real money.

    A LABEL, not a permission check. Callers use it to log loudly and to mark a tool in
    a UI; the decision about whether a bet may actually be placed belongs to
    `sportsdata_agents.betting.policy`, which reasons about edge, stake and budget
    rather than about spelling.
    """
    if tool_name in MONEY_EXCEPTIONS:
        return False
    return bool(MONEY_PATTERN.search(tool_name))


def is_denied(tool_name: str) -> bool:
    """Deprecated. The no-money name-ban was removed on 2026-08-27; nothing is denied by
    name any more, so this always returns False.

    Kept so existing call sites keep working while they are migrated. Do not add new
    uses — if you want to know whether a tool touches money, ask `moves_money`; if you
    want to know whether a bet may be placed, ask the betting policy.
    """
    return False


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

    def _subprocess_env(self) -> dict[str, str]:
        env = {**os.environ, **self._extra_env}
        # Empty groups = deliberately unscoped: ask the server for the FULL catalogue
        # ("*" wildcard, sportsdata-mcp >= v0.2.2). Without this, no env var would mean
        # "nothing enabled" — only the meta-tools — and capability filters resolve empty.
        env["SPORTSDATA_MCP_GROUPS"] = ",".join(self.groups) if self.groups else "*"
        # Megabyte bookmaker feeds destroy the context budget (observed: one run burned
        # its cost ceiling on a handful of market payloads). Cap responses unless the
        # operator already set a cap themselves.
        env.setdefault("SPORTSDATA_MCP_MAX_BYTES", "150000")
        return env

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        env = self._subprocess_env()
        params = StdioServerParameters(command=self._command[0], args=self._command[1:], env=env)
        # If anything after the spawn fails, Python will NOT call __aexit__ (CM protocol),
        # so we must tear down ourselves or the subprocess is orphaned.
        try:
            self._stdio_cm = stdio_client(params)
            read, write = await self._stdio_cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            _check_mcp_version(await self._session.initialize())
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
        """The scoped tool catalogue, cached for the session.

        No longer deny-filtered: scoping (which groups the subprocess registered) is what
        bounds this, not a name match. See the module docstring.

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
            self._tools_cache = tools
        return self._tools_cache

    async def tool_names(self) -> set[str]:
        return {t.name for t in await self.list_tools()}

    async def tools_for_capability(self, capability: str) -> list[str]:
        """Cross-provider tool names for a capability tag (via the MCP meta-tool)."""
        payload = await self.call_tool("list_tools_by_capability", {"capability": capability})
        tools = payload.get("tools", []) if isinstance(payload, dict) else []
        names = [t.get("tool") or t.get("name") for t in tools if isinstance(t, dict)]
        return [n for n in names if n]

    # ── calls ──────────────────────────────────────────────────────────────

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 60.0,
    ) -> Any:
        """Call a tool and return its JSON payload. Nothing is refused by name here —
        a tool that can move money is logged loudly and passed through, because the
        decision to place belongs to the betting policy, not to a regex.

        A default read timeout stops a wedged upstream from hanging the agent forever
        (the harness's own budgets layer on top at M0.7).
        """
        if moves_money(name):
            # Not a gate — the betting policy decides that. But a call that can move real
            # money is never allowed to happen quietly: this is the one chokepoint every
            # route reaches, so it is the honest place to record that it happened.
            log.warning("MONEY TOOL CALLED: %s — this can move real funds", name)
        # Charged here, the one chokepoint every route reaches — a directly attached
        # tool, a discovered one via call_data_tool, or a delegated sub-agent's call.
        budget = CURRENT_POLL_BUDGET.get()
        if budget is not None:
            budget.charge(name)
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
