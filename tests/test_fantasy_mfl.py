"""MyFantasyLeague: the adapter, and the three ways this API differs dangerously.

MFL's write contract is documented, which makes it the most knowable of the three
platforms — and its defaults are the most dangerous. A lineup write replaces everything,
a waiver claim appends by default, and an add/drop cannot be undone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sportsdata_agents.fantasy.adapters import adapter_for
from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy
from sportsdata_agents.fantasy.verify import verify_transfers
from sportsdata_agents.tools import mfl_fantasy as mf

LEAGUE, YEAR, TEAM = "12345", 2026, 1
CTX = {"leagueId": LEAGUE, "year": YEAR}


# ─── the write gate ─────────────────────────────────────────────────────


def test_the_mfl_agent_is_never_granted_the_raw_write_group():
    import yaml

    import sportsdata_agents

    spec = yaml.safe_load(
        (Path(sportsdata_agents.__file__).parent / "specs" / "mfl_manager.yaml").read_text())
    tools = spec["agent"]["tools"]
    assert not any(g.endswith(".write") for g in tools["mcp_groups"])
    assert "sport.transactions" in spec["agent"]["forbidden_capabilities"]


def test_no_propose_tool_lets_the_model_act_as_another_franchise():
    """FRANCHISE_ID is MFL's commissioner impersonation. If it were in the schema, a
    model could rewrite a stranger's roster by filling in a field."""
    for name in mf.MFL_TOOLS:
        props = mf.MFL_TOOLS[name].parameters["properties"]
        assert "FRANCHISE_ID" not in props
        assert "franchiseId" not in props


def test_the_adapter_strips_commissioner_impersonation_even_if_asked():
    """Defence in depth: not in the schema, and removed on the way out regardless."""
    a = adapter_for("mfl")
    _tool, args = a.lineup_call(TEAM, {"STARTERS": ["1"], "FRANCHISE_ID": "0009"},
                                {**CTX, "week": 1})
    assert "FRANCHISE_ID" not in args
    _tool, args = a.roster_call(TEAM, {"_tool": "mfl_add_drop", "ADD": "1",
                                       "FRANCHISE_ID": "0009"}, CTX)
    assert "FRANCHISE_ID" not in args


# ─── the adapter ────────────────────────────────────────────────────────


def test_franchise_ids_are_four_digit_strings():
    """`1` matches nothing in an MFL response, which reads as "you are not in this
    league" rather than as the type error it is."""
    a = adapter_for("mfl")
    _tool, args = a.read_squad_call(TEAM, {**CTX, "teamId": TEAM})
    assert args["FRANCHISE"] == "0001"


def test_one_franchise_and_many_are_read_the_same_way():
    """MFL returns one row as an object and many as a list — the shape trap that works
    all season and breaks the week a league has one of something."""
    a = adapter_for("mfl")
    ctx = {**CTX, "teamId": TEAM}
    many = {"rosters": {"franchise": [
        {"id": "0002", "player": [{"id": "1", "status": "ROSTER"}]},
        {"id": "0001", "player": [{"id": "13593", "status": "ROSTER"}]}]}}
    one = {"rosters": {"franchise": {"id": "0001", "player": {"id": "13593", "status": "ROSTER"}}}}
    assert [p["element"] for p in a.picks_from(many, ctx)] == ["13593"]
    assert [p["element"] for p in a.picks_from(one, ctx)] == ["13593"]


def test_roster_status_is_carried_so_an_ir_move_is_verifiable():
    a = adapter_for("mfl")
    body = {"rosters": {"franchise": {"id": "0001", "player": [
        {"id": "1", "status": "ROSTER"}, {"id": "2", "status": "INJURED_RESERVE"}]}}}
    picks = a.picks_from(body, {**CTX, "teamId": TEAM})
    assert {p["element"]: p["position"] for p in picks} == {
        "1": "ROSTER", "2": "INJURED_RESERVE"}


def test_a_team_absent_from_the_response_yields_nothing():
    a = adapter_for("mfl")
    body = {"rosters": {"franchise": {"id": "0009", "player": [{"id": "1"}]}}}
    assert a.picks_from(body, {**CTX, "teamId": TEAM}) == []


def test_mfl_refuses_to_build_a_call_without_league_identity():
    with pytest.raises(ValueError, match="leagueId"):
        adapter_for("mfl").lineup_call(TEAM, {}, {"year": YEAR})


def test_string_player_ids_survive_the_verifier():
    """MFL ids are numeric-looking STRINGS. The verifier used to coerce with int(),
    which happened to work — until a platform used a non-numeric id."""
    before = [{"element": "13593"}, {"element": "0002x"}]
    after = [{"element": "13593"}, {"element": "14208"}]
    r = verify_transfers([{"playerId": "14208", "type": "ADD"},
                          {"playerId": "0002x", "type": "DROP"}],
                         before, after, platform="mfl")
    assert r.ok, r.mismatches


# ─── the league's own rules, not a remembered formation ─────────────────


def test_starter_requirements_are_read_from_the_league():
    count, rules = mf.starter_requirements({
        "starters": {"count": "9", "position": [
            {"name": "QB", "limit": "1"}, {"name": "RB", "limit": "2-4"}]}})
    assert count == 9
    assert rules == ["QB: 1", "RB: 2-4"]


def test_a_flexible_starter_count_disables_the_check_rather_than_guessing():
    """Leagues with a range ("8-9") have no single legal number, so the count check is
    skipped. Guessing one would refuse perfectly legal lineups."""
    count, rules = mf.starter_requirements({"starters": {"count": "8-9", "position": []}})
    assert count is None
    assert rules == []


