"""ESPN: the adapter seam, the platform-specific verifier rules, and the write gate.

ESPN is the second platform, and its job in this test file is to prove the plane is
actually platform-agnostic rather than FPL with a coat of paint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sportsdata_agents.fantasy.adapters import adapter_for
from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy
from sportsdata_agents.fantasy.verify import verify_lineup, verify_transfers
from sportsdata_agents.tools import espn_fantasy as ef

LEAGUE, SEASON, GAME, TEAM = 899098157, 2026, "ffl", 4
CTX = {"leagueId": LEAGUE, "seasonId": SEASON, "game": GAME}
NOW = datetime.now(tz=UTC)


# ─── the write gate, structurally ───────────────────────────────────────


def test_the_espn_agent_is_never_granted_the_raw_write_group():
    import yaml

    import sportsdata_agents

    spec = yaml.safe_load(
        (Path(sportsdata_agents.__file__).parent / "specs" / "espn_manager.yaml").read_text())
    tools = spec["agent"]["tools"]
    assert not any(g.endswith(".write") for g in tools["mcp_groups"])
    assert "sport.transactions" in spec["agent"]["forbidden_capabilities"]
    assert set(tools["native"]) >= {"espn_propose_lineup", "espn_propose_add_drop"}


def test_no_espn_propose_tool_accepts_a_scoring_period_or_a_budget():
    """Both are read from ESPN. In the schema, they would be model-supplied — and a
    lineup set against the wrong week is accepted and does nothing."""
    for name in ("espn_propose_lineup", "espn_propose_add_drop"):
        props = ef.ESPN_FANTASY_TOOLS[name].parameters["properties"]
        assert "scoringPeriodId" not in props
        assert "waiverBudget" not in props


# ─── the adapter seam ───────────────────────────────────────────────────


def test_the_espn_adapter_routes_to_espn_tools():
    a = adapter_for("espn")
    assert a.lineup_call(TEAM, {"items": []}, CTX)[0] == "espnfantasy_set_lineup"
    assert a.roster_call(TEAM, {"items": []}, CTX)[0] == "espnfantasy_add_drop"
    assert a.read_squad_call(TEAM, CTX)[0] == "espnfantasy_rosters"


def test_the_fpl_adapter_is_untouched_by_any_of_this():
    a = adapter_for("fpl")
    assert a.lineup_call(1, {"picks": []}, {"csrf": "x"})[0] == "fpl_set_lineup"
    assert a.read_squad_call(1, {})[0] == "fpl_my_team"


def test_espn_refuses_to_build_a_call_without_league_identity():
    """A mistyped or missing league id writes to a stranger's team. Refusing with a
    sentence beats a KeyError, and both beat guessing."""
    with pytest.raises(ValueError, match="leagueId"):
        adapter_for("espn").lineup_call(TEAM, {}, {"game": "ffl"})


def test_our_roster_is_picked_out_of_a_league_wide_response():
    """ESPN returns EVERY team. Reading the wrong one would verify a stranger's roster
    against our intent and call it a match."""
    body = {"teams": [
        {"id": 3, "roster": {"entries": [{"playerId": 111, "lineupSlotId": 0}]}},
        {"id": TEAM, "roster": {"entries": [
            {"playerId": 222, "lineupSlotId": 2}, {"playerId": 333, "lineupSlotId": 20}]}},
    ]}
    picks = adapter_for("espn").picks_from(body, {**CTX, "teamId": TEAM})
    assert [p["element"] for p in picks] == [222, 333]
    assert [p["position"] for p in picks] == [2, 20]


def test_a_team_absent_from_the_response_yields_nothing():
    """Which the verifier reports as a mismatch — the safe direction."""
    body = {"teams": [{"id": 9, "roster": {"entries": [{"playerId": 1, "lineupSlotId": 0}]}}]}
    assert adapter_for("espn").picks_from(body, {**CTX, "teamId": TEAM}) == []


# ─── verifier rules that are NOT universal ──────────────────────────────


def espn_roster(*pairs):
    return [{"element": pid, "position": slot, "is_captain": False,
             "is_vice_captain": False, "multiplier": 1} for pid, slot in pairs]


def test_espn_is_not_asked_for_a_captain_it_does_not_have():
    """FPL's captain rule on ESPN would report a failure on every correct write, which
    is how a verifier teaches people to ignore it."""
    want = espn_roster((222, 2), (333, 20))
    assert verify_lineup(want, want, platform="espn").ok
    # …and FPL still demands one.
    assert not verify_lineup(want, want, platform="fpl").ok


def test_espn_lineup_mismatch_is_still_caught():
    want = espn_roster((222, 2), (333, 20))
    got = espn_roster((222, 20), (333, 20))      # never left the bench
    r = verify_lineup(want, got, platform="espn")
    assert not r.ok
    assert any("position" in m for m in r.mismatches)


def test_espn_add_and_drop_items_are_understood_by_the_shared_verifier():
    before = [{"element": 1}, {"element": 2}]
    after = [{"element": 1}, {"element": 3}]
    items = [{"playerId": 3, "type": "ADD"}, {"playerId": 2, "type": "DROP"}]
    assert verify_transfers(items, before, after, platform="espn").ok


def test_an_espn_add_that_did_not_land_is_caught():
    before = after = [{"element": 1}, {"element": 2}]
    items = [{"playerId": 3, "type": "ADD"}]
    r = verify_transfers(items, before, after, platform="espn")
    assert not r.ok


def test_an_unpaired_espn_add_is_allowed_to_change_the_roster_size():
    """FPL swaps are one-for-one; an ESPN roster with a spare slot takes an ADD alone.
    Applying FPL's size rule would fail a perfectly good claim."""
    before = [{"element": 1}]
    after = [{"element": 1}, {"element": 3}]
    r = verify_transfers([{"playerId": 3, "type": "ADD"}], before, after, platform="espn")
    assert r.ok, r.mismatches


