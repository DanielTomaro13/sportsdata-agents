"""The agent's only way to change a real fantasy team.

The FPL write tools live in the MCP server's `fpl.write` group. This module deliberately
does NOT grant them to the agent. Instead it exposes two native tools that describe an
*intent*, and every intent goes through `fantasy.execute.run_intent` — policy, then
approval, then a single write, then a read-back.

WHY THE MODEL IS NOT GIVEN THE WRITE TOOLS. A policy engine the model can route around is
decoration. If `fpl_set_lineup` is in the model's tool list, then "never play a chip
without asking" is a sentence in a prompt, and prompts lose arguments. Keeping the write
behind a native tool makes the gate structural: there is no token the model can emit that
reaches FPL without passing through `run_intent`.

TWO VALUES ARE NEVER TAKEN FROM THE MODEL, and this is the other half of the design:

* **the deadline** — fetched from `fpl_gameweeks`. If the model supplied it, then every
  timing rule (act only near the deadline, never after it, quiet hours) could be defeated
  by a hallucinated timestamp.
* **the points cost of a transfer** — computed from `fpl_my_team.transfers`. If the model
  supplied it, `max_hit` would be a suggestion. A model that believes a -8 is worth it
  could report it as free and the policy would agree.

Everything else — which players, which captain, why — is exactly what the model is for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sportsdata_agents.agents.harness import ToolDef

PLATFORM = "fpl"


# ─── facts the model is not allowed to assert ───────────────────────────


async def _mcp_call(name: str, args: dict[str, Any]) -> Any:
    from sportsdata_agents.config import get_settings
    from sportsdata_agents.mcp.manager import MCPManager

    # `fpl.write` is named explicitly: it is unreachable through `*` or `fpl.*` by
    # design, so this is the one place in the codebase that opts into it.
    async with MCPManager(
        groups=["fpl.managers", "fpl.reference", "fpl.write"],
        command=get_settings().mcp_command,
    ) as manager:
        return await manager.call_tool(name, args)


async def _next_gameweek() -> tuple[int, datetime]:
    """(event id, deadline) for the gameweek a write would apply to — from FPL, not from
    the model. Every timing rule in the policy depends on this being true."""
    payload = await _mcp_call("fpl_gameweeks", {})
    events = payload.get("events", []) if isinstance(payload, dict) else []
    for event in events:
        if event.get("is_next"):
            return int(event["id"]), _parse(event["deadline_time"])
    # No "next" means the season is over, or FPL is between states. Refusing is correct:
    # a write with no deadline to check against cannot be gated on time at all.
    raise RuntimeError(
        "FPL reports no upcoming gameweek — there is no deadline to act before, so no "
        "write can be timed or verified. Nothing was sent."
    )


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def _transfer_cost(my_team: dict, count: int) -> tuple[int, int, str]:
    """(points cost, free transfers remaining, explanation), computed from FPL's own
    numbers. `count` is how many transfers are being made.

    The pre-deadline 'unlimited' window is handled first because getting it wrong is the
    single most common FPL reasoning error — during it, changes are free and unlimited,
    and quoting a 4-point hit is simply false.
    """
    block = my_team.get("transfers") or {}
    if block.get("status") == "unlimited":
        return 0, count, "before the first deadline — transfers are free and unlimited"

    per_hit = int(block.get("cost") or 4)
    limit = block.get("limit")
    made = int(block.get("made") or 0)
    if limit is None:
        return 0, 0, "FPL did not report a transfer limit — treating these as free"

    free_left = max(0, int(limit) - made)
    chargeable = max(0, count - free_left)
    return (
        chargeable * per_hit,
        free_left,
        f"{free_left} free of {limit} ({made} used); {chargeable} x {per_hit}pt",
    )


# ─── the two tools the agent actually gets ──────────────────────────────


async def fpl_propose_lineup(args: dict[str, Any]) -> Any:
    """{entry, picks, summary, diff?} → policy decision, and a write only if it allows one."""
    from sportsdata_agents.fantasy.execute import Intent, run_intent
    from sportsdata_agents.fantasy.policy import LeaguePolicy, load_policies

    entry = int(args["entry"])
    picks = args["picks"]
    if not isinstance(picks, list) or not picks:
        raise ValueError("picks must be a non-empty list of {element, position, ...}")

    chip = args.get("chip")
    event, deadline = await _next_gameweek()
    key = f"{PLATFORM}:{entry}"
    policy = load_policies().get(key) or LeaguePolicy(platform=PLATFORM, entry=entry)
    now = datetime.now(tz=UTC)

    # A chip rides along with a lineup write, so it must be judged as a chip — never
    # inherited from the lineup's more permissive setting.
    decision = (
        policy.for_chip(str(chip)) if chip
        else policy.for_lineup(now=now, deadline=deadline)
    )

    payload: dict[str, Any] = {"picks": picks}
    if chip:
        payload["chip"] = chip

    outcome = await run_intent(
        Intent(
            action="lineup", entry=entry,
            summary=str(args.get("summary") or f"Set the XI for GW{event}"),
            diff=list(args.get("diff") or []),
            payload=payload, deadline=deadline,
        ),
        decision, call=_call_adapter, csrf=_csrf(),
    )
    return _report(outcome, decision, event, deadline)


async def fpl_propose_transfer(args: dict[str, Any]) -> Any:
    """{entry, transfers, summary, diff?} → policy decision, and a write only if it allows one."""
    from sportsdata_agents.fantasy.execute import Intent, run_intent
    from sportsdata_agents.fantasy.policy import LeaguePolicy, load_policies

    entry = int(args["entry"])
    transfers = args["transfers"]
    if not isinstance(transfers, list):
        raise ValueError("transfers must be a list of {element_in, element_out, ...}")

    chip = args.get("chip")
    event, deadline = await _next_gameweek()
    my_team = await _mcp_call("fpl_my_team", {"managerId": entry})
    if not isinstance(my_team, dict):
        raise RuntimeError("could not read your squad — refusing to transfer blind")

    hit, free_left, how = _transfer_cost(my_team, len(transfers))
    key = f"{PLATFORM}:{entry}"
    policy = load_policies().get(key) or LeaguePolicy(platform=PLATFORM, entry=entry)
    now = datetime.now(tz=UTC)

    decision = (
        policy.for_chip(str(chip)) if chip else
        policy.for_transfer(
            hit=hit, free_transfers=free_left, transfers_used=len(transfers),
            now=now, deadline=deadline,
        )
    )

    payload: dict[str, Any] = {"entry": entry, "event": event, "transfers": transfers}
    if chip:
        payload["chip"] = chip

    outcome = await run_intent(
        Intent(
            action="transfer", entry=entry,
            summary=str(args.get("summary") or f"{len(transfers)} transfer(s) for GW{event}"),
            diff=[*list(args.get("diff") or []), f"cost: {hit}pt — {how}"],
            payload=payload, deadline=deadline, cost_points=hit,
        ),
        decision, call=_call_adapter, csrf=_csrf(),
    )
    result = _report(outcome, decision, event, deadline)
    result["points_cost"] = hit
    result["cost_basis"] = how
    return result


async def fantasy_review_proposals(args: dict[str, Any]) -> Any:
    """What is currently waiting on the owner. Read-only — an agent cannot approve its
    own proposal, which is the entire point of there being one."""
    from sportsdata_agents.fantasy.approvals import Store

    return {
        "pending": [
            {"id": p.id[:8], "action": p.action, "summary": p.summary,
             "cost_points": p.cost_points, "expires_at": p.expires_at}
            for p in Store.load().pending()
        ],
        "note": "Only the owner can approve these: `agents fantasy approve <id>`.",
    }


# ─── plumbing ───────────────────────────────────────────────────────────


async def _call_adapter(tool: str, **kwargs: Any) -> Any:
    return await _mcp_call(tool, kwargs)


def _csrf() -> str:
    """FPL's CSRF token, from the environment or the connect-written config.

    Empty is not an error here — the write will fail with a 403 and be reported as a
    failure, which is more useful than refusing before the policy has even been consulted.
    """
    import os

    if token := os.environ.get("FPL_CSRF_TOKEN"):
        return token
    try:
        from pathlib import Path

        import yaml

        path = Path.home() / ".config" / "sportsdata-mcp" / "config.yaml"
        if path.exists():
            secrets = (yaml.safe_load(path.read_text()) or {}).get("secrets") or {}
            return str(secrets.get("FPL_CSRF_TOKEN") or "")
    except (OSError, ValueError):
        pass
    return ""


def _report(outcome: Any, decision: Any, event: int, deadline: datetime) -> dict[str, Any]:
    """What the model is told afterwards. It says plainly whether the team changed —
    a model that cannot tell "proposed" from "done" will tell the owner the wrong thing."""
    body: dict[str, Any] = {
        "status": outcome.status,
        "team_changed": outcome.changed_anything,
        "policy": decision.reason,
        "detail": outcome.detail,
        "gameweek": event,
        "deadline": deadline.isoformat(),
    }
    if outcome.proposal is not None:
        body["proposal_id"] = outcome.proposal.id[:8]
        body["awaiting_owner"] = outcome.proposal.state.value == "pending"
    if outcome.verification is not None:
        body["verified"] = outcome.verification.ok
        if not outcome.verification.ok:
            body["mismatches"] = outcome.verification.mismatches
    return body


_ENTRY = {"type": "integer", "description": "The manager id of the team to change — the owner's own."}

FANTASY_TOOLS: dict[str, ToolDef] = {
    "fpl_propose_lineup": ToolDef(
        name="fpl_propose_lineup",
        description=(
            "Propose a starting XI, bench order, captain and vice-captain for the owner's FPL team. "
            "This does NOT necessarily change the team: the owner's policy decides whether it is "
            "applied now, sent to them for approval, or refused. Read the returned `status` and "
            "`team_changed` and tell the owner exactly which happened — never claim a change you "
            "were not told about. Send ALL FIFTEEN picks, as fpl_my_team returns them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry": _ENTRY,
                "picks": {
                    "type": "array",
                    "description": (
                        "All 15 picks: {element, position, is_captain, is_vice_captain, multiplier}. "
                        "position 1-11 is the XI in slot order, 12-15 the bench in order; multiplier "
                        "1 normal, 2 captain, 3 triple-captain, 0 bench."
                    ),
                    "items": {"type": "object"},
                },
                "summary": {"type": "string", "description": "One line the owner will see on their phone."},
                "diff": {
                    "type": "array",
                    "description": "The change in human terms, one line each, e.g. 'Salah -> captain'.",
                    "items": {"type": "string"},
                },
                "chip": {
                    "type": "string",
                    "enum": ["bboost", "3xc"],
                    "description": (
                        "A TEAM chip to play with this lineup. Chips are ALWAYS routed to the "
                        "owner for approval, whatever else the policy allows."
                    ),
                },
            },
            "required": ["entry", "picks", "summary"],
        },
        execute=fpl_propose_lineup,
    ),
    "fpl_propose_transfer": ToolDef(
        name="fpl_propose_transfer",
        description=(
            "Propose one or more transfers for the owner's FPL team. This does NOT necessarily "
            "make them: the policy decides. The points cost is computed from the owner's actual "
            "free-transfer count — do not state a cost yourself, read `points_cost` from the result. "
            "`selling_price` must come from fpl_my_team, never from now_cost."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry": _ENTRY,
                "transfers": {
                    "type": "array",
                    "description": (
                        "{element_in, element_out, purchase_price, selling_price} per transfer. "
                        "For a straight buy use element_out: null, selling_price: 0. Send [] to play "
                        "a transfer chip alone."
                    ),
                    "items": {"type": "object"},
                },
                "summary": {"type": "string", "description": "One line the owner will see on their phone."},
                "diff": {
                    "type": "array",
                    "description": "One line per move, e.g. 'OUT Salah (13.0) / IN Saka (10.2)'.",
                    "items": {"type": "string"},
                },
                "chip": {
                    "type": "string",
                    "enum": ["wildcard", "freehit"],
                    "description": "A TRANSFER chip. Chips are ALWAYS routed to the owner for approval.",
                },
            },
            "required": ["entry", "transfers", "summary"],
        },
        execute=fpl_propose_transfer,
    ),
    "fantasy_review_proposals": ToolDef(
        name="fantasy_review_proposals",
        description=(
            "List proposals still waiting on the owner's approval. Read-only — you cannot approve "
            "your own proposal, and should not imply to the owner that you can."
        ),
        parameters={"type": "object", "properties": {}},
        execute=fantasy_review_proposals,
    ),
}
