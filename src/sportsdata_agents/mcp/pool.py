"""MCP session pool: one subprocess per *identical scope*, shared across agents.

Each capability-only agent wants the full catalogue ("*"); without pooling, a team
spawns one full server per agent (~1.5s startup + a process each). Sharing is safe
**only across identical scopes** — the subprocess scope is the least-privilege
boundary (§13), so different groups/env still get separate processes. Per-agent
capability filtering happens above this layer, in the bridge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Self

from sportsdata_agents.mcp.manager import MCPManager

PoolKey = tuple[tuple[str, ...], tuple[tuple[str, str], ...]]


class MCPSessionPool:
    """Owns the managers it creates; borrowers must not close them."""

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self._command = list(command) if command else None
        self._managers: dict[PoolKey, MCPManager] = {}

    def __len__(self) -> int:
        return len(self._managers)

    async def get(self, groups: Sequence[str], extra_env: Mapping[str, str] | None = None) -> MCPManager:
        """A started manager for this scope — created on first use, then shared."""
        key: PoolKey = (tuple(sorted(groups)), tuple(sorted((extra_env or {}).items())))
        if key not in self._managers:
            manager = MCPManager(groups=list(groups), command=self._command, extra_env=dict(extra_env or {}))
            await manager.__aenter__()
            self._managers[key] = manager
        return self._managers[key]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Close every owned manager; one failing close must not leak the rest.
        managers = list(self._managers.values())
        self._managers.clear()
        first_error: BaseException | None = None
        for manager in managers:
            try:
                await manager.__aexit__(exc_type, exc, tb)
            except BaseException as e:  # collect; close the rest, then re-raise
                first_error = first_error or e
        if first_error is not None:
            raise first_error
