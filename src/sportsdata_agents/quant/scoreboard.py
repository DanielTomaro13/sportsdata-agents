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
- ``prediction_value`` — settled against Kalshi/Polymarket resolutions
  (the nightly results run records the resolved outcome label per event):
  win when the backed outcome is the resolved winner.
- ``exchange_value`` / ``model_value`` — h2h outcomes settle against the
  fixture's recorded winner, translated between the result book's listing
  order and the fixture's (the backtester's rule); totals and derivative
  markets stay pending until Phase B's score-based settlement.
- ``stat_value`` — needs player-stat actuals (no results source yet);
  counted, and the report SAYS so — a scoreboard that quietly skips
  losses is a lie.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import Alert, Event, EventResult, Fixture

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
    value = {"fired": 0, "settled": 0, "wins": 0, "pending": 0,
             "staked": 0.0, "pnl": 0.0}  # prediction + exchange/model h2h
    other: dict[str, dict[str, int]] = {}
    racing_buckets = {"thin": {"settled": 0, "staked": 0.0, "pnl": 0.0},
                      "liquid": {"settled": 0, "staked": 0.0, "pnl": 0.0}}

    # Results are recorded under ONE provider's race ids while alerts carry
    # the FLAGGED book's — a direct (provider, event) lookup would leave most
    # books' alerts pending forever. The resolver maps every book's event onto
    # a shared fixture, so settlement joins THROUGH the fixture (the same
    # pattern the backtester settles predictions with).
    keys = {(str((a.payload or {}).get("provider", "")),
             str((a.payload or {}).get("event_external_id", "")))
            for a in alerts if a.kind == "racing_value"}
    keys.discard(("", ""))
    results: dict[tuple[str, str], str] = {}
    fixture_by_key: dict[tuple[str, str], Any] = {}
    result_by_fixture: dict[Any, str] = {}
    if keys:
        ext_ids = {e for _p, e in keys}
        mappings = (await session.execute(
            select(Event).where(Event.external_id.in_(ext_ids),
                                Event.fixture_id.is_not(None))
        )).scalars().all()
        for m in mappings:
            if (m.provider, m.external_id) in keys:
                fixture_by_key[(m.provider, m.external_id)] = m.fixture_id
        siblings: list[Event] = []
        if fixture_by_key:
            siblings = list((await session.execute(
                select(Event).where(
                    Event.fixture_id.in_(set(fixture_by_key.values())))
            )).scalars().all())
        sibling_fixture = {(s.provider, s.external_id): s.fixture_id for s in siblings}
        all_ext = ext_ids | {e for _p, e in sibling_fixture}
        rows = (await session.execute(
            select(EventResult).where(EventResult.event_external_id.in_(all_ext))
        )).scalars().all()
        for r in rows:
            selection = str(r.winning_selection)
            results[(r.provider, r.event_external_id)] = selection
            fixture = sibling_fixture.get((r.provider, r.event_external_id))
            if fixture is not None and selection.isdigit():
                result_by_fixture.setdefault(fixture, selection)

    # prediction resolutions: (kalshi|polymarket, event id) -> winner label
    pred_keys = set()
    for a in alerts:
        if a.kind != "prediction_value":
            continue
        pl = a.payload or {}
        back = str(pl.get("back", "")).lower()
        event = str(pl.get(f"{back}_event", ""))
        if back and event:
            pred_keys.add((back, event))
    if pred_keys:
        rows = (await session.execute(
            select(EventResult).where(
                EventResult.provider.in_({p for p, _e in pred_keys}),
                EventResult.event_external_id.in_({e for _p, e in pred_keys}))
        )).scalars().all()
        for r in rows:
            results[(r.provider, r.event_external_id)] = str(r.winning_selection)

    # fixture winners for exchange/model h2h: result-frame side -> fixture frame
    fixture_ids = set()
    for a in alerts:
        if a.kind == "exchange_value" and (a.payload or {}).get("fixture_id"):
            with contextlib.suppress(ValueError):
                fixture_ids.add(uuid.UUID(str(a.payload["fixture_id"])))
    h2h_winner: dict[Any, str] = {}
    if fixture_ids:
        from sportsdata_agents.quant.backtest import _translate_side

        fixtures = {f.id: f for f in (await session.execute(
            select(Fixture).where(Fixture.id.in_(fixture_ids)))).scalars().all()}
        siblings2 = (await session.execute(
            select(Event).where(Event.fixture_id.in_(fixture_ids)))).scalars().all()
        by_ext: dict[str, list[Any]] = {}
        for s2 in siblings2:
            by_ext.setdefault(s2.external_id, []).append(s2)
        if by_ext:
            res_rows = (await session.execute(
                select(EventResult).where(
                    EventResult.event_external_id.in_(set(by_ext))))).scalars().all()
            for r in res_rows:
                side = str(r.winning_selection)
                if side not in ("home", "away", "draw"):
                    continue
                result_name = str((r.meta or {}).get("event_name") or "")
                for s2 in by_ext.get(r.event_external_id, []):
                    if s2.provider != r.provider or s2.fixture_id in h2h_winner:
                        continue
                    fx = fixtures.get(s2.fixture_id)
                    if fx is None or not result_name:
                        continue
                    translated = _translate_side(side, fx.name, result_name)
                    if translated:
                        h2h_winner[s2.fixture_id] = translated

    for alert in alerts:
        payload = alert.payload or {}
        if alert.kind == "racing_value":
            racing["fired"] += 1
            stake = float(payload.get("kelly_stake") or 0.0)
            number = payload.get("runner_number")
            key = (str(payload.get("provider", "")), str(payload.get("event_external_id", "")))
            winner: str | None = results.get(key)
            if winner is None:
                winner = result_by_fixture.get(fixture_by_key.get(key))
            # a mis-merged fixture carrying a league-style winner ("home") must
            # leave the alert pending, never grade it a loss
            if not stake or number is None or winner is None or not winner.isdigit():
                racing["pending"] += 1
                continue
            racing["settled"] += 1
            racing["staked"] += stake
            thin = (payload.get("exchange_matched") or 0) < 1500
            bucket_key = "thin" if thin else "liquid"
            b = racing_buckets[bucket_key]
            b["staked"] += stake
            if str(number) == winner:
                racing["wins"] += 1
                won = stake * (float(payload.get("odds", 0.0)) - 1.0)
                racing["pnl"] += won
                b["pnl"] += won
            else:
                racing["pnl"] -= stake
                b["pnl"] -= stake
            b["settled"] += 1
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
        elif alert.kind == "prediction_value":
            value["fired"] += 1
            stake = float(payload.get("kelly_stake") or 0.0)
            back = str(payload.get("back", "")).lower()
            event = str(payload.get(f"{back}_event", ""))
            winner = results.get((back, event))
            outcome_label = str(payload.get("outcome", "")).lower().strip()
            if not stake or not winner or not outcome_label:
                value["pending"] += 1
                continue
            value["settled"] += 1
            value["staked"] += stake
            if winner == outcome_label:
                value["wins"] += 1
                value["pnl"] += stake * (float(payload.get("back_odds", 0.0)) - 1.0)
            else:
                value["pnl"] -= stake
        elif alert.kind == "exchange_value":
            value["fired"] += 1
            stake = float(payload.get("kelly_stake") or 0.0)
            outcome_label = str(payload.get("outcome", ""))
            fixture_key = None
            with contextlib.suppress(ValueError):
                fixture_key = uuid.UUID(str(payload.get("fixture_id", "")))
            winner = h2h_winner.get(fixture_key)
            if (not stake or winner is None
                    or outcome_label not in ("home", "away", "draw")):
                value["pending"] += 1
                continue
            value["settled"] += 1
            value["staked"] += stake
            if winner == outcome_label:
                value["wins"] += 1
                value["pnl"] += stake * (float(payload.get("odds", 0.0)) - 1.0)
            else:
                value["pnl"] -= stake
        elif alert.kind in ("model_value", "stat_value", "value", "back_lay"):
            bucket = other.setdefault(alert.kind, {"fired": 0, "still_value": 0})
            bucket["fired"] += 1
            outcome = payload.get("outcome")
            if isinstance(outcome, dict) and outcome.get("still_value"):
                bucket["still_value"] += 1

    racing["staked"] = round(racing["staked"], 2)
    racing["pnl"] = round(racing["pnl"], 2)
    value["staked"] = round(value["staked"], 2)
    value["pnl"] = round(value["pnl"], 2)
    arbs["locked_profit"] = round(arbs["locked_profit"], 2)
    report = {"since": since.isoformat(), "until": until.isoformat(),
              "racing": racing, "arbs": arbs, "value": value, "other": other,
              "racing_buckets": racing_buckets}
    report["suggestions"] = tuning_suggestions(report)
    return report


