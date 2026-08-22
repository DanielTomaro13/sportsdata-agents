"""The part that makes it a season rather than a demo.

Two jobs, both cheap and both deterministic — no LLM in this path:

1. **The staleness alarm.** Verify the credential works DAYS before a deadline. The
   plan calls this the highest-value reliability work in the whole build, and the reason
   is the asymmetry: a cookie that expired on Tuesday is a two-minute chore if you learn
   about it on Tuesday, and a lost gameweek if you learn about it at 17:29 on Friday.

2. **The run trigger.** Wake the agent once, inside the window its policy says it may
   act in. Without this the policy engine is real but nothing ever consults it.

THREE THINGS THIS DELIBERATELY DOES NOT DO:

* **It does not nag.** A credential that is fine produces silence. An alarm that fires
  every 30 minutes for three days is one the owner mutes, and then the real one is muted
  too. Urgency scales with time-to-deadline instead: quiet beyond 72h, daily inside it,
  loud inside 24h.
* **It does not re-run the agent on every tick.** Once per gameweek per team. A 30-minute
  job inside a 6-hour act window would otherwise be a dozen LLM runs per gameweek, each
  billed, each proposing the same thing.
* **It does not watch teams nobody asked it to.** The registry is the set of saved
  policies. Setting a policy is the opt-in; there is no separate subscription to forget.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .errors import flatten_error

logger = logging.getLogger(__name__)


class Credential(StrEnum):
    OK = "ok"
    EXPIRED = "expired"       # present but signed out, or rejected
    MISSING = "missing"       # never configured
    UNKNOWN = "unknown"       # the check itself failed (network, upstream down)


# How loud to be, by hours remaining before the deadline. First match wins.
#
# The ladder exists because "your cookie is broken" means something different four days
# out (a chore) than it does four hours out (a lost gameweek), and one alert tone for
# both trains the owner to ignore the one that matters.
LADDER: tuple[tuple[float, str, float], ...] = (
    #  hours_left <=, ntfy priority, re-alert no more often than (hours)
    (6.0,   "urgent",  1.0),
    (24.0,  "high",    6.0),
    (72.0,  "default", 24.0),
)
#: Beyond the last rung, log and stay silent — there is genuinely time.
QUIET_BEYOND_HOURS = 72.0


@dataclass
class WatchState:
    """What we already told the owner, so we do not tell them again."""

    last_alert_at: str = ""
    last_alerted_credential: str = ""
    last_run_event: int = 0
    last_checked_at: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> WatchState:
        return cls(**{k: v for k, v in d.items() if k in cls.__annotations__})


@dataclass
class Watch:
    """Per-team watch state, keyed the same way policies are (`platform:entry`)."""

    path: Path
    teams: dict[str, WatchState] = field(default_factory=dict)

    @classmethod
    def load(cls, base: Path | None = None) -> Watch:
        from ..paths import data_dir

        path = (base or data_dir()) / "fantasy-watch.json"
        w = cls(path=path)
        if path.exists():
            raw = json.loads(path.read_text())
            w.teams = {k: WatchState.from_dict(v) for k, v in raw.items()}
        return w

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: asdict(v) for k, v in self.teams.items()}, indent=2))

    def for_key(self, key: str) -> WatchState:
        return self.teams.setdefault(key, WatchState())


# ─── is the credential actually usable? ─────────────────────────────────


def classify_error(text: str) -> tuple[Credential, str] | None:
    """Map an upstream failure to a credential state, or None when it says nothing
    about the credential."""
    # MISSING is checked FIRST: the "set FPL_SESSION_COOKIE" error also carries a 403,
    # and "you never configured it" is a different chore from "yours went stale".
    if "FPL_SESSION_COOKIE" in text or "needs an API key" in text:
        return Credential.MISSING, "no FPL session cookie is configured"
    if "403" in text or "Authentication credentials" in text or "credentials were not" in text:
        return Credential.EXPIRED, "FPL rejected the session cookie (403)"
    return None


async def check_credential(entry: int, policy=None) -> tuple[Credential, str]:
    """Ask the platform, through the SAME path the agent would use.

    Deliberately not a bespoke request to some cheap `/me/` endpoint: a check that
    exercises a different code path than the write can pass while the write fails. FPL is
    checked with `fpl_my_team` and ESPN with `espnfantasy_rosters` — in both cases the
    call a change reads first.
    """
    platform = getattr(policy, "platform", "fpl")
    if platform == "espn":
        return await _check_espn(entry, policy)
    if platform == "mfl":
        return await _check_mfl(entry, policy)

    from ..tools.fantasy import _mcp_call

    try:
        body = await _mcp_call("fpl_my_team", {"managerId": entry})
    except BaseException as e:
        text = flatten_error(e)
        if (verdict := classify_error(text)) is not None:
            return verdict
        # An upstream wobble is NOT an expiry. Calling it one would cry wolf and, worse,
        # would make the real expiry indistinguishable from a bad afternoon at FPL.
        return Credential.UNKNOWN, f"could not check: {type(e).__name__}: {text[:160]}"

    if isinstance(body, dict) and body.get("picks"):
        return Credential.OK, f"session valid — {len(body['picks'])} picks readable"
    return Credential.EXPIRED, "the session answered but returned no squad"


async def _check_espn(entry: int, policy) -> tuple[Credential, str]:
    """ESPN's cookie pair, verified against the actual league.

    This matters MORE than FPL's, not less. An `espn_s2` lasts about a year, which means
    it fails exactly once per season, silently, at a moment nobody chose — and a league
    you can no longer read is indistinguishable from a league you were removed from
    unless something checks on purpose.
    """
    from ..tools.espn_fantasy import _mcp_call

    ctx = getattr(policy, "context", {}) or {}
    missing = [k for k in ("leagueId", "seasonId", "game") if not ctx.get(k)]
    if missing:
        return Credential.UNKNOWN, f"policy is missing {', '.join(missing)} — cannot check"
    try:
        body = await _mcp_call("espnfantasy_rosters", {
            "game": ctx["game"], "seasonId": int(ctx["seasonId"]),
            "leagueId": int(ctx["leagueId"]), "view": ["mRoster"],
        })
    except BaseException as e:
        text = flatten_error(e)
        if (verdict := classify_espn_error(text)) is not None:
            return verdict
        return Credential.UNKNOWN, f"could not check: {type(e).__name__}: {text[:160]}"

    teams = (body or {}).get("teams") or [] if isinstance(body, dict) else []
    if not teams:
        return Credential.EXPIRED, "ESPN answered but returned no teams"
    if not any(int(t.get("id", -1)) == entry for t in teams):
        # Readable but not ours: a real state, and a different chore from an expiry.
        return Credential.EXPIRED, (
            f"league readable but team {entry} is not in it — check the teamId, or you "
            "may have been removed from the league"
        )
    return Credential.OK, f"cookie valid — {len(teams)} teams readable"


async def _check_mfl(entry: int, policy) -> tuple[Credential, str]:
    """MyFantasyLeague's cookie, verified the way MFL itself proves identity.

    `myleagues` is the call that answers "who are you" — and MFL's THIRD way of saying no
    lives here: a bad or missing cookie returns {"leagues": {}} with HTTP 200 and no error
    field at all. Treating that as success is how a dead credential gets reported healthy,
    so an empty league list is read as signed out.
    """
    from ..tools.mfl_fantasy import _mcp_call

    ctx = getattr(policy, "context", {}) or {}
    if not ctx.get("year"):
        return Credential.UNKNOWN, "policy is missing `year` — cannot check"
    try:
        body = await _mcp_call("mfl_my_leagues", {"year": int(ctx["year"])})
    except BaseException as e:
        text = flatten_error(e)
        if "MFL_COOKIE" in text or "needs an API key" in text:
            return Credential.MISSING, "no MFL cookie is configured"
        if "error" in text.lower() and ("cookie" in text.lower() or "login" in text.lower()):
            return Credential.EXPIRED, "MFL rejected the cookie"
        return Credential.UNKNOWN, f"could not check: {type(e).__name__}: {text[:160]}"

    leagues = ((body or {}).get("leagues") or {}).get("league") if isinstance(body, dict) else None
    rows = leagues if isinstance(leagues, list) else ([leagues] if leagues else [])
    if not rows:
        return Credential.EXPIRED, (
            "signed out — MFL returned no leagues for this cookie (it answers 200 with an "
            "empty list rather than an error). Log in at myfantasyleague.com and re-run "
            "`sportsdata-mcp connect mfl`"
        )
    want = str(ctx.get("leagueId", ""))
    if want and not any(str(r.get("league_id")) == want for r in rows if isinstance(r, dict)):
        return Credential.EXPIRED, (
            f"cookie works but league {want} is not among the {len(rows)} it can see — "
            "check the league id, or you may have been removed"
        )
    return Credential.OK, f"cookie valid — {len(rows)} league(s) visible"


def classify_espn_error(text: str) -> tuple[Credential, str] | None:
    """ESPN's typed error bodies, which say precisely which failure this is."""
    if "ESPN_FANTASY_COOKIE" in text or "needs an API key" in text:
        return Credential.MISSING, "no ESPN cookie is configured"
    if "AUTH_LEAGUE_NOT_VISIBLE" in text or "not authorized to view this League" in text:
        return Credential.EXPIRED, "ESPN will not show this league — the cookie is stale or you lost access"
    if "AUTH_MISSING_CREDENTIALS" in text or "Credentials are missing" in text:
        return Credential.MISSING, "ESPN received no credentials"
    if "401" in text or "Unauthorized" in text:
        return Credential.EXPIRED, "ESPN rejected the cookie (401)"
    return None


