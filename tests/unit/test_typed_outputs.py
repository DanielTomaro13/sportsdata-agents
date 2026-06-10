"""M0.9 — typed outputs: registry, parsing, harness enforcement, lint."""

from __future__ import annotations

from typing import Any

import pytest

from sportsdata_agents.agents.harness import Harness
from sportsdata_agents.agents.loader import lint_specs, load_builtin_specs
from sportsdata_agents.agents.outputs import (
    OddsComparison,
    StatsAnswer,
    extract_json,
    get_output_type,
    parse_output,
)
from sportsdata_agents.agents.spec import AgentSpec
from sportsdata_agents.models.gateway import ModelReply
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")

STATS_JSON = (
    '{"answer": "Judge plays for the Yankees", '
    '"facts": [{"claim": "current team", "value": "New York Yankees", "source": "mlb_player"}], '
    '"sources": ["mlb_player"]}'
)


def _spec(**over: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "id": "typed",
        "display_name": "Typed",
        "system_prompt": "Answer.",
        "output_type": "StatsAnswer",
    }
    base.update(over)
    return AgentSpec.model_validate(base)


def _text(text: str) -> ModelReply:
    return ModelReply(text=text, model="fake", tokens_in=50, tokens_out=10, cost_usd=0.001)


class ScriptedProvider:
    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, *, tier="balanced", workspace, budget=None, **kw):  # type: ignore[no-untyped-def]
        self.seen = messages
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if budget is not None:
            budget.charge(reply.cost_usd)
        return reply


# ── registry + parsing ───────────────────────────────────────────────────


def test_registry_resolves_and_fails_loudly() -> None:
    assert get_output_type("OddsComparison") is OddsComparison
    with pytest.raises(KeyError, match="GhostType"):
        get_output_type("GhostType")


@pytest.mark.parametrize(
    "text",
    [
        STATS_JSON,  # bare JSON
        f"```json\n{STATS_JSON}\n```",  # fenced
        f"Here you go:\n{STATS_JSON}\nHope that helps!",  # surrounded by prose
    ],
)
def test_parse_output_handles_real_model_formats(text: str) -> None:
    parsed, err = parse_output(text, StatsAnswer)
    assert err == "" and isinstance(parsed, StatsAnswer)
    assert parsed.facts[0].source == "mlb_player"


def test_parse_output_reports_schema_errors() -> None:
    parsed, err = parse_output('{"answer": 42}', StatsAnswer)  # wrong type
    assert parsed is None and "answer" in err


def test_extract_json_no_object_returns_text() -> None:
    assert extract_json("no json here") == "no json here"


# ── harness enforcement ──────────────────────────────────────────────────


async def test_harness_parses_typed_output() -> None:
    provider = ScriptedProvider(_text(f"```json\n{STATS_JSON}\n```"))
    res = await Harness(_spec(), provider=provider, workspace=WS).run("q")
    assert res.stop_reason == "done"
    assert isinstance(res.parsed, StatsAnswer)
    assert res.parsed.answer.startswith("Judge")
    # the schema instructions reached the system prompt
    assert "matching this schema" in provider.seen[0]["content"]


async def test_harness_feeds_back_format_error_then_parses() -> None:
    provider = ScriptedProvider(_text("not json at all"), _text(STATS_JSON))
    res = await Harness(_spec(), provider=provider, workspace=WS).run("q")
    assert res.stop_reason == "done"
    assert isinstance(res.parsed, StatsAnswer)
    assert provider.calls == 2
    feedback = [m for m in res.messages if "[format]" in (m.get("content") or "")]
    assert feedback, "format feedback never reached the model"


async def test_harness_gives_up_after_retry_with_parsed_none() -> None:
    provider = ScriptedProvider(_text("junk"), _text("still junk"))
    res = await Harness(_spec(), provider=provider, workspace=WS).run("q")
    assert res.stop_reason == "done"
    assert res.parsed is None
    assert res.output == "still junk"  # the text is still surfaced honestly


async def test_format_feedback_is_truncated() -> None:
    """Pydantic errors echo the invalid input — a long junk answer must not be pasted
    back into the window wholesale (§8.2 context hygiene)."""
    junk = "x" * 5000
    provider = ScriptedProvider(_text(junk), _text(STATS_JSON))
    res = await Harness(_spec(), provider=provider, workspace=WS).run("q")
    feedback = next(m for m in res.messages if "[format]" in (m.get("content") or ""))
    assert len(feedback["content"]) < 1000


async def test_delegate_summary_carries_structured_typed_output() -> None:
    """A sub-agent's typed output must reach the caller as structure, not a
    double-encoded string."""
    import json as _json

    from sportsdata_agents.agents.runtime import AgentRuntime, delegate_tool

    sub_provider = ScriptedProvider(_text(STATS_JSON))
    async with AgentRuntime(
        _spec(id="stats_sub"), provider=sub_provider, workspace=WS
    ) as sub:
        tool = delegate_tool(sub)
        out = _json.loads(await tool.execute({"task": "who does Judge play for?"}))
    assert out["agent"] == "stats_sub"
    assert out["data"]["answer"].startswith("Judge")
    assert out["data"]["facts"][0]["source"] == "mlb_player"


def test_harness_unknown_output_type_fails_at_construction() -> None:
    spec = _spec()
    object.__setattr__(spec, "output_type", "GhostType")  # bypass spec validation deliberately
    with pytest.raises(KeyError, match="GhostType"):
        Harness(spec, provider=ScriptedProvider(_text("x")), workspace=WS)


# ── specs + lint integration ─────────────────────────────────────────────


def test_builtin_specialists_declare_typed_outputs() -> None:
    specs = load_builtin_specs()
    assert specs["odds_specialist"].output_type == "OddsComparison"
    assert specs["stats_specialist"].output_type == "StatsAnswer"
    assert specs["orchestrator"].output_type is None  # free-text synthesis
    assert lint_specs(specs) == []


def test_lint_flags_unregistered_output_type() -> None:
    specs = {"a": _spec(id="a", output_type=None)}
    object.__setattr__(specs["a"], "output_type", "GhostType")
    problems = lint_specs(specs)
    assert any("GhostType" in p for p in problems)
