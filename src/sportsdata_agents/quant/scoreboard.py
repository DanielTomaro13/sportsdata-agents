"""Alert P&L scoreboard: "if you'd staked every printed Kelly, here's the
running profit" — the feedback loop as a measured number, not anecdotes.

Every value alert stores the Kelly stake it PRINTED at fire time (payload
kelly_stake/bankroll), and racing alerts carry their settlement keys
(provider + event id + saddle number). This module grades them:

- ``racing_value`` — settled against ``event_results`` (winner = saddle
  number, the racing results ingestion's convention): a win pays
  ``stake * (odds - 1)``, a loss costs the stake. Alerts whose race has no
  recorded result yet stay PENDING, never guessed.
- ``arb`` — an arb is not a bet on an outcome but on both sides: its "P&L"
  is the locked profit IF both legs were still takeable when re-measured
  (the honesty loop stamps ``outcome.still_arb`` ~5 minutes after firing).
  Vanished arbs count 0, not a loss — you simply couldn't take them.
- other value kinds (model/exchange/stat/prediction) — settlement needs
  per-kind result joins that land with Phase B; they are counted (fired /
  measured-still-value) but not staked in the P&L total yet, and the
  report SAYS so — a scoreboard that quietly skips losses is a lie.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import Alert, EventResult

__all__ = ["alert_pnl", "format_scoreboard"]


async def alert_pnl(
    session: AsyncSession,
    *,
    since: dt.datetime,
    until: dt.datetime | None = None,
) -> dict[str, Any]:
    until = until or dt.datetime.now(dt.UTC)
    alerts = (await session.execute(
        select(Alert).where(Alert.created_at >= since, Alert.created_at < until)
    )).scalars().all()

    racing = {"fired": 0, "settled": 0, "wins": 0, "pending": 0,
              "staked": 0.0, "pnl": 0.0}
    arbs = {"fired": 0, "measured": 0, "still_takeable": 0, "locked_profit": 0.0}
    other: dict[str, dict[str, int]] = {}

    # one query for every result the racing alerts might need
    keys = {(str((a.payload or {}).get("provider", "")),
             str((a.payload or {}).get("event_external_id", "")))
            for a in alerts if a.kind == "racing_value"}
    keys.discard(("", ""))
    results: dict[tuple[str, str], str] = {}
    if keys:
        rows = (await session.execute(
            select(EventResult).where(
                EventResult.event_external_id.in_({e for _p, e in keys})
            )
        )).scalars().all()
        for r in rows:
            results[(r.provider, r.event_external_id)] = str(r.winning_selection)

    for alert in alerts:
        payload = alert.payload or {}
        if alert.kind == "racing_value":
            racing["fired"] += 1
            stake = float(payload.get("kelly_stake") or 0.0)
            number = payload.get("runner_number")
            key = (str(payload.get("provider", "")), str(payload.get("event_external_id", "")))
            winner = results.get(key)
            if not stake or number is None or winner is None:
                racing["pending"] += 1
                continue
            racing["settled"] += 1
            racing["staked"] += stake
            if str(number) == winner:
                racing["wins"] += 1
                racing["pnl"] += stake * (float(payload.get("odds", 0.0)) - 1.0)
            else:
                racing["pnl"] -= stake
        elif alert.kind == "arb":
            arbs["fired"] += 1
            # legacy alerts stored outcome as a plain string — dicts only here
            outcome = payload.get("outcome")
            outcome = outcome if isinstance(outcome, dict) else {}
            if outcome:
                arbs["measured"] += 1
                if outcome.get("still_arb"):
                    arbs["still_takeable"] += 1
                    inv = float(payload.get("sum_inverse") or 0.0)
                    bankroll = float(payload.get("bankroll") or 100.0)
                    if inv > 0:
                        arbs["locked_profit"] += bankroll * (1.0 / inv - 1.0)
        elif alert.kind in ("model_value", "exchange_value", "stat_value",
                            "prediction_value", "value"):
            bucket = other.setdefault(alert.kind, {"fired": 0, "still_value": 0})
            bucket["fired"] += 1
            outcome = payload.get("outcome")
            if isinstance(outcome, dict) and outcome.get("still_value"):
                bucket["still_value"] += 1

    racing["staked"] = round(racing["staked"], 2)
    racing["pnl"] = round(racing["pnl"], 2)
    arbs["locked_profit"] = round(arbs["locked_profit"], 2)
    return {"since": since.isoformat(), "until": until.isoformat(),
            "racing": racing, "arbs": arbs, "other": other}


def format_scoreboard(report: dict[str, Any]) -> str:
    """The weekly push, in plain English with the caveats attached."""
    racing = report["racing"]
    arbs = report["arbs"]
    lines = [":bar_chart: Alert P&L scoreboard (last 7 days)"]
    if racing["settled"]:
        roi = (racing["pnl"] / racing["staked"] * 100.0) if racing["staked"] else 0.0
        lines.append(
            f"Racing: {racing['settled']} settled of {racing['fired']} fired — "
            f"{racing['wins']} won · staked ${racing['staked']:.2f} · "
            f"P&L ${racing['pnl']:+.2f} ({roi:+.1f}% ROI)"
            + (f" · {racing['pending']} pending results" if racing["pending"] else "")
        )
    elif racing["fired"]:
        lines.append(f"Racing: {racing['fired']} fired, all awaiting results")
    if arbs["fired"]:
        lines.append(
            f"Arbs: {arbs['fired']} fired · {arbs['still_takeable']} still takeable "
            f"when re-checked · locked profit if taken ${arbs['locked_profit']:.2f}"
        )
    for kind, stats in sorted(report["other"].items()):
        lines.append(f"{kind}: {stats['fired']} fired · "
                     f"{stats['still_value']} still live when re-checked")
    if len(lines) == 1:
        lines.append("No alerts fired this period.")
    lines.append("_Kelly stakes as printed on each alert; racing settles against "
                 "recorded results; other value kinds join the P&L with Phase B._")
    return "\n".join(lines)