# ─── how loud, and how often ────────────────────────────────────────────


def alert_plan(
    state: Credential, *, hours_left: float, last_alert_at: str,
    last_alerted_credential: str, now: datetime,
) -> tuple[bool, str]:
    """(should_alert, ntfy priority).

    UNKNOWN never pages on its own. A network blip at 3am is not the owner's problem, and
    an alarm that fires on transient failures is one they will mute before the real
    expiry ever happens.
    """
    if state is Credential.OK or state is Credential.UNKNOWN:
        return False, "default"
    if hours_left > QUIET_BEYOND_HOURS:
        return False, "default"

    priority, cooldown_h = "urgent", 1.0
    for ceiling, prio, cool in LADDER:
        if hours_left <= ceiling:
            priority, cooldown_h = prio, cool
            break

    # A state CHANGE always speaks, whatever the cooldown — going from working to
    # expired is news even if we alerted an hour ago about something else.
    if last_alerted_credential and last_alerted_credential != state.value:
        return True, priority
    if not last_alert_at:
        return True, priority
    since = (now - datetime.fromisoformat(last_alert_at)).total_seconds() / 3600
    return since >= cooldown_h, priority


def alert_text(entry: int, state: Credential, detail: str, hours_left: float,
               platform: str = "fpl") -> str:
    urgency = (
        "the deadline is inside a day" if hours_left <= 24
        else f"{hours_left:.0f}h until the deadline"
    )
    connector = {"fpl": "fpl", "espn": "espnfantasy"}.get(platform, platform)
    return (
        f"{platform.upper()} credential {state.value} for team {entry} — {urgency}.\n"
        f"  {detail}\n"
        f"  Your agent cannot set the lineup or move players until this is fixed.\n"
        f"  Fix it with:  sportsdata-mcp connect {connector}"
    )


