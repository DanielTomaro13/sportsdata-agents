"""Tests for the native tools that are the agent's only route to a real team.

The claims worth making here are structural: the model cannot reach FPL without passing
the policy, and it cannot influence the two facts the policy depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sportsdata_agents.tools import fantasy as ft
from sportsdata_agents.tools.registry import get_native_tools

ENTRY = 3942695
SOON = datetime.now(tz=UTC) + timedelta(hours=3)
FAR = datetime.now(tz=UTC) + timedelta(days=5)


def squad(captain=1):
    return [
        {"element": e, "position": i, "is_captain": e == captain,
         "is_vice_captain": False, "multiplier": 2 if e == captain else 1}
        for i, e in enumerate(range(1, 16), start=1)
    ]


class FakeMCP:
    """Stands in for the MCP server. Records every tool the code reached for."""

    def __init__(self, *, deadline=SOON, transfers=None, picks=None, event=2):
        self.deadline, self.event = deadline, event
        self.transfers = transfers if transfers is not None else {
            "status": "limited", "cost": 4, "limit": 1, "made": 0, "bank": 5,
        }
        self.picks = picks if picks is not None else squad()
        self.calls: list[str] = []

    async def __call__(self, name, args):
        self.calls.append(name)
        if name == "fpl_gameweeks":
            return {"events": [
                {"id": self.event - 1, "is_next": False, "deadline_time": "2026-08-15T17:30:00Z"},
                {"id": self.event, "is_next": True,
                 "deadline_time": self.deadline.isoformat().replace("+00:00", "Z")},
            ]}
        if name == "fpl_my_team":
            return {"picks": self.picks, "transfers": self.transfers}
        return {"ok": True}


@pytest.fixture
def mcp(monkeypatch, tmp_path):
    fake = FakeMCP()
    monkeypatch.setattr(ft, "_mcp_call", fake)
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")
    # Policies and proposals go to a temp dir, never the real one.
    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    return fake


# ─── the structural claim ───────────────────────────────────────────────


def test_the_agent_is_never_granted_a_raw_write_tool():
    """If `fpl_set_lineup` were reachable, the policy would be decoration."""
    import yaml

    import sportsdata_agents

    packaged = Path(sportsdata_agents.__file__).parent / "specs" / "fpl_manager.yaml"
    spec = yaml.safe_load(packaged.read_text())
    tools = spec["agent"]["tools"]
    assert "fpl.write" not in tools["mcp_groups"]
    assert not any(g.endswith(".write") for g in tools["mcp_groups"])
    assert "sport.transactions" in spec["agent"]["forbidden_capabilities"]


def test_the_no_money_filter_still_denies_everything_that_moves_money():
    """FPL "transfers" tripped the money-verb filter, so two exact names are exempt.
    This pins the hole to exactly those two — the filter's value is that it is blunt,
    and a relaxed regex would silently re-admit the real money verbs."""
    from sportsdata_agents.mcp.manager import DENY_EXCEPTIONS, is_denied

    assert {"fpl_transfers", "fpl_propose_transfer"} == DENY_EXCEPTIONS
    for name in (
        "betfair_cashout", "place_bet", "account_balance", "wallet_withdraw",
        "sportsbet_betslip", "deposit_funds", "bank_transfer", "wire_transfer",
        "fpl_transfers_v2", "transfer_money",
    ):
        assert is_denied(name), f"{name} must stay denied"
    assert not is_denied("fpl_transfers")
    assert not is_denied("fpl_propose_transfer")


def test_the_propose_tools_are_registered_and_resolvable():
    names = ["fpl_propose_lineup", "fpl_propose_transfer", "fantasy_review_proposals"]
    assert [t.name for t in get_native_tools(names)] == names


def test_no_propose_tool_accepts_a_deadline_or_a_cost():
    """The two facts the policy depends on must not be model-supplied — if they were in
    the schema, a hallucinated timestamp would defeat every timing rule."""
    for name in ("fpl_propose_lineup", "fpl_propose_transfer"):
        props = ft.FANTASY_TOOLS[name].parameters["properties"]
        assert "deadline" not in props
        assert "points_cost" not in props
        assert "hit" not in props
        assert "cost" not in props


# ─── policy is actually consulted ───────────────────────────────────────


async def test_default_policy_proposes_rather_than_writing(mcp):
    out = await ft.fpl_propose_lineup(
        {"entry": ENTRY, "picks": squad(), "summary": "Set the XI"})
    assert out["status"] == "proposed"
    assert out["team_changed"] is False
    assert out["awaiting_owner"] is True
    assert "fpl_set_lineup" not in mcp.calls


async def test_an_auto_policy_writes_and_verifies(mcp, tmp_path):
    from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy

    save_policy(LeaguePolicy(platform="fpl", entry=ENTRY, lineup="auto",
                             quiet_hours=None), tmp_path)
    out = await ft.fpl_propose_lineup(
        {"entry": ENTRY, "picks": squad(), "summary": "Set the XI"})
    assert out["status"] == "acted"
    assert out["team_changed"] is True
    assert out["verified"] is True
    assert "fpl_set_lineup" in mcp.calls


async def test_a_far_deadline_is_refused_even_on_auto(mcp, tmp_path):
    from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy

    mcp.deadline = FAR
    save_policy(LeaguePolicy(platform="fpl", entry=ENTRY, lineup="auto",
                             quiet_hours=None), tmp_path)
    out = await ft.fpl_propose_lineup(
        {"entry": ENTRY, "picks": squad(), "summary": "Set the XI"})
    assert out["status"] == "skipped"
    assert "too early" in out["policy"]
    assert "fpl_set_lineup" not in mcp.calls


async def test_a_chip_is_routed_to_the_owner_even_when_lineup_is_auto(mcp, tmp_path):
    """The chip must be judged as a chip, not inherited from the lineup's setting."""
    from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy

    save_policy(LeaguePolicy(platform="fpl", entry=ENTRY, lineup="auto",
                             quiet_hours=None), tmp_path)
    out = await ft.fpl_propose_lineup(
        {"entry": ENTRY, "picks": squad(), "summary": "Bench boost", "chip": "bboost"})
    assert out["status"] == "proposed"
    assert "chip" in out["policy"]
    assert "fpl_set_lineup" not in mcp.calls


