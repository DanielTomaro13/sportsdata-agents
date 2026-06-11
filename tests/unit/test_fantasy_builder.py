"""M3.3 — fantasy lineup optimisation (deterministic), agent-builder flow, the
capability label map, and the Discord routing core."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from sportsdata_agents.quant.lineup import optimize_lineup
from sportsdata_agents.tools.builder import builder_tools, capability_labels

pytestmark = pytest.mark.unit


# ── lineup optimiser ───────────────────────────────────────────────────────


PLAYERS = [
    {"name": "A", "position": "PG", "salary": 30, "projection": 50},
    {"name": "B", "position": "PG", "salary": 20, "projection": 40},
    {"name": "C", "position": "C", "salary": 50, "projection": 60},
    {"name": "D", "position": "C", "salary": 40, "projection": 55},
    {"name": "E", "positions": ["PG", "C"], "salary": 25, "projection": 45},
    {"name": "F", "position": "SG", "salary": 15, "projection": 20},
]


def _exhaustive_best(players: list[dict], slots: list[str], cap: float) -> float:
    """Brute force every assignment — the oracle the beam must match on small pools."""
    def fits(p: dict, slot: str) -> bool:
        positions = set(p.get("positions") or [p.get("position")])
        return slot == "UTIL" or slot in positions

    best = 0.0
    for combo in itertools.permutations(players, len(slots)):
        if len({p["name"] for p in combo}) != len(slots):
            continue
        if not all(fits(p, s) for p, s in zip(combo, slots, strict=True)):
            continue
        if sum(p["salary"] for p in combo) > cap:
            continue
        best = max(best, sum(p["projection"] for p in combo))
    return best


def test_optimizer_matches_exhaustive_oracle() -> None:
    slots = ["PG", "C", "UTIL"]
    out = optimize_lineup(PLAYERS, slots, 100)
    assert out["projected_points"] == _exhaustive_best(PLAYERS, slots, 100) == 150.0
    assert out["salary"] <= 100
    assert [p["slot"] for p in out["lineup"]] == slots  # slot order preserved


def test_optimizer_locks_and_exclusions() -> None:
    out = optimize_lineup(PLAYERS, ["PG", "C"], 100, locked=["B"])
    names = {p["name"] for p in out["lineup"]}
    assert "B" in names
    out = optimize_lineup(PLAYERS, ["PG", "C"], 100, excluded=["C", "D"])
    assert {p["name"] for p in out["lineup"]} == {"A", "E"}  # E covers C
    with pytest.raises(ValueError, match="locked players not in the pool"):
        optimize_lineup(PLAYERS, ["PG"], 100, locked=["Nobody"])
    with pytest.raises(ValueError, match="no affordable player"):
        optimize_lineup(PLAYERS, ["PG", "C"], 10)


# ── agent builder ──────────────────────────────────────────────────────────


def test_capability_labels_cover_the_headline_tags() -> None:
    labels = capability_labels()
    assert labels["sport.prices"]["label"] == "Live odds"
    assert all("label" in v and "description" in v for v in labels.values())
    assert len(labels) >= 40  # generated from the MCP catalogue


async def test_builder_drafts_validates_and_saves_versioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §7.1 exit-gate core: a goal becomes a validated spec; saving twice
    requires a version bump and archives the old version (D27)."""
    monkeypatch.setenv("SPORTSDATA_AGENTS_USER_SPECS_DIR", str(tmp_path))
    tools = {t.name: t for t in builder_tools()}

    catalogue = await tools["list_capabilities"].execute({})
    assert any(d["tag"] == "sport.prices" for d in catalogue["data"])
    assert any(s["id"] == "dfs_lineup_building" for s in catalogue["skills"])

    draft = await tools["draft_agent_spec"].execute({"spec": {
        "id": "totals_watcher", "display_name": "Totals Watcher",
        "goal_prompt": "Watch AFL totals lines and summarise big moves when asked.",
        "capabilities": ["sport.prices"], "tier": "fast",
    }})
    assert draft["ok"] is True and draft["summary"]["id"] == "totals_watcher"

    saved = await tools["save_agent_spec"].execute({"yaml": draft["yaml"]})
    assert saved["version"] == "0.1.0" and Path(saved["saved"]).is_file()

    # same version again: refused; bumped version: archives the old file
    with pytest.raises(ValueError, match="bump the version"):
        await tools["save_agent_spec"].execute({"yaml": draft["yaml"]})
    bumped = await tools["draft_agent_spec"].execute({"spec": {
        "id": "totals_watcher", "display_name": "Totals Watcher",
        "goal_prompt": "Watch AFL and NRL totals lines.", "version": "0.2.0",
        "capabilities": ["sport.prices"], "tier": "fast",
    }})
    await tools["save_agent_spec"].execute({"yaml": bumped["yaml"]})
    assert (tmp_path / "totals_watcher@0.1.0.yaml").is_file()  # D27 archive

    # the saved agent is loadable through the normal loader + catalog
    from sportsdata_agents.agents.loader import load_spec_catalog, load_specs_dir

    assert load_specs_dir(tmp_path)["totals_watcher"].version == "0.2.0"
    assert sorted(load_spec_catalog(tmp_path)["totals_watcher"]) == ["0.1.0", "0.2.0"]


