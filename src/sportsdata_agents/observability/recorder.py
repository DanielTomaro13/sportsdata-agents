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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import CURRENT_RUN_ID
from sportsdata_agents.data.models import AgentRun, RunTranscript, ToolCall, UsageLedger
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.models.gateway import UsageEvent

logger = logging.getLogger(__name__)

# Trace capture (M4.5): how much of a run's transcript we persist. Generous enough to
# show the reasoning + tool results, bounded so a chatty run can't bloat the warehouse.
TRANSCRIPT_MAX_MESSAGES = 80
TRANSCRIPT_CHARS_PER_MESSAGE = 4000
INPUT_TASK_CHARS = 2000


def distill_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A compact, JSON-safe trace of a run: drop the (large, static) system prompt,
    keep each turn's role + truncated content, and note any tool calls an assistant
    turn made. This is what the workbench renders as 'what it did / said to itself'."""
    out: list[dict[str, Any]] = []
    for m in messages[:TRANSCRIPT_MAX_MESSAGES]:
        role = str(m.get("role") or "")
        if role == "system":
            continue
        entry: dict[str, Any] = {
            "role": role,
            "content": str(m.get("content") or "")[:TRANSCRIPT_CHARS_PER_MESSAGE],
        }
        tool_calls = m.get("tool_calls")
        if tool_calls:
            names: list[str] = []
            for tc in tool_calls:
                name = getattr(tc, "name", None)
                if name is None and isinstance(tc, dict):
                    name = tc.get("name") or (tc.get("function") or {}).get("name")
                if name:
                    names.append(str(name))
            if names:
                entry["tools"] = names
        out.append(entry)
    return out


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
        error: str | None = None, transcript: list[dict[str, Any]] | None = None,
    ) -> None: ...


class DbRecorder:
    """Persists runs/tool-calls/usage to the §9 tables, tenant-scoped."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], scope: TenantScope) -> None:
        self._sf = session_factory
        self._scope = scope
        self._usage: dict[uuid.UUID | None, list[UsageEvent]] = defaultdict(list)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """For session-bound tool factories (tracking/memory) sharing this DB."""
        return self._sf

    @property
    def scope(self) -> TenantScope:
        return self._scope

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
                    input_task=task[:INPUT_TASK_CHARS] if task else None,
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
        error: str | None = None, transcript: list[dict[str, Any]] | None = None,
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
            if transcript:  # M4.5: the run's trace (distilled here; idempotent per run)
                distilled = distill_transcript(transcript)
                existing = await session.scalar(
                    select(RunTranscript).where(RunTranscript.agent_run_id == run_id)
                )
                if existing is None:
                    session.add(
                        RunTranscript(
                            tenant_id=self._scope.tenant_id,
                            workspace_id=self._scope.workspace_id,
                            agent_run_id=run_id,
                            messages=distilled,
                        )
                    )
                else:
                    existing.messages = distilled
            await session.commit()