def _roi(stats: dict[str, Any]) -> float | None:
    staked = float(stats.get("staked") or 0.0)
    return (float(stats["pnl"]) / staked * 100.0) if staked else None


def tuning_suggestions(report: dict[str, Any]) -> list[str]:
    """Data-driven threshold advice from SETTLED outcomes — suggestions only,
    the operator changes params via update_watch; nothing self-modifies. Every
    rule needs a minimum sample so one bad weekend can't retune the system."""
    out: list[str] = []
    racing = report["racing"]
    if racing["settled"] >= 10:
        roi = _roi(racing)
        if roi is not None and roi < -5.0:
            out.append(f"racing ROI {roi:+.0f}% over {racing['settled']} settled — "
                       "consider raising racing_value min_edge_pct")
    buckets = report.get("racing_buckets") or {}
    thin, liquid = buckets.get("thin", {}), buckets.get("liquid", {})
    thin_roi, liquid_roi = _roi(thin), _roi(liquid)
    if (thin.get("settled", 0) >= 5 and thin_roi is not None and thin_roi < 0.0
            and (liquid_roi is None or liquid_roi >= 0.0)):
        out.append(f"racing on thin Betfair markets (<$1.5k matched) is losing "
                   f"({thin_roi:+.0f}% over {thin['settled']}) while liquid ones "
                   "aren't — consider raising racing_value min_matched to 1500")
    value = report.get("value") or {}
    if value.get("settled", 0) >= 10:
        roi = _roi(value)
        if roi is not None and roi < -5.0:
            out.append(f"prediction/exchange value ROI {roi:+.0f}% over "
                       f"{value['settled']} settled — consider raising "
                       "min_edge_pct or min_volume on those watches")
    return out


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
    value = report.get("value") or {}
    if value.get("settled"):
        roi = (value["pnl"] / value["staked"] * 100.0) if value["staked"] else 0.0
        lines.append(
            f"Value (prediction/exchange): {value['settled']} settled of "
            f"{value['fired']} fired — {value['wins']} won · "
            f"staked ${value['staked']:.2f} · P&L ${value['pnl']:+.2f} ({roi:+.1f}% ROI)"
            + (f" · {value['pending']} pending" if value.get("pending") else "")
        )
    elif value.get("fired"):
        lines.append(f"Value (prediction/exchange): {value['fired']} fired, awaiting results")
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
    for tip in report.get("suggestions") or []:
        lines.append(f":wrench: {tip}")
    lines.append("_Kelly stakes as printed; racing, prediction and exchange h2h "
                 "settle against recorded results; stat/model derivatives join "
                 "with Phase B's score-based settlement._")
    return "\n".join(lines)
