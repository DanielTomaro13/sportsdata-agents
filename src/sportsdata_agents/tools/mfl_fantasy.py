"""The MFL agent's only route to a real roster.

Same shape as the FPL and ESPN tools: `myfantasyleague.write` is never granted to a
spec, so every change passes through `run_intent` — policy, approval, one write,
read-back.

WHAT IS DIFFERENT ABOUT MFL, and why each difference is handled here rather than left to
the model:

* **A lineup write is a FULL REPLACEMENT.** `STARTERS` is the entire starting lineup;
  anyone omitted is benched, and MFL does not complain. So the tool refuses a lineup that
  does not satisfy the league's own declared `starters` requirements — read from the
  league, never assumed, because MFL leagues are famously non-standard.
* **`REPLACE` is not the default on waivers.** Omit it and claims are APPENDED to
  whatever is already queued, which is how the same claim gets submitted twice. This
  module always sends it explicitly.
* **A blind bid spends real money.** The bid is checked against what the league says is
  left before the policy sees it.
* **`FRANCHISE_ID` means "act as another franchise"** and is commissioner-only. It is
  never sent. An agent acting on its owner's team is acting as itself, and passing this
  is the one way to rewrite a stranger's roster by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sportsdata_agents.agents.harness import ToolDef

PLATFORM = "mfl"

#: MFL has no single lock either — each NFL game locks its own players. The horizon is
#: the next kickoff among the roster where that is knowable, and this when it is not.
DEFAULT_HORIZON_HOURS = 12.0


async def _mcp_call(name: str, args: dict[str, Any]) -> Any:
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.mcp.manager import MCPManager

    async with MCPManager(
        groups=["myfantasyleague.league", "myfantasyleague.reference",
                "myfantasyleague.scoring", "myfantasyleague.mine",
                "myfantasyleague.write"],
        command=get_settings().mcp_command,
    ) as manager:
        return await manager.call_tool(name, args)


def _policy_for(entry: int, league_id: str):
    from sportsdata_agents.fantasy.policy import load_policies

    key = f"{PLATFORM}:{league_id}:{entry}"
    policy = load_policies().get(key)
    if policy is None:
        raise RuntimeError(
            f"no policy for {key} — run `agents fantasy policy {entry} --platform mfl "
            f"--set context.leagueId={league_id} --set context.year=<season>` first. The "
            "policy is where this team's league and season live; without it there is "
            "nothing to write to."
        )
    return policy


async def _week_and_horizon(ctx: dict) -> tuple[int, datetime]:
    """(week, the moment decisions must beat) — from MFL, never from the model.

    BOTH VALUES COME FROM `nflSchedule`, and the reason is a bug worth remembering. The
    league export has no `currentWeek` field — it carries `startWeek` and `endWeek` only
    — so reading the week from there fell through to `startWeek` and the agent would have
    written a WEEK 1 lineup every week of the season, silently, forever. `nflSchedule`
    with no week argument is documented to return the current one, and it is the only
    endpoint that does.

    The horizon is the NEXT KICKOFF still in the future. NFL has no single lock: each
    player freezes when his own game starts, so the honest deadline is the moment the
    first of them does. It also advances by itself through the week — Thursday night,
    then Sunday early, then Sunday late — so it never goes stale and never goes negative,
    which a fixed weekly deadline would do every Friday.
    """
    now = datetime.now(tz=UTC)
    try:
        schedule = await _mcp_call("mfl_nfl_schedule", {"year": int(ctx["year"])})
    except Exception as e:
        raise RuntimeError(
            f"MFL did not return an NFL schedule, so there is no week to write against "
            f"and no kickoff to act before. Nothing was sent. ({type(e).__name__})"
        ) from e

    block = (schedule or {}).get("nflSchedule") if isinstance(schedule, dict) else None
    if not isinstance(block, dict) or block.get("week") is None:
        raise RuntimeError(
            "MFL's NFL schedule carried no week — refusing to guess which week to set. "
            "Writing the wrong week is accepted silently and does nothing."
        )
    week = int(ctx.get("week") or block["week"])

    kickoffs = sorted(
        k for k in (_kickoff(m) for m in _as_list(block.get("matchup"))) if k and k > now
    )
    if kickoffs:
        return week, kickoffs[0]
    # Every game this week has started. There is no lock left to beat, so fall back to a
    # short horizon rather than a past one — a negative countdown reads as "the deadline
    # passed", which would stop the agent acting on next week's roster.
    return week, now + timedelta(hours=DEFAULT_HORIZON_HOURS)


def _kickoff(matchup: object) -> datetime | None:
    if not isinstance(matchup, dict):
        return None
    try:
        return datetime.fromtimestamp(int(matchup["kickoff"]), tz=UTC)
    except (KeyError, TypeError, ValueError):
        return None


def _as_list(value: object) -> list:
    """MFL returns one row as an object and many as a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def starter_requirements(league: dict) -> tuple[int | None, list[str]]:
    """(how many starters, the per-position rules) as THIS league declares them.

    MFL leagues are highly customisable — superflex, two quarterbacks, idp, no kicker.
    A hardcoded formation would be wrong more often than right, so the check below reads
    what the league itself says.
    """
    # A league that allows a partial lineup has no required count — enforcing one would
    # refuse a legal lineup. Verified present on a real league: partialLineupAllowed is
    # "NO" there, and the count is exact.
    if str((league or {}).get("partialLineupAllowed", "NO")).upper() in ("YES", "1", "TRUE"):
        return None, []
    starters = (league or {}).get("starters") or {}
    count = starters.get("count")
    rules = []
    positions = starters.get("position")
    for p in (positions if isinstance(positions, list) else [positions] if positions else []):
        if isinstance(p, dict):
            rules.append(f"{p.get('name')}: {p.get('limit')}")
    try:
        return (int(count) if count is not None else None), rules
    except (TypeError, ValueError):
        # `count` can be a range like "8-9" in leagues with flexible lineups; there is no
        # single number to check against, so the count check is skipped rather than
        # guessed at.
        return None, rules