# ─── the propose tools ──────────────────────────────────────────────────


class FakeMCP:
    def __init__(self, *, starters_count="2", week=3, roster=None):
        self.starters_count, self.week = starters_count, week
        self.roster = roster if roster is not None else [
            {"id": "13593", "status": "ROSTER"}, {"id": "14208", "status": "ROSTER"}]
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "mfl_league":
            return {"league": {"currentWeek": self.week,
                               "starters": {"count": self.starters_count,
                                            "position": [{"name": "QB", "limit": "1"}]}}}
        if name == "mfl_rosters":
            return {"rosters": {"franchise": {"id": "0001", "player": self.roster}}}
        return {"status": "OK"}

    @property
    def tools(self):
        return [t for t, _ in self.calls]


@pytest.fixture
def mfl(monkeypatch, tmp_path):
    fake = FakeMCP()
    monkeypatch.setattr(mf, "_mcp_call", fake)
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")
    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    return fake


async def test_without_a_policy_it_refuses_rather_than_inventing_a_league(mfl):
    with pytest.raises(RuntimeError, match="no policy"):
        await mf.mfl_propose_lineup({"entry": TEAM, "leagueId": LEAGUE,
                                     "starters": ["1", "2"], "summary": "x"})


async def test_a_short_lineup_is_refused_before_anything_is_sent(mfl, tmp_path):
    """The trap this exists for: STARTERS is a FULL REPLACEMENT, so a lineup missing a
    player does not error — it silently benches them."""
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX,
                             lineup="auto", quiet_hours=None), tmp_path)
    with pytest.raises(ValueError, match="FULL REPLACEMENT"):
        await mf.mfl_propose_lineup({"entry": TEAM, "leagueId": LEAGUE,
                                     "starters": ["13593"], "summary": "one short"})
    assert "mfl_set_lineup" not in mfl.tools


async def test_default_policy_proposes_rather_than_writing(mfl, tmp_path):
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX), tmp_path)
    out = await mf.mfl_propose_lineup({"entry": TEAM, "leagueId": LEAGUE,
                                       "starters": ["13593", "14208"], "summary": "Set it"})
    assert out["status"] == "proposed"
    assert out["team_changed"] is False
    assert "mfl_set_lineup" not in mfl.tools


async def test_an_auto_policy_writes_and_reads_back(mfl, tmp_path):
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX,
                             lineup="auto", quiet_hours=None), tmp_path)
    out = await mf.mfl_propose_lineup({"entry": TEAM, "leagueId": LEAGUE,
                                       "starters": ["13593", "14208"], "summary": "Set it"})
    assert out["status"] == "acted"
    assert "mfl_set_lineup" in mfl.tools
    assert mfl.tools.count("mfl_rosters") == 2      # before AND after


async def test_a_blind_bid_always_replaces_the_queue(mfl, tmp_path):
    """MFL APPENDS by default. A scheduled job that runs twice would otherwise submit
    the same claim twice and bid for it twice."""
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX,
                             transfers="auto", max_hit=50, quiet_hours=None), tmp_path)
    out = await mf.mfl_propose_blind_bid({
        "entry": TEAM, "leagueId": LEAGUE, "summary": "Bid",
        "bids": [{"add": "999", "amount": 7, "drop": "13593"}]})
    assert out["total_bid"] == 7
    _t, args = next(c for c in mfl.calls if c[0] == "mfl_blind_bid")
    assert args["REPLACE"] == 1
    assert args["PICKS"] == "999_7_13593"


async def test_a_bid_with_no_drop_uses_mfls_sentinel(mfl, tmp_path):
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX,
                             transfers="auto", max_hit=50, quiet_hours=None), tmp_path)
    await mf.mfl_propose_blind_bid({
        "entry": TEAM, "leagueId": LEAGUE, "summary": "Bid",
        "bids": [{"add": "999", "amount": 3}]})
    _t, args = next(c for c in mfl.calls if c[0] == "mfl_blind_bid")
    assert args["PICKS"] == "999_3_0000"


async def test_the_total_bid_is_what_max_hit_governs(mfl, tmp_path):
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX,
                             transfers="auto", max_hit=5, quiet_hours=None), tmp_path)
    out = await mf.mfl_propose_blind_bid({
        "entry": TEAM, "leagueId": LEAGUE, "summary": "Too rich",
        "bids": [{"add": "999", "amount": 12}]})
    assert out["total_bid"] == 12
    assert out["status"] == "proposed"
    assert "above the 5-point limit" in out["policy"]
    assert "mfl_blind_bid" not in mfl.tools


async def test_an_add_drop_warns_that_it_cannot_be_undone(mfl, tmp_path):
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX), tmp_path)
    out = await mf.mfl_propose_add_drop({
        "entry": TEAM, "leagueId": LEAGUE, "summary": "Grab him", "add": "999"})
    assert out["status"] == "proposed"
    from sportsdata_agents.fantasy.approvals import Store

    proposal = Store.load(tmp_path).find(out["proposal_id"])
    assert any("cancel" in d for d in proposal.diff)


async def test_no_week_refuses_rather_than_guessing(mfl, monkeypatch, tmp_path):
    save_policy(LeaguePolicy(platform="mfl", entry=TEAM, context=CTX), tmp_path)

    async def no_league(name, args):
        return {}

    monkeypatch.setattr(mf, "_mcp_call", no_league)
    with pytest.raises(RuntimeError, match="league settings"):
        await mf.mfl_propose_lineup({"entry": TEAM, "leagueId": LEAGUE,
                                     "starters": ["1"], "summary": "x"})
