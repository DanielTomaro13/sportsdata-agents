"""Console progress: live delegation/tool lines while the team works (CLI-only).

Wraps an optional inner recorder (the DbRecorder) — printing is additive, persistence
is untouched, and like all recording it must never break a run.
"""

from __future__ import annotations

import uuid
from typing import Any

from rich.console import Console

from sportsdata_agents.models.gateway import UsageEvent
from sportsdata_agents.observability.recorder import RunRecorder


class ConsoleProgressRecorder:
    """Prints run progress; forwards every hook (and the usage sink) to ``inner``."""

    def __init__(self, console: Console, inner: RunRecorder | None = None) -> None:
        self.console = console
        self.inner = inner

    def usage_sink(self, event: UsageEvent) -> None:
        sink = getattr(self.inner, "usage_sink", None)
        if sink is not None:
            sink(event)

    async def on_run_start(
        self, *, run_id: uuid.UUID, parent_run_id: uuid.UUID | None, agent: str, task: str
    ) -> None:
        if parent_run_id is not None:  # only narrate delegations, not the root run
            self.console.print(f"  [dim]→ {agent}: {task[:80]}[/dim]")
        if self.inner is not None:
            await self.inner.on_run_start(run_id=run_id, parent_run_id=parent_run_id, agent=agent, task=task)

    async def on_tool_call(
        self, *, run_id: uuid.UUID, tool: str, arguments: dict[str, Any], ok: bool, latency_ms: int
    ) -> None:
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        self.console.print(f"    [dim]{mark} {tool} ({latency_ms} ms)[/dim]")
        if self.inner is not None:
            await self.inner.on_tool_call(
                run_id=run_id, tool=tool, arguments=arguments, ok=ok, latency_ms=latency_ms
            )

    async def on_run_end(
        self, *, run_id: uuid.UUID, agent: str, status: str, cost_usd: float, latency_ms: int,
        error: str | None = None,
    ) -> None:
        if self.inner is not None:
            await self.inner.on_run_end(
                run_id=run_id, agent=agent, status=status, cost_usd=cost_usd, latency_ms=latency_ms, error=error
            )
