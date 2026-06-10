"""M0.13 — grounding verification: fabricated numbers caught, grounded answers pass."""

from __future__ import annotations

from typing import Any

import pytest

from sportsdata_agents.agents.grounding import ADVISORY_DISCLAIMER, extract_numbers, grounding_verifier
from sportsdata_agents.agents.harness import Harness
from sportsdata_agents.agents.runtime import AgentRuntime
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.models.gateway import ModelReply, ToolCallRequest
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


# ── number extraction ────────────────────────────────────────────────────


def test_extract_numbers_normalizes() -> None:
    nums = extract_numbers("He hit 42 HRs, avg .311, odds 2.50, salary $1,234,567 in 2025")
    assert {"42", "0.311", "2.5", "1234567", "2025"} <= nums


def test_single_digit_integers_skipped() -> None:
    assert extract_numbers("in 1 sentence, the top 5 teams") == set()
    assert "2.5" in extract_numbers("odds of 2.5")  # decimals always count


# ── the verifier ─────────────────────────────────────────────────────────


def test_fabricated_number_caught() -> None:
    ok, feedback = grounding_verifier(
        "Judge hit 62 home runs", ["which team is Judge on?", '{"team": "Yankees", "hr": 58}']
    )
    assert not ok
    assert "62" in feedback and "data is unavailable" in feedback


def test_grounded_numbers_pass() -> None:
    ok, _ = grounding_verifier(
        "Judge hit 58 home runs at odds of 2.50",
        ["question", '{"hr": 58, "price": 2.50}'],
    )
    assert ok


def test_echoed_user_number_passes() -> None:
    """A number the USER said (e.g. 'is 62 HRs good?') is not a fabrication."""
    ok, _ = grounding_verifier("62 home runs would be exceptional", ["is 62 HRs good?"])
    assert ok


def test_comma_and_decimal_normalization_matches() -> None:
    ok, _ = grounding_verifier("the crowd was 47,123", ["q", '{"attendance": 47123}'])
    assert ok
    ok, _ = grounding_verifier("priced at 2.5", ["q", '{"odds": 2.50}'])
    assert ok


def test_no_numbers_passes_trivially() -> None:
    ok, _ = grounding_verifier("The Yankees won comfortably.", ["who won?"])
    assert ok


def test_number_with_no_tool_evidence_fails() -> None:
    """An agent answering numerically without ANY supporting data is the core case."""
    ok, feedback = grounding_verifier("They scored 117 points", ["who won the game?"])
    assert not ok and "117" in feedback


# ── exit gate: through the harness ───────────────────────────────────────


def _spec(**over: Any) -> AgentSpec:
    base: dict[str, Any] = {"id": "g", "display_name": "g", "system_prompt": "x"}
    base.update(over)
    return AgentSpec.model_validate(base)


def _text(text: str) -> ModelReply:
    return ModelReply(text=text, model="fake", tokens_in=50, tokens_out=10, cost_usd=0.001)


def _tool_call(name: str) -> ModelReply:
    return ModelReply(
        text="", model="fake", tokens_in=50, tokens_out=10, cost_usd=0.001,
        tool_calls=(ToolCallRequest(id="c", name=name, arguments={}),),
    )


class ScriptedProvider:
    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if budget is not None:
            budget.charge(reply.cost_usd)
        return reply


from sportsdata_agents.agents.harness import ToolDef  # noqa: E402


def stats_tool() -> ToolDef:
    async def execute(args: dict[str, Any]) -> Any:
        return {"player": "Judge", "home_runs": 58}

    return ToolDef(name="stats", description="", parameters={"type": "object"}, execute=execute)


async def test_exit_gate_fabrication_caught_then_corrected() -> None:
    """M0.13 exit gate: a fabricated figure is caught by the grounding check; the
    corrected (grounded) answer then passes verified=True."""
    provider = ScriptedProvider(
        _tool_call("stats"),
        _text("Judge hit 62 home runs this season."),  # fabricated — tool said 58
        _text("Judge hit 58 home runs this season."),  # corrected after feedback
    )
    h = Harness(_spec(), provider=provider, workspace=WS, tools=[stats_tool()], verifier=grounding_verifier)
    res = await h.run("how many home runs does Judge have?")
    assert res.stop_reason == "done"
    assert res.verified is True
    assert "58" in res.output
    feedback = [m for m in res.messages if "appear in no tool result" in (m.get("content") or "")]
    assert feedback, "grounding feedback never reached the model"


async def test_exit_gate_persistent_fabrication_reported_unverified() -> None:
    provider = ScriptedProvider(
        _tool_call("stats"),
        _text("Judge hit 62 home runs."),
        _text("No really, 62 home runs."),  # doubles down
    )
    h = Harness(_spec(), provider=provider, workspace=WS, tools=[stats_tool()], verifier=grounding_verifier)
    res = await h.run("how many?")
    assert res.stop_reason == "done"
    assert res.verified is False  # surfaced honestly, not silently accepted


async def test_runtime_wires_grounding_by_default() -> None:
    """context.verify (the spec default) gets the grounding verifier without explicit wiring."""
    async with AgentRuntime(_spec(), provider=ScriptedProvider(_text("hi")), workspace=WS) as rt:
        assert rt.harness is not None
        assert rt.harness.verifier is grounding_verifier

    spec_off = _spec(context={"verify": False})
    async with AgentRuntime(spec_off, provider=ScriptedProvider(_text("hi")), workspace=WS) as rt:
        assert rt.harness is not None
        assert rt.harness.verifier is None


def test_disclaimer_constant_has_no_edge_language() -> None:
    """§14: informational framing, no profit/edge promises."""
    for banned in ("profit", "edge", "guarantee", "win"):
        assert banned not in ADVISORY_DISCLAIMER.lower()
    assert "informational" in ADVISORY_DISCLAIMER