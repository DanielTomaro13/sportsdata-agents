"""M0.7 — the harness: loop control, context policy, skills disclosure, verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sportsdata_agents.agents.harness import Harness, ToolDef, default_compactor
from sportsdata_agents.agents.skills import SkillSet, load_skillset, parse_skill_md
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest
from sportsdata_agents.workspace import Budgets, Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


def make_spec(**overrides: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "id": "test_agent",
        "display_name": "Test Agent",
        "system_prompt": "Answer questions.",
    }
    base.update(overrides)
    return AgentSpec.model_validate(base)


def text_reply(text: str, *, tokens_in: int = 100, cost: float = 0.01) -> ModelReply:
    return ModelReply(text=text, model="fake", tokens_in=tokens_in, tokens_out=10, cost_usd=cost)


def tool_reply(name: str, args: dict[str, Any] | None = None, *, tokens_in: int = 100) -> ModelReply:
    return ModelReply(
        text="",
        model="fake",
        tokens_in=tokens_in,
        tokens_out=10,
        cost_usd=0.01,
        tool_calls=(ToolCallRequest(id="c1", name=name, arguments=args or {}),),
    )


class ScriptedProvider:
    """Returns the scripted replies in order (repeating the last); charges the budget."""

    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        self.seen_messages.append([dict(m) for m in messages])
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if budget is not None:
            budget.charge(reply.cost_usd)
        return reply


def echo_tool(name: str = "echo") -> ToolDef:
    async def execute(args: dict[str, Any]) -> Any:
        return {"echo": args}

    return ToolDef(name=name, description="echoes", parameters={"type": "object"}, execute=execute)


# ── stop conditions ──────────────────────────────────────────────────────


async def test_done_on_final_answer() -> None:
    h = Harness(make_spec(), provider=ScriptedProvider(text_reply("final")), workspace=WS)
    res = await h.run("q")
    assert res.stop_reason == "done"
    assert res.output == "final"
    assert res.steps == 1 and res.cost_usd == pytest.approx(0.01)


async def test_max_steps_stops() -> None:
    spec = make_spec(limits={"max_steps": 2, "max_tool_calls": 50, "cost_ceiling_usd": 9.0})
    provider = ScriptedProvider(
        tool_reply("echo", {"a": 1}), tool_reply("echo", {"a": 2}), tool_reply("echo", {"a": 3})
    )
    h = Harness(spec, provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "max_steps"
    assert res.steps == 2


async def test_max_tool_calls_stops() -> None:
    spec = make_spec(limits={"max_tool_calls": 2, "max_steps": 50, "cost_ceiling_usd": 9.0})
    provider = ScriptedProvider(
        tool_reply("echo", {"a": 1}), tool_reply("echo", {"a": 2}), tool_reply("echo", {"a": 3})
    )
    h = Harness(spec, provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "max_tool_calls"
    assert res.tool_call_count == 2


async def test_budget_exhaustion_stops_before_next_call() -> None:
    spec = make_spec(limits={"cost_ceiling_usd": 0.015, "max_steps": 50, "max_tool_calls": 50})
    provider = ScriptedProvider(tool_reply("echo", {"a": 1}), tool_reply("echo", {"a": 2}), text_reply("never"))
    h = Harness(spec, provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "budget_exhausted"
    assert provider.calls == 2  # third model call was refused by the ceiling


async def test_timeout_stops() -> None:
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    spec = make_spec(limits={"timeout_seconds": 5, "max_steps": 50})
    provider = ScriptedProvider(tool_reply("echo", {"a": 1}), text_reply("never"))
    h = Harness(spec, provider=provider, workspace=WS, tools=[echo_tool()], now=lambda: next(clock))
    res = await h.run("q")
    assert res.stop_reason == "timeout"


async def test_no_progress_detector_stops_thrash() -> None:
    same = tool_reply("echo", {"a": 1})
    provider = ScriptedProvider(same, same, same, same)
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "no_progress"


async def test_no_progress_detector_catches_oscillation() -> None:
    """a,b,a,b,a,b — the classic two-tool thrash — must stop, not burn the ceilings."""
    a, b = tool_reply("echo", {"x": "a"}), tool_reply("echo", {"x": "b"})
    provider = ScriptedProvider(a, b, a, b, a, b, a, b)
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "no_progress"
    assert res.tool_call_count == 6  # period 2 x 3 repeats, not the 25-call ceiling


async def test_varied_calls_are_not_flagged_as_thrash() -> None:
    """Distinct work must never trip the detector."""
    replies = [tool_reply("echo", {"i": i}) for i in range(8)] + [text_reply("done")]
    provider = ScriptedProvider(*replies)
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "done"


def test_is_thrashing_unit() -> None:
    from sportsdata_agents.agents.harness import is_thrashing

    assert is_thrashing(["a", "a", "a"])
    assert is_thrashing(["x", "a", "b", "a", "b", "a", "b"])  # period 2 at the tail
    assert is_thrashing(["a", "b", "c"] * 3)  # period 3
    assert not is_thrashing(["a", "b", "a", "b"])  # only 2 repeats of the cycle
    assert not is_thrashing(["a", "b", "a", "c", "a", "b"])  # no clean cycle
    assert not is_thrashing([])


# ── §8.1 clamping ────────────────────────────────────────────────────────


def test_workspace_clamps_spec_limits() -> None:
    spec = make_spec(limits={"max_steps": 500, "cost_ceiling_usd": 100.0, "timeout_seconds": 10_000})
    ws = Workspace(budgets=Budgets(max_steps=40, per_run_usd=0.5, timeout_seconds=120))
    h = Harness(spec, provider=ScriptedProvider(text_reply("x")), workspace=ws)
    assert h.max_steps == 40
    assert h.cost_ceiling_usd == 0.5
    assert h.timeout_seconds == 120


# ── tool execution edge cases ────────────────────────────────────────────


async def test_unknown_tool_is_reported_to_model_not_raised() -> None:
    provider = ScriptedProvider(tool_reply("ghost"), text_reply("recovered"))
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "done"
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert "unknown tool" in tool_msgs[0]["content"]


async def test_denied_tool_request_is_refused_not_executed() -> None:
    executed = []

    async def execute(args: dict[str, Any]) -> Any:
        executed.append(args)
        return "x"

    # The model asks for a denied name that isn't even in the toolset.
    provider = ScriptedProvider(tool_reply("place_bet"), text_reply("ok"))
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert "no-money" in tool_msgs[0]["content"]
    assert executed == []


async def test_tool_exception_returned_as_error_content() -> None:
    async def boom(args: dict[str, Any]) -> Any:
        raise RuntimeError("kaput")

    tool = ToolDef(name="boom", description="", parameters={"type": "object"}, execute=boom)
    provider = ScriptedProvider(tool_reply("boom"), text_reply("recovered"))
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=[tool])
    res = await h.run("q")
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert "RuntimeError: kaput" in tool_msgs[0]["content"]
    assert res.stop_reason == "done"


def test_denied_tool_in_toolset_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="denied tool"):
        Harness(make_spec(), provider=ScriptedProvider(text_reply("x")), workspace=WS, tools=[echo_tool("place_bet")])


def test_duplicate_tool_names_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        Harness(
            make_spec(),
            provider=ScriptedProvider(text_reply("x")),
            workspace=WS,
            tools=[echo_tool("echo"), echo_tool("echo")],
        )


async def test_multi_tool_batch_keeps_protocol_order(tmp_path: Path) -> None:
    """assistant(tool_calls=[a,b]) must be followed by tool(a), tool(b) with NOTHING
    between — a skill triggered by result a must disclose only after the whole batch."""

    async def trigger_tool(args: dict[str, Any]) -> Any:
        return {"note": "remove the vig here"}  # triggers the skill

    tools = [
        ToolDef(name="t_a", description="", parameters={"type": "object"}, execute=trigger_tool),
        echo_tool("t_b"),
    ]
    batch = ModelReply(
        text="",
        model="fake",
        tokens_in=100,
        tokens_out=10,
        cost_usd=0.01,
        tool_calls=(
            ToolCallRequest(id="a", name="t_a", arguments={}),
            ToolCallRequest(id="b", name="t_b", arguments={}),
        ),
    )
    provider = ScriptedProvider(batch, text_reply("done"))
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=tools, skills=_skillset(tmp_path))
    res = await h.run("compare books")

    roles = [m["role"] for m in res.messages]
    i = roles.index("assistant")
    assert roles[i + 1] == "tool" and roles[i + 2] == "tool", f"batch interleaved: {roles}"
    # the disclosure exists, but only after both tool messages
    disclosure_idx = next(
        idx for idx, m in enumerate(res.messages) if "[skill loaded" in (m.get("content") or "")
    )
    assert disclosure_idx == i + 3


# ── context policy (§8.2) ────────────────────────────────────────────────


async def test_compaction_fires_past_threshold() -> None:
    compacted: list[int] = []

    def compactor(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compacted.append(len(messages))
        return default_compactor(messages, keep_last=2)

    spec = make_spec(limits={"max_tokens": 1000}, context={"long_run": "compact"})
    # tokens_in 800 >= 0.7 * 1000 → compaction after the reply
    provider = ScriptedProvider(tool_reply("echo", {"a": 1}, tokens_in=800), text_reply("done", tokens_in=100))
    h = Harness(spec, provider=provider, workspace=WS, tools=[echo_tool()], compactor=compactor)
    res = await h.run("q")
    assert compacted, "compactor never fired"
    assert res.stop_reason == "done"


async def test_reset_policy_stops_for_handoff() -> None:
    spec = make_spec(limits={"max_tokens": 1000}, context={"long_run": "reset"})
    provider = ScriptedProvider(tool_reply("echo", {"a": 1}, tokens_in=900))
    h = Harness(spec, provider=provider, workspace=WS, tools=[echo_tool()])
    res = await h.run("q")
    assert res.stop_reason == "context_exhausted"


def test_default_compactor_keeps_system_task_and_recent() -> None:
    msgs = [{"role": "system", "content": "s"}] + [{"role": "user", "content": str(i)} for i in range(10)]
    out = default_compactor(msgs, keep_last=3)
    assert out[0]["role"] == "system"
    assert out[1]["content"] == "0"  # the ORIGINAL TASK survives compaction (observed live: losing
    # it made the model burn budget guessing what was asked)
    assert "compacted" in out[2]["content"]
    assert [m["content"] for m in out[-3:]] == ["7", "8", "9"]


def test_default_compactor_drops_orphaned_tool_heads() -> None:
    """The keep_last slice must not leave the tail starting with unpaired tool results."""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "r1"},
        {"role": "tool", "tool_call_id": "b", "content": "r2"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "next"},
    ]
    # keep_last=3 slices starting at a tool message → orphans must be dropped
    out = default_compactor(msgs, keep_last=3)
    kept_roles = [m["role"] for m in out[3:]]  # after system + task + marker
    assert kept_roles and kept_roles[0] != "tool", f"orphaned tool head survived: {kept_roles}"


# ── verification (§13.1 hook) ────────────────────────────────────────────


async def test_verifier_failure_feeds_back_then_accepts() -> None:
    verdicts = iter([(False, "missing source"), (True, "")])
    provider = ScriptedProvider(text_reply("draft"), text_reply("final with sources"))
    h = Harness(
        make_spec(),
        provider=provider,
        workspace=WS,
        verifier=lambda text, evidence: next(verdicts),
    )
    res = await h.run("q")
    assert res.stop_reason == "done"
    assert res.verified is True
    assert res.output == "final with sources"
    # the feedback message reached the model
    assert any("failed verification" in (m.get("content") or "") for m in res.messages if m["role"] == "user")


async def test_verifier_exhausts_retries_and_reports_unverified() -> None:
    provider = ScriptedProvider(text_reply("draft"), text_reply("still bad"))
    h = Harness(make_spec(), provider=provider, workspace=WS, verifier=lambda text, evidence: (False, "nope"))
    res = await h.run("q")
    assert res.stop_reason == "done"
    assert res.verified is False


# ── skills: progressive disclosure (§8.2) ────────────────────────────────


SKILL_MD = """---
name: vig_removal
description: Remove the bookmaker margin to estimate fair probabilities.
triggers: [vig, fair price, overround]
---
To remove the vig: normalise implied probabilities to sum to 1.
"""


def _skillset(tmp_path: Path) -> SkillSet:
    d = tmp_path / "vig_removal"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return load_skillset(["vig_removal"], root=tmp_path)


def test_skill_parses_and_matches() -> None:
    skill = parse_skill_md(SKILL_MD)
    assert skill.name == "vig_removal"
    assert skill.matches("what's the FAIR PRICE here?")
    assert not skill.matches("who won the game")


def test_skill_triggers_respect_word_boundaries() -> None:
    """'vig' must not fire inside 'navigation'."""
    skill = parse_skill_md(SKILL_MD)
    assert not skill.matches("open the navigation menu")
    assert skill.matches("the vig on this market is 5%")


async def test_skill_disclosed_jit_on_trigger_and_only_once(tmp_path: Path) -> None:
    provider = ScriptedProvider(text_reply("answer"))
    h = Harness(make_spec(), provider=provider, workspace=WS, skills=_skillset(tmp_path))
    res = await h.run("remove the vig from these odds, then the vig again")

    sys_msg = res.messages[0]["content"]
    assert "Skills available" in sys_msg and "vig_removal:" in sys_msg  # index up front
    disclosures = [m for m in res.messages if "[skill loaded: vig_removal]" in (m.get("content") or "")]
    assert len(disclosures) == 1  # body disclosed exactly once
    assert "normalise implied probabilities" in disclosures[0]["content"]


async def test_skill_not_disclosed_without_trigger(tmp_path: Path) -> None:
    provider = ScriptedProvider(text_reply("answer"))
    h = Harness(make_spec(), provider=provider, workspace=WS, skills=_skillset(tmp_path))
    res = await h.run("who won the cricket?")
    assert not any("[skill loaded" in (m.get("content") or "") for m in res.messages)


async def test_skill_triggered_by_tool_result(tmp_path: Path) -> None:
    async def execute(args: dict[str, Any]) -> Any:
        return {"note": "the overround here is 105%"}

    tool = ToolDef(name="markets", description="", parameters={"type": "object"}, execute=execute)
    provider = ScriptedProvider(tool_reply("markets"), text_reply("answer"))
    h = Harness(make_spec(), provider=provider, workspace=WS, tools=[tool], skills=_skillset(tmp_path))
    res = await h.run("compare these books")  # no trigger in the prompt itself
    assert any("[skill loaded: vig_removal]" in (m.get("content") or "") for m in res.messages)


# ── per-run recorder isolation (the gateway's SSE mirror must not race) ──


class _ListRecorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def on_run_start(self, **kw: Any) -> None:
        self.events.append(f"start:{kw['task']}")

    async def on_tool_call(self, **kw: Any) -> None:
        self.events.append(f"tool:{kw['tool']}")

    async def on_run_end(self, **kw: Any) -> None:
        self.events.append(f"end:{kw['status']}")


class _ToolThenDoneProvider:
    """Per-RUN scripting (a shared call counter would interleave across concurrent
    runs): tool call first, then 'done' once this run's messages show a tool result."""

    async def complete(self, messages, *, budget=None, **kw):  # type: ignore[no-untyped-def]
        if budget is not None:
            budget.charge(0.01)
        if any(m.get("role") == "tool" for m in messages):
            return text_reply("done")
        return tool_reply("echo")


