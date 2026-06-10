"""M0.11 — run/tool/usage persistence via the DbRecorder (in-memory SQLite)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.agents.harness import Harness, ToolDef
from sportsdata_agents.agents.runtime import AgentRuntime
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.data.models import AgentRun, ToolCall, UsageLedger
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest, UsageEvent
from sportsdata_agents.observability.recorder import DbRecorder
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.integration

WS = Workspace(tenant_id="t", workspace_id="w")
SCOPE = TenantScope("t", "w")


def _spec(id_: str, **over: Any) -> AgentSpec:
    base: dict[str, Any] = {"id": id_, "display_name": id_, "system_prompt": "x"}
    base.update(over)
    return AgentSpec.model_validate(base)


def _text(text: str) -> ModelReply:
    return ModelReply(text=text, model="fake-model", tokens_in=50, tokens_out=10, cost_usd=0.002)


def _tool_call(name: str) -> ModelReply:
    return ModelReply(
        text="", model="fake-model", tokens_in=50, tokens_out=10, cost_usd=0.002,
        tool_calls=(ToolCallRequest(id="c", name=name, arguments={"a": 1}),),
    )


class MeteredProvider:
    """Scripted provider that emits UsageEvents like the real gateway (sink + budget)."""

    def __init__(self, recorder: DbRecorder, *replies: ModelReply) -> None:
        self.recorder = recorder
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if budget is not None:
            budget.charge(reply.cost_usd)
        self.recorder.usage_sink(
            UsageEvent(
                kind="llm", model=reply.model, tier=tier, tokens_in=reply.tokens_in,
                tokens_out=reply.tokens_out, cost_usd=reply.cost_usd, latency_ms=5,
                tenant_id=workspace.tenant_id, workspace_id=workspace.workspace_id,
            )
        )
        return reply


def echo_tool() -> ToolDef:
    async def execute(args: dict[str, Any]) -> Any:
        return {"echo": args}

    return ToolDef(name="echo", description="", parameters={"type": "object"}, execute=execute)


def boom_tool() -> ToolDef:
    async def execute(args: dict[str, Any]) -> Any:
        raise RuntimeError("kaput")

    return ToolDef(name="boom", description="", parameters={"type": "object"}, execute=execute)


async def test_run_tool_and_usage_rows_persisted(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    recorder = DbRecorder(db_sessionmaker, SCOPE)
    provider = MeteredProvider(recorder, _tool_call("echo"), _text("final"))
    h = Harness(_spec("a1"), provider=provider, workspace=WS, tools=[echo_tool()], recorder=recorder)
    res = await h.run("q")
    assert res.stop_reason == "done"

    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun))).scalar_one()
        assert run.agent == "a1" and run.status == "ok"
        assert run.tenant_id == "t" and run.workspace_id == "w"
        assert run.parent_run_id is None
        assert float(run.cost_usd) == pytest.approx(0.004)
        assert run.tokens_in == 100 and run.tokens_out == 20
        assert run.model == "fake-model" and run.finished_at is not None

        calls = (await s.execute(select(ToolCall))).scalars().all()
        assert len(calls) == 1
        assert calls[0].tool == "echo" and calls[0].ok is True and calls[0].args == {"a": 1}
        assert calls[0].agent_run_id == run.id

        ledger = (await s.execute(select(UsageLedger))).scalars().all()
        assert len(ledger) == 2  # one row per model call
        assert all(row.agent_run_id == run.id for row in ledger)
        assert sum(float(r.cost_usd) for r in ledger) == pytest.approx(0.004)


async def test_delegation_records_parent_run_id(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    recorder = DbRecorder(db_sessionmaker, SCOPE)
    sub_provider = MeteredProvider(recorder, _text("sub answer"))
    orch_provider = MeteredProvider(
        recorder,
        ModelReply(text="", model="fake-model", tokens_in=50, tokens_out=10, cost_usd=0.002,
                   tool_calls=(ToolCallRequest(id="d", name="sub_agent", arguments={"task": "t"}),)),
        _text("final"),
    )
    async with (
        AgentRuntime(_spec("sub_agent"), provider=sub_provider, workspace=WS, recorder=recorder) as sub,
        AgentRuntime(
            _spec("orch", can_delegate_to=["sub_agent"]),
            provider=orch_provider, workspace=WS, delegates=[sub], recorder=recorder,
        ) as orch,
    ):
        await orch.run("q")

    async with db_sessionmaker() as s:
        runs = {r.agent: r for r in (await s.execute(select(AgentRun))).scalars().all()}
        assert set(runs) == {"orch", "sub_agent"}
        assert runs["orch"].parent_run_id is None
        assert runs["sub_agent"].parent_run_id == runs["orch"].id  # the audit tree
        # delta accounting: child cost is its own; parent cost is the team total
        assert float(runs["sub_agent"].cost_usd) == pytest.approx(0.002)
        assert float(runs["orch"].cost_usd) == pytest.approx(0.006)


async def test_failed_tool_recorded_ok_false(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    recorder = DbRecorder(db_sessionmaker, SCOPE)
    provider = MeteredProvider(recorder, _tool_call("boom"), _text("recovered"))
    h = Harness(_spec("a2"), provider=provider, workspace=WS, tools=[boom_tool()], recorder=recorder)
    await h.run("q")
    async with db_sessionmaker() as s:
        call = (await s.execute(select(ToolCall))).scalar_one()
        assert call.tool == "boom" and call.ok is False


async def test_recorder_failure_never_breaks_the_run() -> None:
    class ExplodingRecorder:
        async def on_run_start(self, **kw: Any) -> None:
            raise RuntimeError("db down")

        async def on_tool_call(self, **kw: Any) -> None:
            raise RuntimeError("db down")

        async def on_run_end(self, **kw: Any) -> None:
            raise RuntimeError("db down")

    class P:
        async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
            if kw.get("budget"):
                kw["budget"].charge(0.001)
            return _text("fine")

    h = Harness(_spec("a3"), provider=P(), workspace=WS, recorder=ExplodingRecorder())
    res = await h.run("q")
    assert res.stop_reason == "done" and res.output == "fine"


def test_migration_0002_idempotent_on_fresh_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh DBs get parent_run_id from 0001's metadata; 0002 must not fail on them."""
    import sqlite3

    from sportsdata_agents.config import get_settings

    db = tmp_path / "m.db"
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    get_settings.cache_clear()
    try:
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        con = sqlite3.connect(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(agent_runs)")}
        con.close()
    finally:
        get_settings.cache_clear()
    assert "parent_run_id" in cols