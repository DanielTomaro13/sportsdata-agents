"""Tests for approvals, read-back verification, and the one write path.

The most valuable tests here are the ones that assert something does NOT happen:
an expired approval is not honoured, a failed write is not retried, and a write whose
read-back disagrees is not reported as a success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sportsdata_agents.fantasy.approvals import State, Store, new_proposal
from sportsdata_agents.fantasy.execute import Intent, run_intent
from sportsdata_agents.fantasy.policy import Decision, Verdict
from sportsdata_agents.fantasy.verify import verify_lineup, verify_transfers

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(hours=3)


def make_proposal(expires: datetime = DEADLINE):
    return new_proposal(
        platform="fpl", entry=3942695, action="lineup",
        summary="Set the XI for GW2", diff=["Salah → captain"],
        payload={"picks": []}, expires_at=expires,
    )


def store_at(tmp_path) -> Store:
    s = Store.load(tmp_path)
    return s


# ─── approvals: expiry is the safety property ───────────────────────────


def test_an_expired_proposal_cannot_be_approved(tmp_path):
    """The important refusal. Acting on a stale approval — a transfer agreed for a
    gameweek that has since locked — is worse than not acting at all."""
    s = store_at(tmp_path)
    p = s.add(make_proposal(expires=NOW - timedelta(minutes=1)))
    got, msg = s.approve(p.id, now=NOW)
    assert got.state is State.EXPIRED
    assert "expired" in msg
    assert got.state is not State.APPROVED


def test_expiry_is_applied_when_listing_not_only_when_approving(tmp_path):
    s = store_at(tmp_path)
    s.add(make_proposal(expires=NOW - timedelta(hours=1)))
    assert s.pending(now=NOW) == []
    assert Store.load(tmp_path).proposals.popitem()[1].state is State.EXPIRED


def test_a_live_proposal_approves(tmp_path):
    s = store_at(tmp_path)
    p = s.add(make_proposal())
    got, msg = s.approve(p.id, now=NOW)
    assert got.state is State.APPROVED and msg == "approved"


def test_approving_twice_does_not_re_arm(tmp_path):
    s = store_at(tmp_path)
    p = s.add(make_proposal())
    s.approve(p.id, now=NOW)
    _, msg = s.approve(p.id, now=NOW)
    assert msg == "already approved"


def test_id_prefix_lookup_refuses_an_ambiguous_match(tmp_path):
    s = store_at(tmp_path)
    s.add(make_proposal())
    s.add(make_proposal())
    assert s.find("") is None  # matches both → no guess


def test_proposals_survive_a_reload(tmp_path):
    s = store_at(tmp_path)
    p = s.add(make_proposal())
    assert Store.load(tmp_path).proposals[p.id].summary == p.summary


def test_notification_names_the_cost_and_the_expiry(tmp_path):
    p = new_proposal(
        platform="fpl", entry=1, action="transfer", summary="Salah → Saka",
        diff=["OUT Salah", "IN Saka"], payload={}, expires_at=DEADLINE, cost_points=4,
    )
    text = p.as_notification()
    assert "cost: 4 points" in text
    assert "expires in" in text
    assert p.id[:8] in text


# ─── read-back verification ─────────────────────────────────────────────


def picks(*elements, captain=None):
    out = []
    for i, e in enumerate(elements, start=1):
        out.append({
            "element": e, "position": i,
            "is_captain": e == captain, "is_vice_captain": False,
            "multiplier": 2 if e == captain else 1,
        })
    return out


def test_a_matching_read_back_confirms():
    want = picks(1, 2, 3, captain=1)
    assert verify_lineup(want, want).ok


def test_a_captain_that_did_not_move_is_caught():
    """The exact silent failure this exists for: FPL returns 200, the captain stays put,
    and the owner finds out when the points do not double."""
    want = picks(1, 2, 3, captain=2)
    got = picks(1, 2, 3, captain=1)
    r = verify_lineup(want, got)
    assert not r.ok
    assert any("is_captain" in m for m in r.mismatches)


def test_a_missing_player_is_caught():
    r = verify_lineup(picks(1, 2, 3, captain=1), picks(1, 2, captain=1))
    assert not r.ok
    assert any("missing" in m for m in r.mismatches)


def test_no_captain_at_all_is_named_explicitly():
    r = verify_lineup(picks(1, 2, 3), picks(1, 2, 3))
    assert not r.ok
    assert any("NO CAPTAIN" in m for m in r.mismatches)


def test_formation_is_checked_on_a_full_squad():
    """FPL can accept a lineup and quietly land an illegal XI. On a complete 15-man
    squad that is detectable, so it is detected."""
    want = picks(*range(1, 16), captain=1)
    got = picks(*range(1, 16), captain=1)
    got[10]["position"] = 12          # only 10 in the XI now
    r = verify_lineup(want, got)
    assert not r.ok
    assert any("in the XI" in m for m in r.mismatches)


def test_formation_is_not_checked_on_a_partial_pick_list():
    """The verifier must not cry wolf — a false alarm here is how a real mismatch gets
    ignored later."""
    want = picks(1, 2, 3, captain=1)
    assert verify_lineup(want, want).ok


def test_an_empty_read_back_fails_rather_than_passing():
    """A verification that could not run must not read as success."""
    assert not verify_lineup(picks(1, 2, captain=1), []).ok


def test_transfer_verification_confirms_both_directions():
    before = [{"element": 1}, {"element": 2}]
    after = [{"element": 1}, {"element": 3}]
    r = verify_transfers([{"element_in": 3, "element_out": 2}], before, after)
    assert r.ok, r.mismatches


def test_a_transfer_that_did_not_happen_is_caught():
    before = after = [{"element": 1}, {"element": 2}]
    r = verify_transfers([{"element_in": 3, "element_out": 2}], before, after)
    assert not r.ok


def test_an_unrequested_move_is_flagged():
    """The scariest outcome — the write did something nobody approved."""
    before = [{"element": 1}, {"element": 2}]
    after = [{"element": 1}, {"element": 3}, {"element": 9}]
    r = verify_transfers([{"element_in": 3, "element_out": 2}], before, after)
    assert not r.ok
    assert any("NOT requested" in m for m in r.mismatches)


def test_a_failed_result_says_so_loudly():
    r = verify_lineup(picks(1, captain=1), picks(1))
    assert "DID NOT LAND" in r.as_notification()


# ─── the write path ─────────────────────────────────────────────────────


class FakeMCP:
    """Records calls; serves a squad that the test controls."""

    def __init__(self, squad, *, raise_on: str | None = None):
        self.squad = squad
        self.raise_on = raise_on
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool: str, **kwargs):
        self.calls.append((tool, kwargs))
        if tool == self.raise_on:
            raise RuntimeError("upstream timed out")
        if tool == "fpl_my_team":
            return {"picks": self.squad}
        return {"ok": True}

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    """Nothing in these tests should reach a real channel."""
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")


async def test_skip_makes_no_calls_at_all(tmp_path):
    mcp = FakeMCP(picks(1, captain=1))
    intent = Intent("lineup", 1, "x", [], {"picks": picks(1, captain=1)}, DEADLINE)
    out = await run_intent(
        intent, Decision(Verdict.SKIP, "policy: lineup is off"),
        call=mcp, store=store_at(tmp_path),
    )
    assert out.status == "skipped"
    assert mcp.calls == []


async def test_ask_writes_a_proposal_and_touches_nothing(tmp_path):
    mcp = FakeMCP(picks(1, captain=1))
    intent = Intent("lineup", 1, "Set the XI", ["Salah → C"], {"picks": []}, DEADLINE)
    out = await run_intent(
        intent, Decision(Verdict.ASK, "policy: lineup is set to ask"),
        call=mcp, store=store_at(tmp_path),
    )
    assert out.status == "proposed"
    assert out.proposal.state is State.PENDING
    assert "fpl_set_lineup" not in mcp.tools()


async def test_act_writes_then_reads_back(tmp_path):
    want = picks(1, 2, captain=1)
    mcp = FakeMCP(want)
    intent = Intent("lineup", 1, "Set the XI", [], {"picks": want}, DEADLINE)
    out = await run_intent(
        intent, Decision(Verdict.ACT, "policy: lineup is automatic"),
        call=mcp, store=store_at(tmp_path), csrf="tok",
    )
    assert out.status == "acted"
    # read → write → read: the read-back is not optional
    assert mcp.tools() == ["fpl_my_team", "fpl_set_lineup", "fpl_my_team"]
    assert out.verification.ok


async def test_a_write_whose_read_back_disagrees_is_reported_as_failed(tmp_path):
    """A 200 is not proof. The squad FPL reports afterwards is."""
    mcp = FakeMCP(picks(1, 2, captain=2))            # captain did not move
    intent = Intent("lineup", 1, "x", [], {"picks": picks(1, 2, captain=1)}, DEADLINE)
    out = await run_intent(
        intent, Decision(Verdict.ACT, "auto"), call=mcp,
        store=store_at(tmp_path), csrf="tok",
    )
    assert out.status == "failed"
    assert not out.verification.ok


async def test_a_failed_write_is_never_retried(tmp_path):
    """A transfer that timed out may still have been applied. Sending it again is how
    you pay two points hits for one move."""
    mcp = FakeMCP([], raise_on="fpl_transfers")
    intent = Intent("transfer", 1, "x", [], {"transfers": []}, DEADLINE)
    out = await run_intent(
        intent, Decision(Verdict.ACT, "auto"), call=mcp,
        store=store_at(tmp_path), csrf="tok",
    )
    assert out.status == "failed"
    assert "NOT retried" in out.detail
    assert mcp.tools().count("fpl_transfers") == 1


async def test_an_unapproved_proposal_cannot_be_executed(tmp_path):
    """Defence in depth — a caller that gets here with a pending proposal has a bug, and
    the bug must not reach the API."""
    s = store_at(tmp_path)
    p = s.add(make_proposal())
    mcp = FakeMCP(picks(1, captain=1))
    intent = Intent("lineup", 1, "x", [], {"picks": []}, DEADLINE)
    out = await run_intent(
        intent, Decision(Verdict.ASK, "ask"), call=mcp, store=s, approved=p,
    )
    assert out.status == "skipped"
    assert mcp.calls == []


async def test_an_approved_proposal_executes_and_records_its_outcome(tmp_path):
    s = store_at(tmp_path)
    p = s.add(make_proposal())
    s.approve(p.id, now=NOW)
    want = picks(1, 2, captain=1)
    mcp = FakeMCP(want)
    intent = Intent("lineup", 3942695, "x", [], {"picks": want}, DEADLINE)
    out = await run_intent(
        intent, Decision(Verdict.ASK, "ask"), call=mcp, store=s,
        csrf="tok", approved=s.proposals[p.id],
    )
    assert out.status == "acted"
    assert Store.load(tmp_path).proposals[p.id].state is State.EXECUTED
