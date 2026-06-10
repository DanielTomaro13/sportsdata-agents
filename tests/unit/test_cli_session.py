"""M0.12 — TeamSession, console progress recorder, and CLI rendering (all offline)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rich.console import Console

from sportsdata_agents.agents.harness import RunResult
from sportsdata_agents.agents.outputs import StatsAnswer
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.gateway.service import TeamSession, parsed_sources
from sportsdata_agents.interfaces.cli.__main__ import _render_result
from sportsdata_agents.interfaces.cli.progress import ConsoleProgressRecorder
from sportsdata_agents.models.gateway import ModelReply, UsageEvent
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


def _spec(id_: str, **over: Any) -> AgentSpec:
    base: dict[str, Any] = {"id": id_, "display_name": id_, "system_prompt": "x"}
    base.update(over)
    return AgentSpec.model_validate(base)


class ScriptedProvider:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        if budget is not None:
            budget.charge(0.001)
        return ModelReply(text=self.text, model="fake", tokens_in=10, tokens_out=5, cost_usd=0.001)


# ── TeamSession ──────────────────────────────────────────────────────────


async def test_session_runs_a_single_agent_without_mcp() -> None:
    """A no-tools agent needs no MCP subprocess — the channel seam works standalone."""
    specs = {"solo": _spec("solo")}
    session = TeamSession(specs=specs, workspace=WS, provider=ScriptedProvider("hi there"), agent_id="solo")
    async with session:
        res = await session.run("hello?")
    assert res.stop_reason == "done"
    assert res.output == "hi there"
    assert session.agent_name == "solo"


async def test_session_unknown_agent_fails_loudly() -> None:
    session = TeamSession(specs={"solo": _spec("solo")}, workspace=WS, provider=ScriptedProvider("x"), agent_id="ghost")
    with pytest.raises(KeyError, match="ghost"):
        async with session:
            pass


async def test_session_not_started_refuses_run() -> None:
    session = TeamSession(specs={"solo": _spec("solo")}, workspace=WS, provider=ScriptedProvider("x"), agent_id="solo")
    with pytest.raises(RuntimeError, match="not started"):
        await session.run("q")


# ── ConsoleProgressRecorder ──────────────────────────────────────────────


class RecordingInner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: list[UsageEvent] = []

    def usage_sink(self, event: UsageEvent) -> None:
        self.events.append(event)

    async def on_run_start(self, **kw: Any) -> None:
        self.calls.append("start")

    async def on_tool_call(self, **kw: Any) -> None:
        self.calls.append("tool")

    async def on_run_end(self, **kw: Any) -> None:
        self.calls.append("end")


async def test_progress_recorder_prints_and_forwards() -> None:
    console = Console(record=True, width=100)
    inner = RecordingInner()
    rec = ConsoleProgressRecorder(console, inner=inner)

    root, sub = uuid.uuid4(), uuid.uuid4()
    await rec.on_run_start(run_id=root, parent_run_id=None, agent="orch", task="t")  # root: silent
    await rec.on_run_start(run_id=sub, parent_run_id=root, agent="stats_specialist", task="who won?")
    await rec.on_tool_call(run_id=sub, tool="mlb_teams", arguments={}, ok=True, latency_ms=42)
    await rec.on_tool_call(run_id=sub, tool="boom", arguments={}, ok=False, latency_ms=7)
    await rec.on_run_end(run_id=sub, agent="stats_specialist", status="ok", cost_usd=0.01, latency_ms=100)

    out = console.export_text()
    assert "stats_specialist: who won?" in out  # delegation narrated
    assert "mlb_teams (42 ms)" in out
    assert "boom (7 ms)" in out
    assert "orch" not in out.replace("stats_specialist", "")  # root run not narrated
    assert inner.calls == ["start", "start", "tool", "tool", "end"]  # everything forwarded

    rec.usage_sink(
        UsageEvent(kind="llm", model="m", tier="fast", tokens_in=1, tokens_out=1,
                   cost_usd=0.0, latency_ms=1, tenant_id="t", workspace_id="w")
    )
    assert len(inner.events) == 1  # sink delegated


async def test_progress_recorder_without_inner_is_fine() -> None:
    rec = ConsoleProgressRecorder(Console(record=True), inner=None)
    await rec.on_run_start(run_id=uuid.uuid4(), parent_run_id=None, agent="a", task="t")
    await rec.on_run_end(run_id=uuid.uuid4(), agent="a", status="ok", cost_usd=0, latency_ms=1)
    rec.usage_sink(
        UsageEvent(kind="llm", model="m", tier="fast", tokens_in=1, tokens_out=1,
                   cost_usd=0.0, latency_ms=1, tenant_id="t", workspace_id="w")
    )  # no inner → no-op, no crash


# ── rendering ────────────────────────────────────────────────────────────


def _result(**over: Any) -> RunResult:
    base: dict[str, Any] = {
        "output": "raw text",
        "stop_reason": "done",
        "steps": 2,
        "tool_call_count": 3,
        "cost_usd": 0.0123,
    }
    base.update(over)
    return RunResult(**base)


def test_render_prefers_typed_answer_and_shows_sources() -> None:
    console = Console(record=True, width=100)
    parsed = StatsAnswer(answer="Judge plays for the Yankees", sources=["mlb_player"])
    _render_result(console, _result(parsed=parsed, verified=True))
    out = console.export_text()
    assert "Judge plays for the Yankees" in out
    assert "sources: mlb_player" in out
    assert "cost=$0.0123" in out and "verified=True" in out


def test_render_falls_back_to_raw_output() -> None:
    console = Console(record=True, width=100)
    _render_result(console, _result())
    out = console.export_text()
    assert "raw text" in out
    assert "stop=done" in out


def test_parsed_sources_helper() -> None:
    assert parsed_sources(_result(parsed=StatsAnswer(answer="a", sources=["x", "y"]))) == ["x", "y"]
    assert parsed_sources(_result()) == []


# ── provider detection (BYO-LLM, §8.1) ───────────────────────────────────


def test_detect_tier_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from sportsdata_agents.gateway.service import detect_tier_overrides

    for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert detect_tier_overrides() == {}  # no keys → policy defaults (which will then fail loudly)

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    overrides = detect_tier_overrides()
    assert set(overrides) == {"fast", "balanced", "strong"}
    assert all(m.startswith("openrouter/") for m in overrides.values())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # anthropic outranks → defaults suffice
    assert detect_tier_overrides() == {}
    assert "ANTHROPIC_API_KEY" in os.environ


def test_cli_help_lists_run_and_chat() -> None:
    from typer.testing import CliRunner

    from sportsdata_agents.interfaces.cli.__main__ import app

    out = CliRunner().invoke(app, ["--help"]).output
    assert "run" in out and "chat" in out


async def test_try_db_recorder_probes_connectivity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sessionmaker is lazy — without a real connect() probe, a down DB 'succeeds'
    here and then spams guarded-hook warnings on every run."""
    from sportsdata_agents.config import Settings, get_settings
    from sportsdata_agents.data.db import reset_engine
    from sportsdata_agents.data.repository import TenantScope
    from sportsdata_agents.gateway.service import try_db_recorder

    scope = TenantScope("t", "w")
    await reset_engine()
    try:
        # unreachable DB → None, not a recorder that fails later
        monkeypatch.setenv("SPORTSDATA_AGENTS_DATABASE_URL", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
        get_settings.cache_clear()
        assert await try_db_recorder(Settings(_env_file=None), scope) is None  # type: ignore[call-arg]

        # reachable (sqlite) → a real recorder
        await reset_engine()
        monkeypatch.setenv("SPORTSDATA_AGENTS_DATABASE_URL", "sqlite+aiosqlite://")
        get_settings.cache_clear()
        recorder = await try_db_recorder(Settings(_env_file=None), scope)  # type: ignore[call-arg]
        assert recorder is not None
    finally:
        await reset_engine()
        get_settings.cache_clear()
