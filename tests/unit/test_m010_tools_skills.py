"""M0.10 — new native tools (golden values) + the builtin skill bundles end-to-end."""

from __future__ import annotations

from typing import Any

import pytest

from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.agents.runtime import AgentRuntime
from sportsdata_agents.agents.skills import builtin_skills_dir, load_skillset
from sportsdata_agents.mcp.manager import is_denied
from sportsdata_agents.models.gateway import ModelReply
from sportsdata_agents.tools.registry import NATIVE_TOOLS
from sportsdata_agents.workspace import Workspace

pytestmark = pytest.mark.unit

WS = Workspace(tenant_id="t", workspace_id="w")


# ── new native tools: golden values ──────────────────────────────────────


async def test_expected_value_golden() -> None:
    out = await NATIVE_TOOLS["expected_value"].execute({"probability": 0.5, "odds": 2.10})
    assert out["expected_value"] == pytest.approx(0.05)
    assert out["is_value"] is True
    out = await NATIVE_TOOLS["expected_value"].execute({"probability": 0.5, "odds": 1.90})
    assert out["expected_value"] == pytest.approx(-0.05)
    assert out["is_value"] is False


async def test_kelly_fraction_golden() -> None:
    # p=0.55 at evens: f = (b*p - q)/b = (0.55 - 0.45)/1 = 0.10
    out = await NATIVE_TOOLS["kelly_fraction"].execute({"probability": 0.55, "odds": 2.0})
    assert out["kelly_fraction"] == pytest.approx(0.10)
    # negative-edge prices clamp to 0 — never a negative suggestion
    out = await NATIVE_TOOLS["kelly_fraction"].execute({"probability": 0.40, "odds": 2.0})
    assert out["kelly_fraction"] == 0.0


@pytest.mark.parametrize("tool", ["expected_value", "kelly_fraction"])
async def test_probability_bounds_enforced(tool: str) -> None:
    with pytest.raises(ValueError, match="probability"):
        await NATIVE_TOOLS[tool].execute({"probability": 1.5, "odds": 2.0})


def test_kelly_name_dodges_the_deny_filter_deliberately() -> None:
    """The naming matters: 'kelly_stake' would (rightly) be denied; kelly_fraction is
    informational and passes."""
    assert is_denied("kelly_stake")
    assert not is_denied("kelly_fraction")


# ── builtin skill bundles ────────────────────────────────────────────────


def test_builtin_skills_load_and_trigger() -> None:
    skills = load_skillset(["vig_removal", "compare_odds"])  # default root = packaged skills/
    assert len(skills) == 2
    assert (builtin_skills_dir() / "vig_removal" / "SKILL.md").is_file()

    hits = skills.newly_triggered("what's the fair price on this market?")
    assert [s.name for s in hits] == ["vig_removal"]
    hits = skills.newly_triggered("find me the best odds across bookmakers")
    assert [s.name for s in hits] == ["compare_odds"]


def test_builtin_skill_triggers_avoid_false_positives() -> None:
    skills = load_skillset(["vig_removal", "compare_odds"])
    assert skills.newly_triggered("open the navigation menu") == []  # not "vig"
    assert skills.newly_triggered("what was the winning margin?") == []  # no "margin" trigger


async def test_odds_specialist_runs_with_builtin_skills_end_to_end() -> None:
    """Exit gate: the odds specialist's skills disclose JIT in a real harness run
    (scripted model; no MCP needed — native tools only for this check)."""
    spec = load_builtin_specs()["odds_specialist"].model_copy(
        update={"tools": type(load_builtin_specs()["odds_specialist"].tools)(native=["vig_removal", "best_price"])}
    )

    class P:
        async def complete(self, messages: Any, **kw: Any) -> ModelReply:
            self.messages = messages
            if kw.get("budget"):
                kw["budget"].charge(0.001)
            return ModelReply(text='{"selection":"x","quotes":[],"best":{"book":"tab","odds":2.0},"sources":[]}',
                              model="fake", tokens_in=50, tokens_out=10, cost_usd=0.001)

    async with AgentRuntime(spec, provider=P(), workspace=WS) as rt:
        res = await rt.run("remove the vig and find the best price across books")

    sys_msg = res.messages[0]["content"]
    assert "vig_removal:" in sys_msg and "compare_odds:" in sys_msg  # index up front
    disclosed = [m for m in res.messages if "[skill loaded" in (m.get("content") or "")]
    assert {d["content"].split("]")[0].split(": ")[1] for d in disclosed} == {"vig_removal", "compare_odds"}
    assert res.parsed is not None  # typed output still parses alongside skills