async def test_builder_guardrails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSDATA_AGENTS_USER_SPECS_DIR", str(tmp_path))
    tools = {t.name: t for t in builder_tools()}
    # builtin id collision refused at draft time
    draft = await tools["draft_agent_spec"].execute({"spec": {
        "id": "value_scout", "display_name": "X", "goal_prompt": "y",
    }})
    assert draft["ok"] is False and "collides" in draft["problems"][0]
    # ops-plane specs cannot be saved through the builder
    ops_yaml = """
spec_version: 1
agent:
  id: sneaky
  display_name: X
  plane: ops
  system_prompt: y
"""
    with pytest.raises(ValueError, match="product-plane only"):
        await tools["save_agent_spec"].execute({"yaml": ops_yaml})
    # money-ish capability refused by the spec models themselves
    draft = await tools["draft_agent_spec"].execute({"spec": {
        "id": "bad", "display_name": "X", "goal_prompt": "y",
        "native": ["place_bet"],
    }})
    assert draft["ok"] is False


# ── discord routing core ───────────────────────────────────────────────────


def test_discord_mention_stripping_and_clipping() -> None:
    from sportsdata_agents.interfaces.discord.app import clip, strip_bot_mention

    assert strip_bot_mention("<@123> who won?", 123) == "who won?"
    assert strip_bot_mention("<@!123>  who won?", 123) == "who won?"
    assert strip_bot_mention("plain dm", 123) == "plain dm"
    assert clip("x" * 3000).endswith("…") and len(clip("x" * 3000)) <= 1901


async def test_discord_handle_message_routes_to_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from sportsdata_agents.interfaces.discord import app as discord_app

    async def fake_gateway(text: str, *, channel_key: str) -> dict[str, Any]:
        assert text == "who won the afl?" and channel_key == "555"
        return {"answer": "Bulldogs by 2.", "sources": ["afl_api"], "verified": True,
                "cost_usd": 0.01}

    monkeypatch.setattr(discord_app, "ask_gateway", fake_gateway)
    reply = await discord_app.handle_message("<@9> who won the afl?", channel_key="555", bot_id=9)
    assert reply is not None and "Bulldogs by 2." in reply and "grounded" in reply
    assert await discord_app.handle_message("<@9>", channel_key="555", bot_id=9) is None


async def test_calibration_curve_bins_reliability() -> None:
    from sportsdata_agents.tools.registry import NATIVE_TOOLS

    pairs = ([{"prob": 0.8, "outcome": 1}] * 8 + [{"prob": 0.8, "outcome": 0}] * 2
             + [{"prob": 0.2, "outcome": 0}] * 9 + [{"prob": 0.2, "outcome": 1}])
    out = await NATIVE_TOOLS["calibration_curve"].execute({"pairs": pairs, "bins": 5})
    by_bin = {b["bin"]: b for b in out["bins"]}
    assert by_bin["0.80-1.00"]["observed_frequency"] == 0.8  # perfectly calibrated bin
    assert by_bin["0.20-0.40"]["observed_frequency"] == 0.1
    assert out["n"] == 20 and 0 < out["brier"] < 0.25