# ─── should the agent run at all? ───────────────────────────────────────


def run_due(
    *, policy, event: int, hours_left: float, last_run_event: int, now: datetime,
) -> tuple[bool, str]:
    """Whether to wake the agent for this gameweek.

    Runs ONCE per gameweek. The window is the policy's own `act_within_hours_of_deadline`,
    so an owner who widens it gets an earlier run and nobody has two settings to keep in
    sync.
    """
    if last_run_event >= event:
        return False, f"already ran for GW{event}"
    if hours_left <= 0:
        return False, "the deadline has passed"
    if hours_left > policy.act_within_hours_of_deadline:
        return False, f"{hours_left:.1f}h out; window opens at {policy.act_within_hours_of_deadline}h"
    if policy._in_quiet_hours(now):
        # Held, not skipped: the next tick outside quiet hours picks it up, and the
        # window is hours wide, so a night-time deadline is not silently missed.
        return False, "inside quiet hours — holding until the window reopens"
    return True, f"GW{event} deadline in {hours_left:.1f}h"


# ─── the tick the scheduler calls ───────────────────────────────────────


@dataclass
class TickResult:
    checked: int = 0
    alerts: int = 0
    runs: int = 0
    lines: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)
        logger.info("fantasy watch: %s", line)


#: Stand-in for "we could not find out how long there is". Deliberately inside the
#: alerting range: not knowing how long you have is not a reason to assume you have
#: plenty, and the usual cause of not knowing is the credential itself.
UNKNOWN_HORIZON_HOURS = 12.0

#: Which agent manages which platform. Also the definition of "watchable": a team is
#: watched when there is something that could act on it.
AGENTS = {"fpl": "fpl_manager", "espn": "espn_manager", "mfl": "mfl_manager"}