def test_fpl_transfers_still_verify_the_old_way():
    before = [{"element": 1}, {"element": 2}]
    after = [{"element": 1}, {"element": 3}]
    assert verify_transfers([{"element_in": 3, "element_out": 2}], before, after).ok


# ─── policy identity ────────────────────────────────────────────────────


def test_two_leagues_with_the_same_team_id_get_distinct_policies():
    """Every ESPN league numbers its teams from 1, so a bare `espn:4` key would let one
    league's policy silently govern another's team."""
    a = LeaguePolicy(platform="espn", entry=4, context=CTX)
    b = LeaguePolicy(platform="espn", entry=4, context={**CTX, "leagueId": 111})
    assert a.key != b.key


def test_a_policy_that_could_never_execute_cannot_be_saved(tmp_path):
    with pytest.raises(ValueError, match="context"):
        LeaguePolicy(platform="espn", entry=4)


def test_espn_policies_round_trip(tmp_path):
    from sportsdata_agents.fantasy.policy import load_policies

    p = LeaguePolicy(platform="espn", entry=TEAM, context=CTX, lineup="auto")
    save_policy(p, tmp_path)
    assert load_policies(tmp_path)[p.key].context == CTX


# ─── the propose tools go through the policy ────────────────────────────


class FakeMCP:
    def __init__(self, *, period=1, teams=None, budget=None):
        self.period, self.budget = period, budget
        self.teams = teams if teams is not None else [
            {"id": TEAM, "roster": {"entries": [
                {"playerId": 222, "lineupSlotId": 2}, {"playerId": 333, "lineupSlotId": 20}]}}]
        self.calls: list[str] = []

    async def __call__(self, name, args):
        self.calls.append(name)
        if name == "espnfantasy_status":
            return {"status": {"latestScoringPeriod": self.period}}
        if name == "espnfantasy_rosters":
            return {"teams": self.teams}
        if name == "espnfantasy_teams":
            return {"teams": [{"id": TEAM, "waiverBudgetRemaining": self.budget}]}
        return {"ok": True}


@pytest.fixture
def espn(monkeypatch, tmp_path):
    fake = FakeMCP()
    monkeypatch.setattr(ef, "_mcp_call", fake)
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")
    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    return fake


async def test_with_no_policy_it_refuses_rather_than_inventing_a_league(espn):
    with pytest.raises(RuntimeError, match="no policy"):
        await ef.espn_propose_lineup({
            "entry": TEAM, "leagueId": LEAGUE, "summary": "x",
            "items": [{"playerId": 333, "fromLineupSlotId": 20, "toLineupSlotId": 2}]})


async def test_default_policy_proposes_rather_than_writing(espn, tmp_path):
    save_policy(LeaguePolicy(platform="espn", entry=TEAM, context=CTX), tmp_path)
    out = await ef.espn_propose_lineup({
        "entry": TEAM, "leagueId": LEAGUE, "summary": "Start the better player",
        "items": [{"playerId": 333, "fromLineupSlotId": 20, "toLineupSlotId": 2}]})
    assert out["status"] == "proposed"
    assert out["team_changed"] is False
    assert "espnfantasy_set_lineup" not in espn.calls