async def test_concurrent_runs_keep_their_own_recorders() -> None:
    """Two simultaneous runs on ONE shared harness, each with a per-run recorder:
    every event lands with its own run's recorder (contextvar isolation), none with
    the harness default. This is the gateway's warm-session concurrency contract."""
    import asyncio

    base = _ListRecorder()  # the harness-default recorder (e.g. the DB recorder)
    h = Harness(
        make_spec(),
        provider=_ToolThenDoneProvider(),
        workspace=WS,
        tools=[echo_tool()],
        recorder=base,
    )
    rec_a, rec_b = _ListRecorder(), _ListRecorder()
    res_a, res_b = await asyncio.gather(
        h.run("task-A", recorder=rec_a),
        h.run("task-B", recorder=rec_b),
    )
    assert res_a.stop_reason == "done" and res_b.stop_reason == "done"
    assert rec_a.events == ["start:task-A", "tool:echo", "end:ok"]
    assert rec_b.events == ["start:task-B", "tool:echo", "end:ok"]
    assert base.events == []  # overridden runs never leak into the default recorder


def test_spec_limit_clamping_is_logged_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    """§8.1 clamping is correct but must be visible: a spec declaring 600s on a 300s
    workspace runs at 300 — and says so in the log."""
    spec = make_spec(limits={"timeout_seconds": 600, "cost_ceiling_usd": 1.00})
    with caplog.at_level("INFO", logger="sportsdata_agents.agents.harness"):
        h = Harness(spec, provider=ScriptedProvider(text_reply("x")), workspace=WS)
    assert h.timeout_seconds == 300 and h.cost_ceiling_usd == 0.50  # defaults clamp
    log = "\n".join(r.message for r in caplog.records)
    assert "clamps spec limits" in log
    assert "timeout_seconds 600→300" in log and "cost_ceiling_usd 1.0→0.5" in log


async def test_run_without_override_still_uses_default_recorder() -> None:
    base = _ListRecorder()
    h = Harness(
        make_spec(),
        provider=ScriptedProvider(text_reply("final")),
        workspace=WS,
        recorder=base,
    )
    await h.run("plain")
    assert base.events == ["start:plain", "end:ok"]


async def test_artifacts_collected_from_tool_payloads() -> None:
    """run_python's contract: a dict payload with `artifacts` paths — the harness
    harvests them onto RunResult so channels (Slack upload, CLI) can deliver."""

    async def chart_tool(args: dict[str, Any]) -> Any:
        return {"ok": True, "stdout": "done", "artifacts": ["artifacts/x-chart.png"]}

    tool = ToolDef(name="charty", description="x", parameters={"type": "object"}, execute=chart_tool)
    h = Harness(
        make_spec(),
        provider=ScriptedProvider(tool_reply("charty"), text_reply("chart saved")),
        workspace=WS,
        tools=[tool],
    )
    res = await h.run("make a chart")
    assert res.artifacts == ["artifacts/x-chart.png"]