async def test_no_upcoming_gameweek_refuses_rather_than_guessing(mcp, monkeypatch):
    """With no deadline there is nothing to time a write against, so the safe answer is
    to send nothing rather than to pick a plausible-looking date."""
    async def no_next(name, args):
        return {"events": [{"id": 38, "is_next": False,
                            "deadline_time": "2026-05-01T17:30:00Z"}]}

    monkeypatch.setattr(ft, "_mcp_call", no_next)
    with pytest.raises(RuntimeError, match="no upcoming gameweek"):
        await ft.fpl_propose_lineup({"entry": ENTRY, "picks": squad(), "summary": "x"})


# ─── the cost the model does not get to state ───────────────────────────


def test_the_unlimited_window_is_free():
    """Before the first deadline, changes are free and unlimited. Quoting a 4-point hit
    here is the most common FPL reasoning error there is."""
    hit, _free, how = ft._transfer_cost({"transfers": {"status": "unlimited", "limit": None}}, 5)
    assert hit == 0
    assert "free and unlimited" in how


def test_transfers_beyond_the_free_ones_cost_points():
    block = {"transfers": {"status": "limited", "cost": 4, "limit": 1, "made": 0}}
    assert ft._transfer_cost(block, 1)[0] == 0
    assert ft._transfer_cost(block, 2)[0] == 4
    assert ft._transfer_cost(block, 3)[0] == 8


def test_used_transfers_reduce_the_free_allowance():
    block = {"transfers": {"status": "limited", "cost": 4, "limit": 2, "made": 2}}
    hit, free, _ = ft._transfer_cost(block, 1)
    assert (hit, free) == (4, 0)


async def test_a_hit_above_the_ceiling_is_routed_to_the_owner(mcp, tmp_path):
    from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy

    save_policy(LeaguePolicy(platform="fpl", entry=ENTRY, transfers="auto", max_hit=4,
                             quiet_hours=None), tmp_path)
    out = await ft.fpl_propose_transfer({
        "entry": ENTRY, "summary": "Three moves",
        "transfers": [{"element_in": i, "element_out": i + 20} for i in (1, 2, 3)],
    })
    assert out["points_cost"] == 8            # 1 free, 2 chargeable
    assert out["status"] == "proposed"
    assert "above the 4-point limit" in out["policy"]
    assert "fpl_transfers" not in mcp.calls


async def test_a_free_transfer_on_auto_if_free_goes_through(mcp, tmp_path):
    from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy

    save_policy(LeaguePolicy(platform="fpl", entry=ENTRY, transfers="auto_if_free",
                             quiet_hours=None), tmp_path)
    mcp.picks = [{"element": 30}, *squad()[1:]]   # the "after" squad has the new player
    out = await ft.fpl_propose_transfer({
        "entry": ENTRY, "summary": "One move",
        "transfers": [{"element_in": 30, "element_out": 1}],
    })
    assert out["points_cost"] == 0
    assert "fpl_transfers" in mcp.calls


async def test_review_proposals_cannot_approve(mcp):
    await ft.fpl_propose_lineup({"entry": ENTRY, "picks": squad(), "summary": "Set the XI"})
    out = await ft.fantasy_review_proposals({})
    assert len(out["pending"]) == 1
    assert "Only the owner can approve" in out["note"]