#: Don't re-check the credential on every tick — it is an authenticated upstream call.
#: Far from a deadline a daily check is plenty; close to one it is checked every tick,
#: because that is when a stale answer is the expensive one.
CHECK_INTERVAL_HOURS = 12.0


async def tick(*, now: datetime | None = None, base: Path | None = None,
               run_agent: bool = True) -> TickResult:
    """One pass: verify credentials, alert if needed, wake the agent if it is due."""
    from .policy import LeaguePolicy, load_policies

    now = now or datetime.now(tz=UTC)
    result = TickResult()
    policies = load_policies(base)
    if not policies:
        result.say("no teams have a policy — nothing to watch")
        return result

    watch = Watch.load(base)
    result.say(f"{len(policies)} team(s) watched")

    # Anything approved out-of-band — from a phone, another shell — is carried out here.
    # `agents fantasy approve` also executes immediately, so this is the safety net
    # rather than the main path; an approval queue that only drains on one code path is
    # one that silently stops draining.
    if run_agent:
        try:
            from .runner import drain_approved

            for prop, outcome in await drain_approved():
                result.runs += 1
                result.say(f"approved {prop.id[:8]} → {outcome.status}: {outcome.detail}")
        except Exception as e:
            result.say(f"could not execute approved proposals: {type(e).__name__}: {e}")

    for key, policy in policies.items():
        if policy.platform not in AGENTS:
            # Derived, not a third hardcoded list: a platform is watchable exactly when
            # it has a manager agent to wake. Adding one should not mean remembering to
            # edit a tuple over here as well.
            result.say(f"{key}: no manager agent for {policy.platform} — not watched")
            continue
        st = watch.for_key(key)
        result.checked += 1

        # The horizon is PER TEAM, not per tick. FPL has one global deadline; an ESPN
        # league has its own scoring period, and two ESPN leagues need not agree. Reading
        # one clock and applying it to every policy is how a team gets acted on against
        # another league's schedule.
        # A FAILED horizon lookup must NOT skip the credential check. The horizon comes
        # from an authenticated endpoint, so the commonest reason it fails is the very
        # thing the alarm exists to report — and returning early here meant a missing
        # ESPN cookie produced no alert at all. Found live, and the second time this
        # shape of bug has hidden the alarm from itself.
        event = None
        deadline = None
        try:
            event, deadline = await _horizon(policy)
            hours_left = (deadline - now).total_seconds() / 3600
        except BaseException as e:
            hours_left = UNKNOWN_HORIZON_HOURS
            result.say(f"{key}: could not read the schedule "
                       f"({type(e).__name__}: {flatten_error(e)[:120]}) — checking the "
                       "credential anyway, which is usually the cause")

        if _due_for_check(st, now, hours_left,
                          policy.platform in LeaguePolicy.HARD_DEADLINE):
            state, detail = await check_credential(policy.entry, policy)
            st.last_checked_at = now.isoformat(timespec="seconds")
            should, priority = alert_plan(
                state, hours_left=hours_left, last_alert_at=st.last_alert_at,
                last_alerted_credential=st.last_alerted_credential, now=now,
            )
            if should:
                await _page(alert_text(policy.entry, state, detail, hours_left,
                                       policy.platform), priority)
                st.last_alert_at = now.isoformat(timespec="seconds")
                result.alerts += 1
                result.say(f"{key}: ALERTED ({state.value}, {priority}) — {detail}")
            else:
                result.say(f"{key}: {state.value} — {detail}")
            # Recorded whether or not we alerted, so a recovery is seen as a change
            # next time rather than as more of the same.
            st.last_alerted_credential = state.value
            if state is not Credential.OK:
                watch.save()
                continue  # a broken credential cannot run an agent

        if event is None or deadline is None:
            result.say(f"{key}: not running — no schedule to act against")
            watch.save()
            continue

        due, why = run_due(policy=policy, event=event, hours_left=hours_left,
                           last_run_event=st.last_run_event, now=now)
        if not due:
            result.say(f"{key}: not running — {why}")
            continue
        result.say(f"{key}: RUNNING — {why}")
        if run_agent:
            ok = await _wake_agent(policy, event, deadline)
            if ok:
                # Only stamped on success, so a crashed run is retried next tick rather
                # than silently costing the owner a gameweek.
                st.last_run_event = event
                result.runs += 1
            else:
                result.say(f"{key}: the agent run failed — will retry next tick")
        watch.save()

    watch.save()
    return result