async def mfl_propose_lineup(args: dict[str, Any]) -> Any:
    """{entry, leagueId, starters, summary} → policy decision, write only if allowed."""
    from sportsdata_agents.fantasy.execute import Intent, run_intent

    entry, league_id = int(args["entry"]), str(args["leagueId"])
    starters = args["starters"]
    if not isinstance(starters, list) or not starters:
        raise ValueError("starters must be a non-empty list of MFL player ids")

    policy = _policy_for(entry, league_id)
    ctx = {**policy.context, "teamId": entry}
    week, horizon = await _week_and_horizon(ctx)
    ctx["week"] = week

    # A full-replacement write against a league whose rules we have already read: check
    # the count here rather than discovering it from a silently-benched player later.
    league = await _mcp_call("mfl_league", {"year": int(ctx["year"]), "L": league_id})
    required, rules = starter_requirements((league or {}).get("league") or {})
    if required is not None and len(starters) != required:
        raise ValueError(
            f"this league starts {required} players and you supplied {len(starters)}. "
            f"STARTERS is a FULL REPLACEMENT — anyone omitted is benched, and MFL accepts "
            f"it silently. Positions: {', '.join(rules) or 'see mfl_league'}"
        )

    decision = policy.for_lineup(now=datetime.now(tz=UTC), deadline=horizon)
    outcome = await run_intent(
        Intent(
            action="lineup", entry=entry, platform=PLATFORM, context=ctx,
            summary=str(args.get("summary") or f"Set the week {week} lineup"),
            diff=list(args.get("diff") or []),
            payload={"STARTERS": [str(s) for s in starters]},
            deadline=horizon,
        ),
        decision, call=_call_adapter,
    )
    return _report(outcome, decision, week)


async def mfl_propose_add_drop(args: dict[str, Any]) -> Any:
    """{entry, leagueId, add?, drop?, summary} → an IMMEDIATE add/drop, policy permitting."""
    from sportsdata_agents.fantasy.execute import Intent, run_intent

    entry, league_id = int(args["entry"]), str(args["leagueId"])
    add, drop = args.get("add"), [str(d) for d in (args.get("drop") or [])]
    if not add and not drop:
        raise ValueError("nothing to do — supply `add`, `drop`, or both")

    policy = _policy_for(entry, league_id)
    ctx = {**policy.context, "teamId": entry}
    week, horizon = await _week_and_horizon(ctx)
    ctx["week"] = week

    payload: dict[str, Any] = {"_tool": "mfl_add_drop"}
    if add:
        payload["ADD"] = str(add)
    if drop:
        payload["DROP"] = drop

    # An immediate move costs no points and no budget, so the stake is zero — the policy
    # still governs WHETHER it happens, via the transfers mode.
    decision = policy.for_transfer(
        hit=0, free_transfers=0, transfers_used=1,
        now=datetime.now(tz=UTC), deadline=horizon,
    )
    outcome = await run_intent(
        Intent(
            action="transfer", entry=entry, platform=PLATFORM, context=ctx,
            summary=str(args.get("summary") or "Add/drop"),
            diff=[*list(args.get("diff") or []),
                  "IMMEDIATE — there is no window to cancel this"],
            payload=payload, deadline=horizon,
        ),
        decision, call=_call_adapter,
    )
    return _report(outcome, decision, week)


