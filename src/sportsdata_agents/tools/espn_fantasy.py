"""The ESPN agent's only route to a real roster.

Same shape as the FPL tools and for the same reason: the raw `espnfantasy.write` group is
never granted to a spec, so there is no token the model can emit that reaches ESPN without
passing through `run_intent` — policy, then approval, then a single write, then read-back.

WHAT IS DIFFERENT ABOUT ESPN, and why each difference is handled here rather than left to
the model:

* **A team is (league, season, game, teamId).** That identity comes from the stored
  policy, never from the model — a model that mistypes a league id writes to a stranger's
  team.
* **`scoringPeriodId` decides which week a lineup applies to.** It is read from ESPN, not
  supplied: a lineup set against last week's period is accepted and does nothing.
* **Waivers spend real budget.** `bidAmount` is checked against the team's remaining FAAB
  before the policy sees it, so `max_hit` governs actual money-shaped stakes rather than
  a number the model asserted.
* **There is no captain and no fixed formation.** Slot legality is the league's own,
  read from its settings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sportsdata_agents.agents.harness import ToolDef

PLATFORM = "espn"

#: ESPN has no deadline endpoint the way FPL does — a fantasy week rolls over rather than
#: locking at one instant, and individual players lock when their own game starts. The
#: policy's timing rules still need *a* horizon, so the end of the current scoring period
#: stands in for one. Deliberately conservative: a shorter horizon means the agent acts
#: closer to kickoff, which is where the information is.
DEFAULT_HORIZON_HOURS = 12.0


async def _mcp_call(name: str, args: dict[str, Any]) -> Any:
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.mcp.manager import MCPManager

    # `espnfantasy.write` is named explicitly: it is unreachable through `*` or
    # `espnfantasy.*` by design, so this is the one place that opts into it.
    async with MCPManager(
        groups=["espnfantasy.league", "espnfantasy.reference", "espnfantasy.players",
                "espnfantasy.write"],
        command=get_settings().mcp_command,
    ) as manager:
        return await manager.call_tool(name, args)


def _policy_for(entry: int, league_id: int):
    """The stored policy, which is also where the team's identity lives.

    Refusing when there is none is deliberate: an ESPN team cannot be addressed without a
    league and season, and inventing them is how you write to the wrong roster.
    """
    from sportsdata_agents.fantasy.policy import load_policies

    key = f"{PLATFORM}:{league_id}:{entry}"
    policy = load_policies().get(key)
    if policy is None:
        raise RuntimeError(
            f"no policy for {key} — run `agents fantasy policy {entry} --platform espn "
            f"--set context.leagueId={league_id} …` first. The policy is where this "
            "team's league, season and game are recorded; without it there is nothing "
            "to write to."
        )
    return policy


async def _scoring_period(ctx: dict) -> tuple[int, datetime]:
    """(current scoring period, a horizon to time decisions against) — from ESPN."""
    body = await _mcp_call("espnfantasy_status", {
        "game": ctx["game"], "seasonId": int(ctx["seasonId"]),
        "leagueId": int(ctx["leagueId"]),
    })
    status = (body or {}).get("status") if isinstance(body, dict) else None
    period = (status or {}).get("latestScoringPeriod") if isinstance(status, dict) else None
    if period is None:
        raise RuntimeError(
            "ESPN did not report a scoring period, so there is no week to write against "
            "and no horizon to time the decision by. Nothing was sent."
        )
    return int(period), datetime.now(tz=UTC) + timedelta(hours=DEFAULT_HORIZON_HOURS)


async def espn_propose_lineup(args: dict[str, Any]) -> Any:
    """{entry, leagueId, items, summary, diff?} → policy decision, write only if allowed."""
    from sportsdata_agents.fantasy.execute import Intent, run_intent

    entry, league_id = int(args["entry"]), int(args["leagueId"])
    items = args["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list of {playerId, fromLineupSlotId, toLineupSlotId}")

    policy = _policy_for(entry, league_id)
    ctx = dict(policy.context)
    period, horizon = await _scoring_period(ctx)
    ctx["scoringPeriodId"] = period

    decision = policy.for_lineup(now=datetime.now(tz=UTC), deadline=horizon)
    outcome = await run_intent(
        Intent(
            action="lineup", entry=entry, platform=PLATFORM, context=ctx,
            summary=str(args.get("summary") or f"Set the lineup for week {period}"),
            diff=list(args.get("diff") or []),
            payload={"items": [{**i, "type": "LINEUP"} for i in items]},
            deadline=horizon,
        ),
        decision, call=_call_adapter,
    )
    return _report(outcome, decision, period)


async def espn_propose_add_drop(args: dict[str, Any]) -> Any:
    """{entry, leagueId, add?, drop?, summary, bidAmount?} → policy decision, write only if allowed."""
    from sportsdata_agents.fantasy.execute import Intent, run_intent

    entry, league_id = int(args["entry"]), int(args["leagueId"])
    add, drop = args.get("add") or [], args.get("drop") or []
    if not add and not drop:
        raise ValueError("nothing to do — supply `add`, `drop`, or both")

    policy = _policy_for(entry, league_id)
    ctx = dict(policy.context)
    period, horizon = await _scoring_period(ctx)
    ctx["scoringPeriodId"] = period

    kind = str(args.get("type") or "FREEAGENT").upper()
    bid = int(args.get("bidAmount") or 0)
    if kind == "WAIVER" and bid:
        await _check_budget(ctx, entry, bid)

    items = ([{"playerId": int(p), "type": "ADD", "toTeamId": entry} for p in add]
             + [{"playerId": int(p), "type": "DROP", "fromTeamId": entry} for p in drop])
    payload: dict[str, Any] = {"type": kind, "items": items}
    if kind == "WAIVER" and bid:
        payload["bidAmount"] = bid

    # A waiver bid is the stake, so it is what `max_hit` governs — and it is computed
    # here, never taken from the model's own summary of what it is about to spend.
    decision = policy.for_transfer(
        hit=bid, free_transfers=0, transfers_used=len(items),
        now=datetime.now(tz=UTC), deadline=horizon,
    )
    outcome = await run_intent(
        Intent(
            action="transfer", entry=entry, platform=PLATFORM, context=ctx,
            summary=str(args.get("summary") or f"{len(add)} in, {len(drop)} out"),
            diff=[*list(args.get("diff") or []),
                  f"{kind.lower()}{f', bid {bid}' if bid else ''}"],
            payload=payload, deadline=horizon, cost_points=bid,
        ),
        decision, call=_call_adapter,
    )
    result = _report(outcome, decision, period)
    result["bid_amount"] = bid
    return result


async def _check_budget(ctx: dict, entry: int, bid: int) -> None:
    """Refuse a bid the team cannot cover, before the policy is even consulted.

    A rejected over-bid is only an error message; the reason to catch it here is that a
    model reasoning about a budget it has not read will happily bid 40 out of 12.
    """
    body = await _mcp_call("espnfantasy_teams", {
        "game": ctx["game"], "seasonId": int(ctx["seasonId"]),
        "leagueId": int(ctx["leagueId"]),
    })
    for team in (body or {}).get("teams", []) if isinstance(body, dict) else []:
        if int(team.get("id", -1)) != entry:
            continue
        remaining = team.get("waiverBudgetRemaining")
        if remaining is not None and bid > int(remaining):
            raise ValueError(
                f"bid of {bid} exceeds the {remaining} of waiver budget left. Nothing "
                "was sent — a rejected claim still costs you the player."
            )
    return None


async def _call_adapter(tool: str, **kwargs: Any) -> Any:
    return await _mcp_call(tool, kwargs)


def _report(outcome: Any, decision: Any, period: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": outcome.status,
        "team_changed": outcome.changed_anything,
        "policy": decision.reason,
        "detail": outcome.detail,
        "scoring_period": period,
    }
    if outcome.proposal is not None:
        body["proposal_id"] = outcome.proposal.id[:8]
        body["awaiting_owner"] = outcome.proposal.state.value == "pending"
    if outcome.verification is not None:
        body["verified"] = outcome.verification.ok
        if not outcome.verification.ok:
            body["mismatches"] = outcome.verification.mismatches
    return body


_ENTRY = {"type": "integer", "description": "YOUR team id in this league (the teamId in your team URL)."}
_LEAGUE = {"type": "integer", "description": "The league id (from the league URL)."}

ESPN_FANTASY_TOOLS: dict[str, ToolDef] = {
    "espn_propose_lineup": ToolDef(
        name="espn_propose_lineup",
        description=(
            "Propose a lineup change on an ESPN fantasy team — move players between starting "
            "slots and the bench. This does NOT necessarily change the roster: the owner's "
            "policy decides whether it is applied, sent for approval, or refused. Read `status` "
            "and `team_changed` and report exactly which happened; never claim a change you were "
            "not told about. Slot ids are the LEAGUE'S OWN — read them from "
            "espnfantasy_league_settings, never assume, because flex and IR slots differ per "
            "sport and per league."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry": _ENTRY,
                "leagueId": _LEAGUE,
                "items": {
                    "type": "array",
                    "description": (
                        "One entry per player MOVING: {playerId, fromLineupSlotId, toLineupSlotId}. "
                        "Players staying put are omitted."
                    ),
                    "items": {"type": "object"},
                },
                "summary": {"type": "string", "description": "One line the owner will see on their phone."},
                "diff": {"type": "array", "description": "The change in human terms, one line each.",
                         "items": {"type": "string"}},
            },
            "required": ["entry", "leagueId", "items", "summary"],
        },
        execute=espn_propose_lineup,
    ),
    "espn_propose_add_drop": ToolDef(
        name="espn_propose_add_drop",
        description=(
            "Propose adding and/or dropping players on an ESPN fantasy team. FREEAGENT takes an "
            "unowned player now; WAIVER queues a claim and, in a FAAB league, SPENDS the bid if it "
            "wins. An add on a full roster must be paired with a drop in the same call. The bid is "
            "checked against your actual remaining budget before the policy sees it — do not state "
            "a budget yourself, read `bid_amount` back from the result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry": _ENTRY,
                "leagueId": _LEAGUE,
                "add": {"type": "array", "description": "playerIds to add.", "items": {"type": "integer"}},
                "drop": {"type": "array", "description": "playerIds to drop.", "items": {"type": "integer"}},
                "type": {"type": "string", "enum": ["FREEAGENT", "WAIVER"],
                         "description": "FREEAGENT is immediate; WAIVER is a claim processed later."},
                "bidAmount": {"type": "integer", "description": "FAAB bid, WAIVER only. Real budget."},
                "summary": {"type": "string", "description": "One line the owner will see."},
                "diff": {"type": "array", "description": "One line per move.", "items": {"type": "string"}},
            },
            "required": ["entry", "leagueId", "summary"],
        },
        execute=espn_propose_add_drop,
    ),
}
