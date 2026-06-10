"""Run recording (§16): every run, tool call, and model usage event → audit rows.

The harness calls ``RunRecorder`` hooks around its loop; ``DbRecorder`` persists them
to ``agent_runs`` / ``tool_calls`` / ``usage_ledger`` (§9). Delegated sub-runs link to
their caller via ``parent_run_id`` (the harness's run-id contextvar). Two rules:

- **Recording must never break a run** — the harness guards every hook call.
- **Costs flush at run end** — gateway ``UsageEvent``s are buffered per run (the sink
  is sync; DB writes are async) and written in one transaction with the run update.
  A crashed process loses in-flight usage rows; acceptable for now, noted.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import CURRENT_RUN_ID
from sportsdata_agents.data.models import AgentRun, ToolCall, UsageLedger
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.models.gateway import UsageEvent

logger = logging.getLogger(__name__)


class RunRecorder(Protocol):
    """What the harness needs to record a run. All hooks are fire-and-forget-safe."""

    async def on_run_start(
        self, *, run_id: uuid.UUID, parent_run_id: uuid.UUID | None, agent: str, task: str
    ) -> None: ...

    async def on_tool_call(
        self, *, run_id: uuid.UUID, tool: str, arguments: dict[str, Any], ok: bool, latency_ms: int
    ) -> None: ...

    async def on_run_end(
        self, *, run_id: uuid.UUID, agent: str, status: str, cost_usd: float, latency_ms: int,
        error: str | None = None,
    ) -> None: ...


class DbRecorder:
    """Persists runs/tool-calls/usage to the §9 tables, tenant-scoped."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], scope: TenantScope) -> None:
        self._sf = session_factory
        self._scope = scope
        self._usage: dict[uuid.UUID | None, list[UsageEvent]] = defaultdict(list)

    # ── gateway sink (sync; buffered per current run) ─────────────────────

    def usage_sink(self, event: UsageEvent) -> None:
        run_id = CURRENT_RUN_ID.get()
        if run_id is None:
            # No owning run to flush under — buffering would leak unboundedly.
            logger.warning("usage event outside any run dropped (model=%s cost=%.6f)", event.model, event.cost_usd)
            return
        self._usage[run_id].append(event)

    # ── harness hooks ──────────────────────────────────────────────────────

    async def on_run_start(
        self, *, run_id: uuid.UUID, parent_run_id: uuid.UUID | None, agent: str, task: str
    ) -> None:
        logger.info("run_start agent=%s run_id=%s parent=%s", agent, run_id, parent_run_id)
        async with self._sf() as session:
            session.add(
                AgentRun(
                    id=run_id,
                    tenant_id=self._scope.tenant_id,
                    workspace_id=self._scope.workspace_id,
                    parent_run_id=parent_run_id,
                    agent=agent,
                    status="running",
                )
            )
            await session.commit()

    async def on_tool_call(
        self, *, run_id: uuid.UUID, tool: str, arguments: dict[str, Any], ok: bool, latency_ms: int
    ) -> None:
        logger.info("tool_call run_id=%s tool=%s ok=%s latency_ms=%d", run_id, tool, ok, latency_ms)
        async with self._sf() as session:
            session.add(
                ToolCall(
                    tenant_id=self._scope.tenant_id,
                    workspace_id=self._scope.workspace_id,
                    agent_run_id=run_id,
                    tool=tool,
                    args=arguments,
                    ok=ok,
                    latency_ms=latency_ms,
                )
            )
            await session.commit()

    async def on_run_end(
        self, *, run_id: uuid.UUID, agent: str, status: str, cost_usd: float, latency_ms: int,
        error: str | None = None,
    ) -> None:
        events = self._usage.pop(run_id, [])
        tokens_in = sum(e.tokens_in for e in events)
        tokens_out = sum(e.tokens_out for e in events)
        logger.info(
            "run_end agent=%s run_id=%s status=%s cost=%.6f tokens=%d/%d",
            agent, run_id, status, cost_usd, tokens_in, tokens_out,
        )
        async with self._sf() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                # on_run_start may have failed (e.g. DB blip): create the row now, or the
                # ledger inserts below would orphan-FK on Postgres.
                run = AgentRun(
                    id=run_id,
                    tenant_id=self._scope.tenant_id,
                    workspace_id=self._scope.workspace_id,
                    agent=agent,
                )
                session.add(run)
            run.status = status
            run.error = error
            run.cost_usd = Decimal(str(round(cost_usd, 6)))
            run.latency_ms = latency_ms
            run.tokens_in = tokens_in
            run.tokens_out = tokens_out
            run.model = events[-1].model if events else None
            run.tier = events[-1].tier if events else None
            run.finished_at = dt.datetime.now(dt.UTC)
            for e in events:
                session.add(
                    UsageLedger(
                        tenant_id=self._scope.tenant_id,
                        workspace_id=self._scope.workspace_id,
                        agent_run_id=run_id,
                        kind=e.kind,
                        model=e.model,
                        tokens_in=e.tokens_in,
                        tokens_out=e.tokens_out,
                        cost_usd=Decimal(str(round(e.cost_usd, 6))),
                    )
                )
            await session.commit()