async def test_an_auto_policy_writes_and_reads_back(espn, tmp_path):
    save_policy(LeaguePolicy(platform="espn", entry=TEAM, context=CTX,
                             lineup="auto", quiet_hours=None), tmp_path)
    espn.teams = [{"id": TEAM, "roster": {"entries": [{"playerId": 333, "lineupSlotId": 2}]}}]
    out = await ef.espn_propose_lineup({
        "entry": TEAM, "leagueId": LEAGUE, "summary": "Start him",
        "items": [{"playerId": 333, "fromLineupSlotId": 20, "toLineupSlotId": 2}]})
    assert out["status"] == "acted"
    assert espn.calls.count("espnfantasy_rosters") == 2   # before AND after
    assert "espnfantasy_set_lineup" in espn.calls


async def test_a_bid_beyond_the_budget_is_refused_before_the_policy_sees_it(espn, tmp_path):
    """A model reasoning about a budget it has not read will bid 40 out of 12."""
    save_policy(LeaguePolicy(platform="espn", entry=TEAM, context=CTX,
                             transfers="auto", max_hit=100, quiet_hours=None), tmp_path)
    espn.budget = 12
    with pytest.raises(ValueError, match="exceeds the 12"):
        await ef.espn_propose_add_drop({
            "entry": TEAM, "leagueId": LEAGUE, "summary": "Big bid",
            "type": "WAIVER", "bidAmount": 40, "add": [999]})
    assert "espnfantasy_add_drop" not in espn.calls


async def test_a_waiver_bid_is_what_max_hit_governs(espn, tmp_path):
    save_policy(LeaguePolicy(platform="espn", entry=TEAM, context=CTX,
                             transfers="auto", max_hit=5, quiet_hours=None), tmp_path)
    espn.budget = 100
    out = await ef.espn_propose_add_drop({
        "entry": TEAM, "leagueId": LEAGUE, "summary": "Claim him",
        "type": "WAIVER", "bidAmount": 20, "add": [999]})
    assert out["bid_amount"] == 20
    assert out["status"] == "proposed"
    assert "above the 5-point limit" in out["policy"]
    assert "espnfantasy_add_drop" not in espn.calls


async def test_no_scoring_period_refuses_rather_than_guessing_a_week(espn, monkeypatch, tmp_path):
    save_policy(LeaguePolicy(platform="espn", entry=TEAM, context=CTX), tmp_path)

    async def blank(name, args):
        return {"status": {}}

    monkeypatch.setattr(ef, "_mcp_call", blank)
    with pytest.raises(RuntimeError, match="scoring period"):
        await ef.espn_propose_lineup({
            "entry": TEAM, "leagueId": LEAGUE, "summary": "x",
            "items": [{"playerId": 1, "fromLineupSlotId": 20, "toLineupSlotId": 2}]})


def test_the_intended_lineup_is_normalised_to_the_shape_the_read_returns():
    """The write names where a player is GOING (`toLineupSlotId`); the read reports where
    he now IS (`lineupSlotId`). Before this was normalised, the verifier compared the
    roster against an EMPTY intent — so every correct ESPN write was reported as failed,
    silently and identically to a real failure."""
    a = adapter_for("espn")
    payload = {"items": [
        {"playerId": 333, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 2},
        {"playerId": 222, "type": "LINEUP", "fromLineupSlotId": 2, "toLineupSlotId": 20},
    ]}
    picks = a.intended_picks(payload)
    assert [(p["element"], p["position"]) for p in picks] == [(333, 2), (222, 20)]
    # …and it round-trips against a matching read.
    after = espn_roster((333, 2), (222, 20))
    assert verify_lineup(picks, after, platform="espn").ok


def test_add_drop_items_are_not_mistaken_for_lineup_moves():
    a = adapter_for("espn")
    payload = {"items": [{"playerId": 9, "type": "ADD", "toTeamId": TEAM}]}
    assert a.intended_picks(payload) == []


def test_espn_has_no_hard_deadline_so_the_too_early_rule_does_not_apply():
    """ESPN's horizon is a rolling `now + N`, so hours_left never counts down. Applying
    FPL's too-early rule there is a constant, not a window — it blocked the agent
    permanently."""
    from sportsdata_agents.fantasy.policy import Verdict

    p = LeaguePolicy(platform="espn", entry=TEAM, context=CTX,
                     lineup="auto", quiet_hours=None)
    d = p.for_lineup(now=NOW, deadline=NOW + timedelta(hours=12))
    assert d.verdict is Verdict.ACT
    # FPL keeps it: 12h out is still too early there.
    f = LeaguePolicy(platform="fpl", entry=1, lineup="auto", quiet_hours=None)
    assert f.for_lineup(now=NOW, deadline=NOW + timedelta(hours=12)).verdict is Verdict.SKIP