async def _horizon(policy) -> tuple[int, datetime]:
    """(period id, the moment decisions must beat) for ONE team.

    FPL's is a hard deadline the API states outright. ESPN has no single lock — a fantasy
    week rolls and each player locks at his own kickoff — so the current scoring period
    plus a short horizon stands in, which keeps the agent acting near kickoff where the
    team news actually is.
    """
    if policy.platform == "espn":
        from ..tools.espn_fantasy import _scoring_period

        return await _scoring_period(policy.context)
    if policy.platform == "mfl":
        from ..tools.mfl_fantasy import _week_and_horizon

        return await _week_and_horizon(policy.context)
    from ..tools.fantasy import _next_gameweek

    return await _next_gameweek()


def _due_for_check(st: WatchState, now: datetime, hours_left: float,
                   hard_deadline: bool = True) -> bool:
    """Whether to spend an authenticated call verifying the credential this tick.

    `hard_deadline` matters: a platform without one has a horizon of `now + N`, so
    "hours_left <= 24" is permanently true and the throttle never applies — ESPN was
    being checked every 30 minutes, forever. There the ordinary interval governs.
    """
    if hard_deadline and hours_left <= 24:
        return True
    if not st.last_checked_at:
        return True
    since = (now - datetime.fromisoformat(st.last_checked_at)).total_seconds() / 3600
    return since >= CHECK_INTERVAL_HOURS


async def _page(text: str, priority: str) -> bool:
    from ..observability.notify import ntfy_url_for, post_ntfy, push_to_channel
    from .approvals import alert_channel

    channel = alert_channel()
    if channel == "ntfy" or channel.startswith("ntfy:"):
        return await post_ntfy(ntfy_url_for(channel) or "", text, priority=priority)
    return await push_to_channel(channel, text)


def _prompt_for(policy, event: int, deadline: datetime) -> str:
    if policy.platform == "espn":
        ctx = policy.context
        return (
            f"Scoring period {event} is live in ESPN league {ctx.get('leagueId')} "
            f"({ctx.get('game')} {ctx.get('seasonId')}), and you have been woken because "
            f"decisions are due by {deadline.isoformat()}. Manage team {policy.entry}: "
            f"read the league settings for the slot map, the rosters, the matchups and "
            f"the free agents, then set the best legal lineup you can justify and make "
            f"any add/drop clearly worth it. Use espn_propose_lineup and "
            f"espn_propose_add_drop — report exactly what the policy did with each, and "
            f"never claim a change unless team_changed came back true."
        )
    if policy.platform == "mfl":
        ctx = policy.context
        return (
            f"Week {event} in MyFantasyLeague league {ctx.get('leagueId')} "
            f"({ctx.get('year')}), and the next kickoff is {deadline.isoformat()} — after "
            f"that those players lock. Manage franchise {policy.entry}: read mfl_league "
            f"for THIS league's starter rules, then the roster, the projections and the "
            f"free agents, and set the best legal lineup you can justify. Remember the "
            f"lineup is a FULL REPLACEMENT — send every starter. Use mfl_propose_lineup, "
            f"mfl_propose_add_drop and mfl_propose_blind_bid, report exactly what the "
            f"policy did with each, and never claim a change unless team_changed came "
            f"back true."
        )
    return (
        f"The GW{event} deadline is {deadline.isoformat()} — that is soon, which is why "
        f"you have been woken. Review team {policy.entry}: read the gameweek, the squad, "
        f"the fixtures and the players, then set the best XI and captain you can justify, "
        f"and make any transfer that is clearly worth it. Use fpl_propose_lineup and "
        f"fpl_propose_transfer — report exactly what the policy did with each, and do not "
        f"claim a change unless team_changed came back true."
    )


async def _wake_agent(policy, event: int, deadline: datetime) -> bool:
    """Run the platform's manager for one team. The agent decides WHAT; the policy plane
    it calls through decides whether any of it happens."""
    import asyncio
    import sys

    agent = AGENTS.get(policy.platform)
    if agent is None:
        logger.warning("no manager agent for platform %s", policy.platform)
        return False
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "sportsdata_agents.interfaces.cli",
        "run", "--agent", agent, _prompt_for(policy, event, deadline),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("%s run failed rc=%s: %s", agent, proc.returncode,
                       (out or b"").decode(errors="ignore")[-400:])
        return False
    return True
