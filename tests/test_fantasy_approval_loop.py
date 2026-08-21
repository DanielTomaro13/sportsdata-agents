"""The approval loop, end to end.

`agents fantasy approve` used to mark a proposal APPROVED and nothing ever read that
state. The owner said yes, the agent proposed the same thing again on its next run, and
the approval sat there until it expired. An approval queue that never drains is worse
than no queue at all: it looks like consent was honoured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sportsdata_agents.fantasy.approvals import State, Store, new_proposal
from sportsdata_agents.fantasy.execute import execute_approved, intent_from

NOW = datetime.now(tz=UTC)
LATER = NOW + timedelta(hours=6)
ESPN_CTX = {"leagueId": 899098157, "seasonId": 2026, "game": "ffl"}


def fpl_picks(captain=1):
    return [{"element": e, "position": i, "is_captain": e == captain,
             "is_vice_captain": False, "multiplier": 2 if e == captain else 1}
            for i, e in enumerate(range(1, 16), start=1)]


class Recorder:
    def __init__(self, squad=None, teams=None):
        self.squad = squad if squad is not None else fpl_picks()
        self.teams = teams
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, platform):
        async def call(tool, **kwargs):
            self.calls.append((tool, kwargs))
            if tool == "fpl_my_team":
                return {"picks": self.squad}
            if tool == "espnfantasy_rosters":
                return {"teams": self.teams or []}
            return {"ok": True}
        return call

    @property
    def tools(self):
        return [t for t, _ in self.calls]


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")


def store_with(tmp_path, **kw):
    s = Store.load(tmp_path)
    p = s.add(new_proposal(**kw))
    return s, p


# ─── the loop actually closes ───────────────────────────────────────────


async def test_an_approved_proposal_is_carried_out(tmp_path):
    picks = fpl_picks()
    s, p = store_with(tmp_path, platform="fpl", entry=1, action="lineup",
                      summary="Set the XI", diff=["Salah → C"],
                      payload={"picks": picks}, expires_at=LATER)
    s.approve(p.id)
    rec = Recorder(squad=picks)

    done = await execute_approved(call_for=rec, store=s)
    assert len(done) == 1
    assert done[0][1].status == "acted"
    assert "fpl_set_lineup" in rec.tools
    assert Store.load(tmp_path).proposals[p.id].state is State.EXECUTED


async def test_a_pending_proposal_is_left_alone(tmp_path):
    s, _p = store_with(tmp_path, platform="fpl", entry=1, action="lineup", summary="x",
                      diff=[], payload={"picks": fpl_picks()}, expires_at=LATER)
    rec = Recorder()
    assert await execute_approved(call_for=rec, store=s) == []
    assert rec.calls == []


async def test_an_approval_that_expired_before_execution_is_never_carried_out(tmp_path):
    """The rule that matters most, applied at the LAST possible moment: an approval that
    sat unexecuted past its deadline must not be honoured late."""
    s, p = store_with(tmp_path, platform="fpl", entry=1, action="lineup", summary="x",
                      diff=[], payload={"picks": fpl_picks()},
                      expires_at=NOW + timedelta(seconds=1))
    s.approve(p.id)
    s.proposals[p.id].expires_at = (NOW - timedelta(minutes=1)).isoformat(timespec="seconds")
    s.save()

    rec = Recorder()
    assert await execute_approved(call_for=rec, store=s) == []
    assert rec.calls == []
    assert Store.load(tmp_path).proposals[p.id].state is State.EXPIRED


async def test_executing_twice_does_not_write_twice(tmp_path):
    picks = fpl_picks()
    s, p = store_with(tmp_path, platform="fpl", entry=1, action="lineup", summary="x",
                      diff=[], payload={"picks": picks}, expires_at=LATER)
    s.approve(p.id)
    rec = Recorder(squad=picks)

    await execute_approved(call_for=rec, store=s)
    first = rec.tools.count("fpl_set_lineup")
    await execute_approved(call_for=rec, store=s)
    assert rec.tools.count("fpl_set_lineup") == first == 1


async def test_only_targets_one_proposal(tmp_path):
    picks = fpl_picks()
    s = Store.load(tmp_path)
    a = s.add(new_proposal(platform="fpl", entry=1, action="lineup", summary="a",
                           diff=[], payload={"picks": picks}, expires_at=LATER))
    b = s.add(new_proposal(platform="fpl", entry=1, action="lineup", summary="b",
                           diff=[], payload={"picks": picks}, expires_at=LATER))
    s.approve(a.id)
    s.approve(b.id)
    rec = Recorder(squad=picks)

    done = await execute_approved(call_for=rec, store=s, only=a.id)
    assert [p.id for p, _ in done] == [a.id]
    assert Store.load(tmp_path).proposals[b.id].state is State.APPROVED


# ─── the context an ESPN proposal cannot execute without ────────────────


async def test_an_espn_proposal_carries_its_league_identity(tmp_path):
    """A proposal is executed later, possibly in another process. Without league, season
    and game there is no team to write to — so the context rides on the proposal, not
    only on the in-memory intent."""
    s, p = store_with(tmp_path, platform="espn", entry=4, action="lineup",
                      summary="Start him", diff=[], context=ESPN_CTX,
                      payload={"items": [{"playerId": 333, "type": "LINEUP",
                                          "fromLineupSlotId": 20, "toLineupSlotId": 2}]},
                      expires_at=LATER)
    s.approve(p.id)
    assert Store.load(tmp_path).proposals[p.id].context == ESPN_CTX

    rec = Recorder(teams=[{"id": 4, "roster": {"entries": [
        {"playerId": 333, "lineupSlotId": 2}]}}])
    done = await execute_approved(call_for=rec, store=s)
    assert done[0][1].status == "acted"
    _t, args = next(c for c in rec.calls if c[0] == "espnfantasy_set_lineup")
    assert args["leagueId"] == 899098157
    assert args["seasonId"] == 2026
    assert args["game"] == "ffl"


def test_the_rebuilt_intent_matches_what_was_agreed(tmp_path):
    """Rebuilt from the RECORD, not recomputed — if the world moved on, the expiry
    catches it, not a quiet substitution of different picks."""
    payload = {"picks": fpl_picks()}
    _, p = store_with(tmp_path, platform="espn", entry=4, action="lineup", summary="s",
                      diff=["a", "b"], payload=payload, context=ESPN_CTX,
                      expires_at=LATER, cost_points=7)
    i = intent_from(p)
    assert (i.platform, i.entry, i.action) == ("espn", 4, "lineup")
    assert i.payload == payload
    assert i.context == ESPN_CTX
    assert i.cost_points == 7
    assert i.diff == ["a", "b"]


# ─── the csrf bridge ────────────────────────────────────────────────────


def test_the_csrf_token_is_read_out_of_the_session_cookie():
    """`connect fpl` stores three cookies as ONE header. Nothing ever wrote a separate
    FPL_CSRF_TOKEN, so the lookup came back empty, every write sent an empty
    X-CSRFToken, and FPL answered 403 — while the token sat in the string all along."""
    from sportsdata_agents.tools.fantasy import csrf_from_cookie

    header = "pl_profile=eyJ; sessionid=abc123; csrftoken=TOK987"
    assert csrf_from_cookie(header) == "TOK987"
    assert csrf_from_cookie("sessionid=abc123") == ""
    assert csrf_from_cookie("") == ""


def test_csrf_prefers_an_explicit_override(monkeypatch):
    from sportsdata_agents.tools import fantasy as ft

    monkeypatch.setattr(ft, "_stored_secrets", dict)
    monkeypatch.setenv("FPL_SESSION_COOKIE", "csrftoken=FROM_COOKIE")
    assert ft._csrf() == "FROM_COOKIE"
    monkeypatch.setenv("FPL_CSRF_TOKEN", "EXPLICIT")
    assert ft._csrf() == "EXPLICIT"


async def test_an_fpl_write_actually_carries_the_token(tmp_path, monkeypatch):
    """The end the bug was actually at: the header reaching the tool call."""
    from sportsdata_agents.fantasy.runner import csrf_for
    from sportsdata_agents.tools import fantasy as ft

    monkeypatch.setattr(ft, "_stored_secrets", dict)
    monkeypatch.setenv("FPL_SESSION_COOKIE", "sessionid=x; csrftoken=REAL")
    monkeypatch.delenv("FPL_CSRF_TOKEN", raising=False)

    picks = fpl_picks()
    s, p = store_with(tmp_path, platform="fpl", entry=1, action="lineup", summary="x",
                      diff=[], payload={"picks": picks}, expires_at=LATER)
    s.approve(p.id)
    rec = Recorder(squad=picks)

    await execute_approved(call_for=rec, store=s, csrf_for=csrf_for)
    _tool, args = next(c for c in rec.calls if c[0] == "fpl_set_lineup")
    assert args["csrf"] == "REAL", "an empty X-CSRFToken is a guaranteed 403"


async def test_espn_needs_no_csrf(tmp_path):
    """ESPN's cookie pair is enough on its own; sending an empty token there is correct,
    not a bug, and the two platforms must not share one answer."""
    from sportsdata_agents.fantasy.runner import csrf_for

    assert csrf_for("espn") == ""


async def test_a_failure_reports_the_cause_not_the_task_group_wrapper(tmp_path, monkeypatch):
    """MCP failures arrive wrapped by anyio. Recording `str(e)` put "unhandled errors in
    a TaskGroup (1 sub-exception)" in the proposal's outcome — where the owner looks to
    find out what went wrong — instead of the one sentence telling them what to do."""
    s, p = store_with(tmp_path, platform="fpl", entry=1, action="lineup", summary="x",
                      diff=[], payload={"picks": fpl_picks()}, expires_at=LATER)
    s.approve(p.id)

    def exploding(platform):
        async def call(tool, **kwargs):
            if tool == "fpl_set_lineup":
                raise ExceptionGroup(
                    "unhandled errors in a TaskGroup (1 sub-exception)",
                    [RuntimeError("fpl needs an API key: set FPL_SESSION_COOKIE … (HTTP 403.)")],
                )
            return {"picks": fpl_picks()}
        return call

    done = await execute_approved(call_for=exploding, store=s)
    detail = done[0][1].detail
    assert "FPL_SESSION_COOKIE" in detail
    assert "unhandled errors in a TaskGroup" not in detail

    stored = Store.load(tmp_path).proposals[p.id]
    assert stored.state is State.FAILED
    assert "FPL_SESSION_COOKIE" in stored.outcome
