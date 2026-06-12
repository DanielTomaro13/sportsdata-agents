"""The skill learning loop + the generalist catch-all (Hermes-style growth)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sportsdata_agents.agents.loader import load_builtin_specs
from sportsdata_agents.tools import skillsmith

pytestmark = pytest.mark.unit


@pytest.fixture()
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path))
    return tmp_path


async def test_create_list_recall_round_trip(data_dir: Path) -> None:
    out = await skillsmith.create_skill({
        "name": "xg_method", "description": "expected goals from shot data",
        "triggers": ["expected goals", "xg"], "body": "# xG\nSum shot probabilities.",
    })
    assert out["name"] == "xg_method"
    assert (data_dir / "skills" / "xg_method" / "SKILL.md").is_file()

    listed = {s["name"]: s["source"] for s in (await skillsmith.list_skills({}))["skills"]}
    assert listed["xg_method"] == "user"          # the learned one
    assert listed.get("vig_removal") == "builtin"  # built-ins still listed

    recalled = await skillsmith.recall_skill({"name": "xg_method"})
    assert recalled["body"].startswith("# xG") and recalled["description"]


async def test_builtin_skills_are_never_shadowed(data_dir: Path) -> None:
    with pytest.raises(ValueError, match="built-in skill"):
        await skillsmith.create_skill({
            "name": "vig_removal", "description": "x", "triggers": ["x"], "body": "y",
        })


@pytest.mark.parametrize("evil", ["../escape", "a/b", "x", "has space", "../../etc"])
async def test_skill_names_are_slug_validated(data_dir: Path, evil: str) -> None:
    with pytest.raises(ValueError, match="slug"):
        await skillsmith.create_skill({"name": evil, "description": "d", "triggers": ["t"], "body": "b"})
    assert not (data_dir / "skills" / evil).exists()


async def test_create_requires_all_parts(data_dir: Path) -> None:
    for missing in ({"name": "s", "triggers": ["t"], "body": "b"},        # no description
                    {"name": "s", "description": "d", "body": "b"},        # no triggers
                    {"name": "s", "description": "d", "triggers": ["t"]}):  # no body
        with pytest.raises(ValueError):
            await skillsmith.create_skill(missing)


async def test_recall_unknown_raises(data_dir: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no skill named"):
        await skillsmith.recall_skill({"name": "does-not-exist"})


def test_generalist_loads_is_pro_only_and_reachable() -> None:
    from sportsdata_agents.licensing.entitlements import entitlements_for_tier

    specs = load_builtin_specs()
    assert "generalist" in specs
    gen = specs["generalist"]
    assert gen.plane == "product" and gen.sandbox == "ephemeral"
    # the growth tools are granted
    for t in ("create_skill", "list_skills", "recall_skill", "save_agent_spec", "run_python"):
        assert t in gen.tools.native, f"generalist missing {t}"

    plus = entitlements_for_tier("plus")
    assert "generalist" not in (plus.agents or ())          # capability-creation is Pro-only
    assert entitlements_for_tier("pro").allows_agent("generalist")

    # the orchestrator routes here as the fallback
    assert "generalist" in specs["orchestrator"].can_delegate_to


def test_generalist_native_tools_all_resolve() -> None:
    """Every native tool the generalist grants must exist (else the runtime build
    raises). Mirror the runtime's resolution set."""
    from sportsdata_agents.tools.arbitrage import ARBITRAGE_TOOL_NAMES
    from sportsdata_agents.tools.builder import BUILDER_TOOL_NAMES
    from sportsdata_agents.tools.dictionary import DICTIONARY_TOOL_NAMES
    from sportsdata_agents.tools.memory import MEMORY_TOOL_NAMES
    from sportsdata_agents.tools.monitoring import MONITOR_TOOL_NAMES
    from sportsdata_agents.tools.quant import QUANT_TOOL_NAMES
    from sportsdata_agents.tools.registry import NATIVE_TOOLS
    from sportsdata_agents.tools.resolution import RESOLUTION_TOOL_NAMES
    from sportsdata_agents.tools.tracking import TRACKING_TOOL_NAMES

    known = (set(NATIVE_TOOLS) | TRACKING_TOOL_NAMES | MEMORY_TOOL_NAMES | QUANT_TOOL_NAMES
             | DICTIONARY_TOOL_NAMES | RESOLUTION_TOOL_NAMES | MONITOR_TOOL_NAMES
             | BUILDER_TOOL_NAMES | ARBITRAGE_TOOL_NAMES)
    gen = load_builtin_specs()["generalist"]
    assert set(gen.tools.native) <= known, f"unresolved: {set(gen.tools.native) - known}"