async def mfl_propose_blind_bid(args: dict[str, Any]) -> Any:
    """{entry, leagueId, bids, summary} → queued FAAB bids, policy permitting."""
    from sportsdata_agents.fantasy.execute import Intent, run_intent

    entry, league_id = int(args["entry"]), str(args["leagueId"])
    bids = args["bids"]
    if not isinstance(bids, list) or not bids:
        raise ValueError("bids must be a non-empty list of {add, amount, drop?}")

    policy = _policy_for(entry, league_id)
    ctx = {**policy.context, "teamId": entry}
    week, horizon = await _week_and_horizon(ctx)
    ctx["week"] = week

    total = sum(int(b.get("amount", 0)) for b in bids)
    # `0000` is MFL's "dropping nobody" sentinel, and omitting the field entirely is a
    # different request than saying so explicitly.
    picks = ",".join(
        f"{b['add']}_{int(b.get('amount', 0))}_{b.get('drop') or '0000'}" for b in bids
    )

    decision = policy.for_transfer(
        hit=total, free_transfers=0, transfers_used=len(bids),
        now=datetime.now(tz=UTC), deadline=horizon,
    )
    outcome = await run_intent(
        Intent(
            action="transfer", entry=entry, platform=PLATFORM, context=ctx,
            summary=str(args.get("summary") or f"{len(bids)} blind bid(s), {total} total"),
            diff=[*list(args.get("diff") or []),
                  f"BLIND BID — spends up to {total} of budget if the claims win",
                  "queued: the roster does not change until the round processes"],
            # REPLACE is always explicit. Without it MFL APPENDS to the queue, which is
            # how the same claim gets submitted twice by a job that runs more than once.
            payload={"_tool": "mfl_blind_bid", "PICKS": picks, "REPLACE": 1},
            deadline=horizon, cost_points=total,
        ),
        decision, call=_call_adapter,
    )
    result = _report(outcome, decision, week)
    result["total_bid"] = total
    return result


async def _call_adapter(tool: str, **kwargs: Any) -> Any:
    return await _mcp_call(tool, kwargs)


def _report(outcome: Any, decision: Any, week: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": outcome.status,
        "team_changed": outcome.changed_anything,
        "policy": decision.reason,
        "detail": outcome.detail,
        "week": week,
    }
    if outcome.proposal is not None:
        body["proposal_id"] = outcome.proposal.id[:8]
        body["awaiting_owner"] = outcome.proposal.state.value == "pending"
    if outcome.verification is not None:
        body["verified"] = outcome.verification.ok
        if not outcome.verification.ok:
            body["mismatches"] = outcome.verification.mismatches
    return body


_ENTRY = {"type": "integer", "description": "YOUR franchise number in this league (1 for franchise 0001)."}
_LEAGUE = {"type": "string", "description": "The league id, from the league URL."}

MFL_TOOLS: dict[str, ToolDef] = {
    "mfl_propose_lineup": ToolDef(
        name="mfl_propose_lineup",
        description=(
            "Propose a starting lineup on a MyFantasyLeague team. This does NOT necessarily "
            "change the roster: the owner's policy decides. STARTERS IS A FULL REPLACEMENT — "
            "send every starter, because anyone omitted is benched and MFL accepts that "
            "silently. The count is checked against the league's own `starters` rules before "
            "anything is sent. Read `status` and `team_changed` and report exactly which "
            "happened; never claim a change you were not told about."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry": _ENTRY,
                "leagueId": _LEAGUE,
                "starters": {
                    "type": "array",
                    "description": "EVERY starting player id — the complete lineup, not a delta.",
                    "items": {"type": "string"},
                },
                "summary": {"type": "string", "description": "One line the owner will see."},
                "diff": {"type": "array", "description": "The change in human terms.",
                         "items": {"type": "string"}},
            },
            "required": ["entry", "leagueId", "starters", "summary"],
        },
        execute=mfl_propose_lineup,
    ),
    "mfl_propose_add_drop": ToolDef(
        name="mfl_propose_add_drop",
        description=(
            "Propose an IMMEDIATE add and/or drop on a MyFantasyLeague team (first-come, "
            "first-served). Unlike a waiver claim this happens at once and CANNOT BE "
            "CANCELLED, so prefer a blind bid where the league uses one. An add on a full "
            "roster must be paired with a drop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry": _ENTRY,
                "leagueId": _LEAGUE,
                "add": {"type": "string", "description": "One player id to add."},
                "drop": {"type": "array", "description": "Player ids to drop.",
                         "items": {"type": "string"}},
                "summary": {"type": "string", "description": "One line the owner will see."},
                "diff": {"type": "array", "description": "One line per move.",
                         "items": {"type": "string"}},
            },
            "required": ["entry", "leagueId", "summary"],
        },
        execute=mfl_propose_add_drop,
    ),
    "mfl_propose_blind_bid": ToolDef(
        name="mfl_propose_blind_bid",
        description=(
            "Propose blind-bid (FAAB) waiver claims on a MyFantasyLeague team. Bids are "
            "queued and SPEND REAL BUDGET if they win; the roster does not change until the "
            "round processes, so an unchanged roster straight afterwards is not a failure. "
            "Bids always REPLACE the round's existing queue rather than appending, so running "
            "twice does not submit the same claim twice. Do not state a total yourself — read "
            "`total_bid` back from the result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry": _ENTRY,
                "leagueId": _LEAGUE,
                "bids": {
                    "type": "array",
                    "description": (
                        "In priority order: {add: playerId, amount: dollars, drop: playerId}. "
                        "Omit `drop` to drop nobody."
                    ),
                    "items": {"type": "object"},
                },
                "summary": {"type": "string", "description": "One line the owner will see."},
                "diff": {"type": "array", "description": "One line per bid.",
                         "items": {"type": "string"}},
            },
            "required": ["entry", "leagueId", "bids", "summary"],
        },
        execute=mfl_propose_blind_bid,
    ),
}
