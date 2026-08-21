"""Tests for the staleness alarm and the run trigger.

An alarm's value is entirely in when it does NOT fire. One that pages on every tick for
three days is muted, and then the real expiry is muted with it — so most of these assert
silence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sportsdata_agents.fantasy.policy import LeaguePolicy
from sportsdata_agents.fantasy.watch import (
    QUIET_BEYOND_HOURS,
    Credential,
    Watch,
    alert_plan,
    alert_text,
    run_due,
    tick,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def plan(state, hours_left, *, last_alert_at="", last_state=""):
    return alert_plan(state, hours_left=hours_left, last_alert_at=last_alert_at,
                      last_alerted_credential=last_state, now=NOW)


# ─── silence ────────────────────────────────────────────────────────────


def test_a_working_credential_says_nothing():
    assert plan(Credential.OK, 2.0)[0] is False


def test_a_transient_failure_never_pages():
    """UNKNOWN is a network blip or an upstream wobble. Alarming on it makes the real
    expiry indistinguishable from a bad afternoon at FPL."""
    assert plan(Credential.UNKNOWN, 1.0)[0] is False


def test_an_expiry_four_days_out_stays_quiet():
    """There is genuinely time. A page now is noise that costs the later page its force."""
    assert plan(Credential.EXPIRED, QUIET_BEYOND_HOURS + 1)[0] is False


def test_it_does_not_repeat_inside_the_cooldown():
    recent = (NOW - timedelta(minutes=30)).isoformat()
    assert plan(Credential.EXPIRED, 48.0, last_alert_at=recent,
                last_state="expired")[0] is False


# ─── noise, when it is earned ───────────────────────────────────────────


def test_it_pages_once_when_the_deadline_comes_into_range():
    """Two days out it speaks, but at ordinary priority — this is a chore, not a siren."""
    should, priority = plan(Credential.EXPIRED, 48.0)
    assert should is True
    assert priority == "default"


def test_urgency_rises_as_the_deadline_approaches():
    assert plan(Credential.EXPIRED, 70.0)[1] == "default"
    assert plan(Credential.EXPIRED, 20.0)[1] == "high"
    assert plan(Credential.EXPIRED, 3.0)[1] == "urgent"


def test_it_repeats_once_the_cooldown_has_passed():
    """Each rung carries its own cooldown: daily at two days out, hourly inside six."""
    day_old = (NOW - timedelta(hours=25)).isoformat()
    assert plan(Credential.EXPIRED, 48.0, last_alert_at=day_old, last_state="expired")[0] is True
    hour_old = (NOW - timedelta(hours=1, minutes=5)).isoformat()
    assert plan(Credential.EXPIRED, 3.0, last_alert_at=hour_old, last_state="expired")[0] is True
    assert plan(Credential.EXPIRED, 3.0,
                last_alert_at=(NOW - timedelta(minutes=20)).isoformat(),
                last_state="expired")[0] is False


def test_a_state_change_speaks_even_inside_the_cooldown():
    """Working → expired is news, whatever we said an hour ago about something else."""
    recent = (NOW - timedelta(minutes=5)).isoformat()
    assert plan(Credential.MISSING, 48.0, last_alert_at=recent, last_state="expired")[0] is True


def test_the_alert_says_what_to_do_about_it():
    text = alert_text(3942695, Credential.EXPIRED, "FPL rejected the cookie", 5.0)
    assert "sportsdata-mcp connect fpl" in text
    assert "inside a day" in text
    assert "3942695" in text


# ─── the run trigger ────────────────────────────────────────────────────


def policy(**kw):
    kw.setdefault("quiet_hours", None)
    return LeaguePolicy(platform="fpl", entry=1, **kw)


def test_it_runs_once_inside_the_window():
    due, why = run_due(policy=policy(), event=2, hours_left=3.0,
                       last_run_event=0, now=NOW)
    assert due is True
    assert "GW2" in why


def test_it_does_not_run_twice_for_one_gameweek():
    """A 30-minute job inside a 6-hour window would otherwise be a dozen billed LLM
    runs per gameweek, all proposing the same thing."""
    due, why = run_due(policy=policy(), event=2, hours_left=3.0,
                       last_run_event=2, now=NOW)
    assert due is False
    assert "already ran" in why


def test_it_does_not_run_outside_the_policy_window():
    due, _ = run_due(policy=policy(act_within_hours_of_deadline=6.0), event=2,
                     hours_left=40.0, last_run_event=0, now=NOW)
    assert due is False


def test_a_widened_window_runs_earlier_with_no_second_setting():
    due, _ = run_due(policy=policy(act_within_hours_of_deadline=48.0), event=2,
                     hours_left=40.0, last_run_event=0, now=NOW)
    assert due is True


def test_it_does_not_run_after_the_deadline():
    due, why = run_due(policy=policy(), event=2, hours_left=-0.5,
                       last_run_event=0, now=NOW)
    assert due is False
    assert "passed" in why


def test_quiet_hours_hold_the_run_rather_than_cancelling_it():
    night = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
    due, why = run_due(policy=policy(quiet_hours=("23:00", "07:00")), event=2,
                       hours_left=3.0, last_run_event=0, now=night)
    assert due is False
    assert "holding" in why


# ─── the tick as a whole ────────────────────────────────────────────────


@pytest.fixture
def watched(monkeypatch, tmp_path):
    from sportsdata_agents.fantasy.policy import save_policy

    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")
    save_policy(LeaguePolicy(platform="fpl", entry=3942695, lineup="auto",
                             quiet_hours=None), tmp_path)
    return tmp_path


async def test_no_policies_means_no_work(monkeypatch, tmp_path):
    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    result = await tick(now=NOW, base=tmp_path, run_agent=False)
    assert result.checked == 0
    assert "nothing to watch" in result.lines[0]


async def test_a_broken_credential_stops_the_agent_running(watched, monkeypatch):
    """The order matters: alert, then refuse to run. Waking an agent that cannot
    authenticate spends money to produce a 403."""
    import sportsdata_agents.fantasy.watch as w

    deadline = NOW + timedelta(hours=3)
    monkeypatch.setattr(w, "_next_gameweek", None, raising=False)
    monkeypatch.setattr("sportsdata_agents.tools.fantasy._next_gameweek",
                        _fake_gw(2, deadline))
    monkeypatch.setattr(w, "check_credential",
                        _fake_cred(Credential.EXPIRED, "FPL rejected the cookie"))
    ran = []
    monkeypatch.setattr(w, "_wake_agent", _record(ran))

    result = await tick(now=NOW, base=watched)
    assert result.alerts == 1
    assert result.runs == 0
    assert ran == []


async def test_a_healthy_credential_inside_the_window_wakes_the_agent(watched, monkeypatch):
    import sportsdata_agents.fantasy.watch as w

    deadline = NOW + timedelta(hours=3)
    monkeypatch.setattr("sportsdata_agents.tools.fantasy._next_gameweek",
                        _fake_gw(2, deadline))
    monkeypatch.setattr(w, "check_credential", _fake_cred(Credential.OK, "session valid"))
    ran = []
    monkeypatch.setattr(w, "_wake_agent", _record(ran))

    result = await tick(now=NOW, base=watched)
    assert result.alerts == 0
    assert result.runs == 1
    assert ran == [(3942695, 2)]

    # …and a second tick does not run it again.
    again = await tick(now=NOW + timedelta(minutes=30), base=watched)
    assert again.runs == 0


async def test_a_failed_agent_run_is_retried_next_tick(watched, monkeypatch):
    """last_run_event is stamped on success only — a crashed run must not silently
    cost the owner the gameweek."""
    import sportsdata_agents.fantasy.watch as w

    monkeypatch.setattr("sportsdata_agents.tools.fantasy._next_gameweek",
                        _fake_gw(2, NOW + timedelta(hours=3)))
    monkeypatch.setattr(w, "check_credential", _fake_cred(Credential.OK, "ok"))

    async def fails(policy, event, deadline):
        return False

    monkeypatch.setattr(w, "_wake_agent", fails)
    await tick(now=NOW, base=watched)
    assert Watch.load(watched).for_key("fpl:3942695").last_run_event == 0


def _fake_gw(event, deadline):
    async def go():
        return event, deadline
    return go


def _fake_cred(state, detail):
    async def go(entry, policy=None):
        return state, detail
    return go


def _record(sink):
    async def go(policy, event, deadline):
        sink.append((policy.entry, event))
        return True
    return go


# ─── the error the alarm nearly missed ──────────────────────────────────


def test_a_wrapped_403_is_still_recognised_as_a_credential_failure():
    """The MCP client runs inside an anyio task group, so a clean 403 arrives wrapped as
    `ExceptionGroup: unhandled errors in a TaskGroup`. Matching on str(e) alone made a
    plainly-expired cookie read as UNKNOWN — and UNKNOWN never pages, so the alarm stayed
    silent for exactly the failure it exists to catch. Found live, 20h before a deadline.
    """
    from sportsdata_agents.fantasy.watch import classify_error, flatten_error

    inner = RuntimeError(
        "tool fpl_my_team failed: fpl needs an API key: set FPL_SESSION_COOKIE in your "
        'environment and restart. (HTTP 403.) {"detail":"Authentication credentials '
        'were not provided."}'
    )
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    text = flatten_error(wrapped)
    assert "FPL_SESSION_COOKIE" in text
    assert classify_error(text)[0] is Credential.MISSING


def test_an_expired_cookie_is_told_apart_from_one_never_configured():
    """Different chores: 'yours went stale' vs 'you never set one up'."""
    from sportsdata_agents.fantasy.watch import classify_error

    assert classify_error('HTTP 403 {"detail":"Authentication credentials were not '
                          'provided."}')[0] is Credential.EXPIRED
    assert classify_error("fpl needs an API key: set FPL_SESSION_COOKIE")[0] is Credential.MISSING


def test_an_unrelated_failure_says_nothing_about_the_credential():
    from sportsdata_agents.fantasy.watch import classify_error

    assert classify_error("ConnectTimeout: upstream did not respond") is None


def test_flatten_survives_a_cyclic_exception_chain():
    from sportsdata_agents.fantasy.watch import flatten_error

    a, b = RuntimeError("outer"), RuntimeError("inner")
    a.__context__, b.__context__ = b, a       # a cycle; the walk must still terminate
    assert "outer" in flatten_error(a)


async def test_an_unreadable_schedule_still_checks_the_credential(monkeypatch, tmp_path):
    """The horizon comes from an AUTHENTICATED endpoint, so the commonest reason it
    fails is the very thing the alarm exists to report. Returning early on that failure
    meant a missing ESPN cookie produced no alert at all — the second time this shape of
    bug hid the alarm from itself. Found live."""
    import sportsdata_agents.fantasy.watch as w
    from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy

    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")
    save_policy(LeaguePolicy(platform="espn", entry=4,
                             context={"leagueId": 1, "seasonId": 2026, "game": "ffl"}), tmp_path)

    async def no_schedule(policy):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(w, "_horizon", no_schedule)
    monkeypatch.setattr(w, "check_credential",
                        _fake_cred(Credential.MISSING, "no ESPN cookie is configured"))
    ran = []
    monkeypatch.setattr(w, "_wake_agent", _record(ran))

    result = await tick(now=NOW, base=tmp_path)
    assert result.alerts == 1, "a broken credential must page even when the schedule is unreadable"
    assert ran == [], "and nothing should be run against a schedule we cannot see"


async def test_a_working_credential_with_no_schedule_runs_nothing_and_stays_quiet(
        monkeypatch, tmp_path):
    """The other half: an unreadable schedule with a healthy credential is a real
    upstream problem, not the owner's chore. Report it, do not page, do not act."""
    import sportsdata_agents.fantasy.watch as w
    from sportsdata_agents.fantasy.policy import LeaguePolicy, save_policy

    monkeypatch.setattr("sportsdata_agents.paths.data_dir", lambda: tmp_path)
    monkeypatch.setenv("FANTASY_ALERT_CHANNEL", "log")
    save_policy(LeaguePolicy(platform="espn", entry=4, lineup="auto", quiet_hours=None,
                             context={"leagueId": 1, "seasonId": 2026, "game": "ffl"}), tmp_path)

    async def no_schedule(policy):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(w, "_horizon", no_schedule)
    monkeypatch.setattr(w, "check_credential", _fake_cred(Credential.OK, "cookie valid"))
    ran = []
    monkeypatch.setattr(w, "_wake_agent", _record(ran))

    result = await tick(now=NOW, base=tmp_path)
    assert result.alerts == 0
    assert result.runs == 0
    assert ran == []
