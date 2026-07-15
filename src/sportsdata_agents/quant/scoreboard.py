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
- ``exchange_value`` — h2h outcomes settle against the fixture's recorded
  winner, translated between the result book's listing order and the
  fixture's (the backtester's rule).
- ``model_value`` / ``value`` — h2h, totals and lines settle against the
  result's SCORE ("H-A" in the result meta, frame-translated onto the
  fixture): a total is the sum, a line is the margin, a landed line is a
  push. Graded at a FLAT $1 stake — the proving verdict reads hit-rate and
  flat-stake ROI, not compounding Kelly fictions.
- ``bsp_value`` — racing form edges settle exactly like racing_value
  (winner = saddle number through the fixture join).
- ``stat_value`` — needs player-stat actuals (no results source yet);
  counted, and the report SAYS so — a scoreboard that quietly skips
  losses is a lie.

Settled sports alerts also get CLOSING-LINE VALUE: the last recorded price
change before the fixture's start for the same (book, market, selection),
CLV% = alert price / closing price - 1. CLV converges to truth in dozens
of samples where P&L needs hundreds.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import Alert, Event, EventResult, Fixture, Price

__all__ = ["alert_pnl", "format_scoreboard"]


def _maybe_float(value: Any) -> float | None:
    """float(value) or None — payload fields are user-shaped JSON."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _grade(market: str, selection: str, line: float | None,
           home: int, away: int) -> str | None:
    """win/loss/push for a normalised sports selection against a final score;
    None when the market shape isn't gradable from a score."""
    if market == "h2h":
        winner = ("home" if home > away else "away" if away > home else "draw")
        if selection == winner:
            return "win"
        return "push" if winner == "draw" and selection in ("home", "away") else "loss"
    if line is None:
        return None
    if market == "total":
        total = float(home + away)
        if total == line:
            return "push"
        over = total > line
        return "win" if (selection == "over") == over else "loss"
    if market == "line" and selection in ("home", "away"):
        side_margin = float(home - away) if selection == "home" else float(away - home)
        landed = side_margin + line
        if landed == 0:
            return "push"
        return "win" if landed > 0 else "loss"
    return None


async def _fixture_scores(
    session: AsyncSession, fixture_ids: set[Any]
) -> dict[Any, tuple[int, int]]:
    """Final scores per fixture, translated into the FIXTURE's home/away frame
    (a result book listing the teams the other way round swaps the score)."""
    import re as _re

    from sportsdata_agents.quant.backtest import _translate_side

    if not fixture_ids:
        return {}
    fixtures = {f.id: f for f in (await session.execute(
        select(Fixture).where(Fixture.id.in_(fixture_ids)))).scalars().all()}
    siblings = (await session.execute(
        select(Event).where(Event.fixture_id.in_(fixture_ids)))).scalars().all()
    by_ext: dict[str, list[Any]] = {}
    for s in siblings:
        by_ext.setdefault(s.external_id, []).append(s)
    scores: dict[Any, tuple[int, int]] = {}
    if not by_ext:
        return scores
    score_re = _re.compile(r"^(\d+)\s*-\s*(\d+)$")
    rows = (await session.execute(
        select(EventResult).where(
            EventResult.event_external_id.in_(set(by_ext))))).scalars().all()
    for r in rows:
        meta = r.meta or {}
        match = score_re.match(str(meta.get("score") or "").strip())
        result_name = str(meta.get("event_name") or "")
        if not match or not result_name:
            continue
        h, a = int(match.group(1)), int(match.group(2))
        for s in by_ext.get(r.event_external_id, []):
            if s.provider != r.provider or s.fixture_id in scores:
                continue
            fx = fixtures.get(s.fixture_id)
            if fx is None:
                continue
            translated = _translate_side("home", fx.name, result_name)
            if translated == "home":
                scores[s.fixture_id] = (h, a)
            elif translated == "away":
                scores[s.fixture_id] = (a, h)
    return scores


async def _closing_odds(
    session: AsyncSession, *, book: str, event_id: str, market: str,
    selection: str, start: dt.datetime | None,
) -> float | None:
    """The last recorded price CHANGE before the start — the closing line.
    None when the start is unknown or nothing was recorded pre-start."""
    if start is None:
        return None
    row = (await session.execute(
        select(Price.odds).where(
            Price.event_external_id == event_id,
            Price.book == book,
            Price.market == market,
            Price.selection == selection,
            Price.changed_at <= start,
        ).order_by(Price.changed_at.desc()).limit(1)
    )).scalar()
    return float(row) if row is not None else None


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

    racing: dict[str, Any] = {"fired": 0, "settled": 0, "wins": 0, "pending": 0,
                              "staked": 0.0, "pnl": 0.0}
    arbs = {"fired": 0, "measured": 0, "still_takeable": 0, "locked_profit": 0.0}
    value: dict[str, Any] = {"fired": 0, "settled": 0, "wins": 0, "pending": 0,
                             "staked": 0.0, "pnl": 0.0}  # prediction + exchange h2h
    # flat-$1 graded sections: hit-rate + flat-stake ROI + CLV per kind
    def _flat() -> dict[str, Any]:
        return {"fired": 0, "settled": 0, "wins": 0, "pushes": 0, "pending": 0,
                "staked": 0.0, "pnl": 0.0, "clv_n": 0, "clv_sum": 0.0}
    flat: dict[str, dict[str, Any]] = {"model_value": _flat(), "value": _flat()}
    bsp: dict[str, Any] = {"fired": 0, "settled": 0, "wins": 0, "pending": 0,
                           "staked": 0.0, "pnl": 0.0}
    other: dict[str, dict[str, int]] = {}
    racing_buckets = {"thin": {"settled": 0, "staked": 0.0, "pnl": 0.0},
                      "liquid": {"settled": 0, "staked": 0.0, "pnl": 0.0},
                      # no Betfair depth stamped at all (consensus alerts) —
                      # binning them "thin" skewed the tuning advice
                      "no_exchange": {"settled": 0, "staked": 0.0, "pnl": 0.0}}
    # one row per SETTLED bet (pushes excluded — a returned stake carries no
    # information): feeds both the significance layer and the attribution
    # breakdown. section keys: racing / bsp / value / model_value / value_flat.
    bet_rows: list[dict[str, Any]] = []

    def _record(section: str, *, stake: float, pnl: float, odds: float,
                edge: float | None = None, book: str | None = None) -> None:
        bet_rows.append({"section": section, "stake": stake, "pnl": pnl,
                         "odds": odds, "edge": edge, "book": book})

    # Results are recorded under ONE provider's race ids while alerts carry
    # the FLAGGED book's — a direct (provider, event) lookup would leave most
    # books' alerts pending forever. The resolver maps every book's event onto
    # a shared fixture, so settlement joins THROUGH the fixture (the same
    # pattern the backtester settles predictions with).
    keys = {(str((a.payload or {}).get("provider", "")),
             str((a.payload or {}).get("event_external_id", "")))
            for a in alerts if a.kind in ("racing_value", "bsp_value")}
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

    # score-settled kinds: model_value carries its fixture in event_key;
    # `value` alerts map through the events table
    score_fixture_of: dict[str, Any] = {}  # alert id -> fixture uuid
    value_event_ids = set()
    for a in alerts:
        pl = a.payload or {}
        if a.kind == "model_value":
            with contextlib.suppress(ValueError):
                score_fixture_of[str(a.id)] = uuid.UUID(str(pl.get("event_key", "")))
        elif a.kind == "value" and pl.get("event_external_id"):
            value_event_ids.add(str(pl["event_external_id"]))
    if value_event_ids:
        for ev in (await session.execute(
                select(Event).where(Event.external_id.in_(value_event_ids),
                                    Event.fixture_id.is_not(None)))).scalars():
            for a in alerts:
                if (a.kind == "value" and str(a.id) not in score_fixture_of
                        and str((a.payload or {}).get("event_external_id", ""))
                        == ev.external_id):
                    score_fixture_of[str(a.id)] = ev.fixture_id
    score_fixtures = set(score_fixture_of.values())
    scores = await _fixture_scores(session, score_fixtures)
    fixture_start: dict[Any, dt.datetime | None] = {}
    if score_fixtures:
        for fid, start in (await session.execute(
                select(Fixture.id, Fixture.start_time)
                .where(Fixture.id.in_(score_fixtures)))).all():
            fixture_start[fid] = start
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
            # leave the alert pending, never grade it a loss — and odds ≤ 1.0
            # are a recording bug, not a price: pending, not a loss-sized win
            odds = float(payload.get("odds", 0.0))
            if (not stake or number is None or winner is None
                    or not winner.isdigit() or odds <= 1.0):
                racing["pending"] += 1
                continue
            racing["settled"] += 1
            racing["staked"] += stake
            matched = payload.get("exchange_matched")
            bucket_key = ("no_exchange" if matched is None
                          else "thin" if float(matched) < 1500 else "liquid")
            b = racing_buckets[bucket_key]
            b["staked"] += stake
            runners_pl = payload.get("runners") or []
            top_edge = (_maybe_float(runners_pl[0].get("edge_pct"))
                        if runners_pl and isinstance(runners_pl[0], dict) else None)
            if str(number) == winner:
                racing["wins"] += 1
                won = stake * (odds - 1.0)
                racing["pnl"] += won
                b["pnl"] += won
                _record("racing", stake=stake, pnl=won, odds=odds, edge=top_edge)
            else:
                racing["pnl"] -= stake
                b["pnl"] -= stake
                _record("racing", stake=stake, pnl=-stake, odds=odds, edge=top_edge)
            b["settled"] += 1
        elif alert.kind == "bsp_value":
            bsp["fired"] += 1
            runners = payload.get("runners") or []
            top_runner = runners[0] if runners else {}
            stake = float(top_runner.get("kelly_stake") or 0.0)
            number = top_runner.get("number")
            odds = float(top_runner.get("back") or 0.0)
            key = (str(payload.get("provider", "")),
                   str(payload.get("event_external_id", "")))
            winner = results.get(key)
            if winner is None:
                winner = result_by_fixture.get(fixture_by_key.get(key))
            if (not stake or number is None or winner is None
                    or not str(winner).isdigit() or odds <= 1.0):
                bsp["pending"] += 1
                continue
            bsp["settled"] += 1
            bsp["staked"] += stake
            bsp_edge = _maybe_float(top_runner.get("edge_pct"))
            if str(number) == str(winner):
                bsp["wins"] += 1
                bsp["pnl"] += stake * (odds - 1.0)
                _record("bsp", stake=stake, pnl=stake * (odds - 1.0), odds=odds,
                        edge=bsp_edge)
            else:
                bsp["pnl"] -= stake
                _record("bsp", stake=stake, pnl=-stake, odds=odds, edge=bsp_edge)
        elif alert.kind == "arb":
            arbs["fired"] += 1
            # legacy alerts stored outcome as a plain string — dicts only here
            outcome = payload.get("outcome")
            outcome = outcome if isinstance(outcome, dict) else {}
            if outcome:
                arbs["measured"] += 1
                if outcome.get("still_arb"):
                    arbs["still_takeable"] += 1
                    bankroll = float(payload.get("bankroll") or 100.0)
                    # credit the RE-MEASURED margin (what was still takeable
                    # five minutes on), not the fire-time one
                    margin_after = outcome.get("margin_pct_after")
                    if margin_after is not None:
                        arbs["locked_profit"] += bankroll * float(margin_after) / 100.0
                    else:  # legacy alerts without a re-measured margin
                        inv = float(payload.get("sum_inverse") or 0.0)
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
            back_odds = float(payload.get("back_odds", 0.0))
            if winner == outcome_label:
                value["wins"] += 1
                value["pnl"] += stake * (back_odds - 1.0)
                _record("value", stake=stake, pnl=stake * (back_odds - 1.0),
                        odds=back_odds, book=back)
            else:
                value["pnl"] -= stake
                _record("value", stake=stake, pnl=-stake, odds=back_odds, book=back)
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
            ex_odds = float(payload.get("odds", 0.0))
            ex_edge = _maybe_float(payload.get("edge_pct"))
            if winner == outcome_label:
                value["wins"] += 1
                value["pnl"] += stake * (ex_odds - 1.0)
                _record("value", stake=stake, pnl=stake * (ex_odds - 1.0),
                        odds=ex_odds, edge=ex_edge)
            else:
                value["pnl"] -= stake
                _record("value", stake=stake, pnl=-stake, odds=ex_odds, edge=ex_edge)
        elif alert.kind in ("model_value", "value"):
            section = flat[alert.kind]
            section["fired"] += 1
            # normalise the alert's top selection to (market family, side, line)
            if alert.kind == "model_value":
                cands = payload.get("candidates") or []
                top = cands[0] if cands else {}
                market = str(payload.get("market", ""))
                sel, ln = str(top.get("selection", "")), top.get("line")
                odds = float(top.get("odds") or 0.0)
                book = str(top.get("book", ""))
                event_id = str(top.get("event_id", ""))
                raw_market, raw_sel = market, sel  # normalised at record time
                if ln is not None:
                    raw_sel = f"{sel} {float(ln):g}"
            else:
                from sportsdata_agents.operations.monitoring import _market_family, _split_selection

                raw_market = str(payload.get("market", ""))
                raw_sel = str(payload.get("selection", ""))
                market = _market_family(raw_market) or raw_market
                sel, ln = _split_selection(raw_sel.lower())
                odds = float(payload.get("odds") or 0.0)
                book = str(payload.get("book", ""))
                event_id = str(payload.get("event_external_id", ""))
            fixture_id = score_fixture_of.get(str(alert.id))
            score = scores.get(fixture_id)
            if score is None or odds <= 1.0:
                section["pending"] += 1
                continue
            line_val = float(ln) if ln is not None else None
            graded = _grade(market, sel, line_val, score[0], score[1])
            if graded is None:
                section["pending"] += 1
                continue
            section["settled"] += 1
            section["staked"] += 1.0  # flat $1: hit-rate + flat ROI, no Kelly
            sample_key = "model_value" if alert.kind == "model_value" else "value_flat"
            flat_edge = _maybe_float(payload.get("edge_pct")
                                     if alert.kind == "value"
                                     else (payload.get("candidates") or [{}])[0].get("edge_pct"))
            if graded == "win":
                section["wins"] += 1
                section["pnl"] += odds - 1.0
                _record(sample_key, stake=1.0, pnl=odds - 1.0, odds=odds,
                        edge=flat_edge, book=book)
            elif graded == "push":
                section["pushes"] += 1
                section["staked"] -= 1.0  # returned stake
            else:
                section["pnl"] -= 1.0
                _record(sample_key, stake=1.0, pnl=-1.0, odds=odds,
                        edge=flat_edge, book=book)
            closing = await _closing_odds(
                session, book=book, event_id=event_id, market=raw_market,
                selection=raw_sel, start=fixture_start.get(fixture_id))
            if closing is not None and closing > 1.0:
                section["clv_n"] += 1
                section["clv_sum"] += (odds / closing - 1.0) * 100.0
        elif alert.kind in ("stat_value", "back_lay"):
            bucket = other.setdefault(alert.kind, {"fired": 0, "still_value": 0})
            bucket["fired"] += 1
            outcome = payload.get("outcome")
            if isinstance(outcome, dict) and outcome.get("still_value"):
                bucket["still_value"] += 1

    racing["staked"] = round(racing["staked"], 2)
    racing["pnl"] = round(racing["pnl"], 2)
    value["staked"] = round(value["staked"], 2)
    value["pnl"] = round(value["pnl"], 2)
    bsp["staked"] = round(bsp["staked"], 2)
    bsp["pnl"] = round(bsp["pnl"], 2)
    arbs["locked_profit"] = round(arbs["locked_profit"], 2)
    for section in flat.values():
        section["staked"] = round(section["staked"], 2)
        section["pnl"] = round(section["pnl"], 2)
        section["clv_mean_pct"] = (round(section.pop("clv_sum") / section["clv_n"], 2)
                                   if section["clv_n"] else None)
    # significance: is each section's ROI luck at this sample size, or edge?
    def _section_bets(section: str) -> list[tuple[float, float, float]]:
        return [(r["stake"], r["pnl"], r["odds"])
                for r in bet_rows if r["section"] == section]

    racing["significance"] = _significance(_section_bets("racing"))
    bsp["significance"] = _significance(_section_bets("bsp"))
    value["significance"] = _significance(_section_bets("value"))
    flat["model_value"]["significance"] = _significance(_section_bets("model_value"))
    flat["value"]["significance"] = _significance(_section_bets("value_flat"))

    report = {"since": since.isoformat(), "until": until.isoformat(),
              "racing": racing, "arbs": arbs, "value": value, "bsp": bsp,
              "flat": flat, "other": other, "racing_buckets": racing_buckets,
              "attribution": _attribution(bet_rows)}
    report["suggestions"] = tuning_suggestions(report)
    return report


def _roi(stats: dict[str, Any]) -> float | None:
    staked = float(stats.get("staked") or 0.0)
    return (float(stats["pnl"]) / staked * 100.0) if staked else None


# ---------------------------------------------------------------------------
# statistical significance — is a section's ROI signal, or luck?
#
# Two complementary answers per section, computed from the individual settled
# bets (stake, pnl, odds):
#
# - bootstrap 95% CI on ROI: resample the bets with replacement; the interval
#   says how much the ROI estimate itself wobbles at this sample size. A CI
#   whose low end clears 0 is the "this held up" signal.
# - Monte Carlo fair-market p-value: simulate the SAME bets (same odds, same
#   stakes) under the null that every price was fair — each bet wins with
#   probability 1/odds. p = share of simulated worlds with ROI >= observed.
#   Conservative by construction: real book prices carry vig, so a random
#   bettor's true win probability is BELOW 1/odds and the null is generous.
#
# Both are pure resampling — no distributional assumptions, no new deps.
# ---------------------------------------------------------------------------

_SIG_MIN_SETTLED = 5      # below this, any interval is astrology
_SIG_SIMS = 2000          # resamples/simulations per answer
_SIG_SEED = 20260708      # fixed seed: the same report twice is the same report


def _significance(bets: list[tuple[float, float, float]],
                  *, sims: int = _SIG_SIMS) -> dict[str, Any] | None:
    """Bootstrap CI + fair-market Monte Carlo for one section's settled bets.

    ``bets`` rows are (stake, pnl, odds) for each SETTLED bet (pushes
    excluded — returned stakes carry no information).
    """
    import random

    bets = [(s, p, o) for s, p, o in bets if s > 0 and o > 1.0]
    n = len(bets)
    if n < _SIG_MIN_SETTLED:
        return None
    rng = random.Random(_SIG_SEED)
    total_staked = sum(s for s, _p, _o in bets)
    observed_roi = sum(p for _s, p, _o in bets) / total_staked * 100.0

    # bootstrap the ROI
    rois: list[float] = []
    for _ in range(sims):
        staked = pnl = 0.0
        for _k in range(n):
            s, p, _o = bets[rng.randrange(n)]
            staked += s
            pnl += p
        if staked > 0:
            rois.append(pnl / staked * 100.0)
    rois.sort()
    lo = rois[int(0.025 * len(rois))]
    hi = rois[min(int(0.975 * len(rois)), len(rois) - 1)]

    # fair-market null: same bets, win probability 1/odds
    at_least = 0
    for _ in range(sims):
        pnl = 0.0
        for s, _p, o in bets:
            pnl += s * (o - 1.0) if rng.random() < 1.0 / o else -s
        if pnl / total_staked * 100.0 >= observed_roi:
            at_least += 1
    p_fair = at_least / sims

    return {"n": n, "roi_pct": round(observed_roi, 2),
            "roi_ci95": [round(lo, 2), round(hi, 2)],
            "p_fair_market": round(p_fair, 4),
            "verdict": ("edge" if p_fair < 0.05 and lo > 0
                        else "promising" if p_fair < 0.20
                        else "indistinguishable from luck")}


# ---------------------------------------------------------------------------
# attribution — WHERE did the P&L come from? Settled bets bucketed by odds
# band, quoted edge band, and book, per section: the replay/tuning loop reads
# this to see which slice of a kind carries the result (a kind can be +ROI
# overall while one odds band quietly bleeds).
# ---------------------------------------------------------------------------

_ATTR_MIN_N = 5  # buckets thinner than this are noise, not attribution


def _odds_band(odds: float) -> str:
    if odds < 2.0:
        return "<2"
    if odds < 4.0:
        return "2-4"
    if odds < 10.0:
        return "4-10"
    return "10+"


def _edge_band(edge: float) -> str:
    if edge < 10.0:
        return "<10%"
    if edge < 20.0:
        return "10-20%"
    if edge < 40.0:
        return "20-40%"
    return "40%+"


def _attribution(bet_rows: list[dict[str, Any]]) -> dict[str, Any]:
    def _bucketize(rows: list[dict[str, Any]], key_fn: Any) -> dict[str, Any]:
        buckets: dict[str, dict[str, float]] = {}
        for r in rows:
            key = key_fn(r)
            if key is None:
                continue
            b = buckets.setdefault(str(key), {"settled": 0, "staked": 0.0, "pnl": 0.0})
            b["settled"] += 1
            b["staked"] += r["stake"]
            b["pnl"] += r["pnl"]
        out = {}
        for key, b in buckets.items():
            if b["settled"] < _ATTR_MIN_N or not b["staked"]:
                continue
            out[key] = {"settled": int(b["settled"]),
                        "staked": round(b["staked"], 2),
                        "pnl": round(b["pnl"], 2),
                        "roi_pct": round(b["pnl"] / b["staked"] * 100.0, 1)}
        return out

    result: dict[str, Any] = {}
    sections = {r["section"] for r in bet_rows}
    for section in sorted(sections):
        rows = [r for r in bet_rows if r["section"] == section]
        entry: dict[str, Any] = {}
        by_odds = _bucketize(rows, lambda r: _odds_band(r["odds"]))
        if by_odds:
            entry["by_odds"] = by_odds
        by_edge = _bucketize(
            rows, lambda r: _edge_band(r["edge"]) if r.get("edge") is not None else None)
        if by_edge:
            entry["by_edge"] = by_edge
        by_book = _bucketize(rows, lambda r: r.get("book") or None)
        if by_book:
            entry["by_book"] = by_book
        if entry:
            result[section] = entry
    return result


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
    for kind, section in (report.get("flat") or {}).items():
        clv = section.get("clv_mean_pct")
        if (section.get("clv_n", 0) >= 20 and clv is not None and clv < 0.0):
            out.append(f"{kind} mean CLV {clv:+.1f}% over {section['clv_n']} "
                       "settled — the alerts are getting worse-than-closing "
                       "prices; consider raising that watch's min_edge_pct")
    return out


def format_scoreboard(report: dict[str, Any]) -> str:
    """The weekly push, in plain English with the caveats attached."""
    racing = report["racing"]
    arbs = report["arbs"]

    def _sig_note(section: dict[str, Any]) -> str:
        sig = section.get("significance")
        if not sig:
            return ""
        lo, hi = sig["roi_ci95"]
        return (f"\n    ↳ 95% CI [{lo:+.1f}%, {hi:+.1f}%] · "
                f"p(luck) {sig['p_fair_market']:.2f} → {sig['verdict']}")

    lines = [":bar_chart: Alert P&L scoreboard (last 7 days)"]
    if racing["settled"]:
        roi = (racing["pnl"] / racing["staked"] * 100.0) if racing["staked"] else 0.0
        lines.append(
            f"Racing: {racing['settled']} settled of {racing['fired']} fired — "
            f"{racing['wins']} won · staked ${racing['staked']:.2f} · "
            f"P&L ${racing['pnl']:+.2f} ({roi:+.1f}% ROI)"
            + (f" · {racing['pending']} pending results" if racing["pending"] else "")
            + _sig_note(racing)
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
            + _sig_note(value)
        )
    elif value.get("fired"):
        lines.append(f"Value (prediction/exchange): {value['fired']} fired, awaiting results")
    bsp = report.get("bsp") or {}
    if bsp.get("settled"):
        roi = (bsp["pnl"] / bsp["staked"] * 100.0) if bsp["staked"] else 0.0
        lines.append(
            f"BSP/form value: {bsp['settled']} settled of {bsp['fired']} fired — "
            f"{bsp['wins']} won · staked ${bsp['staked']:.2f} · "
            f"P&L ${bsp['pnl']:+.2f} ({roi:+.1f}% ROI)"
            + (f" · {bsp['pending']} pending" if bsp.get("pending") else "")
            + _sig_note(bsp))
    elif bsp.get("fired"):
        lines.append(f"BSP/form value: {bsp['fired']} fired, awaiting results")
    labels = {"model_value": "Model value (calibrated)",
              "value": "Model edges (stats/anchored)"}
    for kind, section in (report.get("flat") or {}).items():
        if section.get("settled"):
            roi = (section["pnl"] / section["staked"] * 100.0
                   if section["staked"] else 0.0)
            clv = section.get("clv_mean_pct")
            lines.append(
                f"{labels.get(kind, kind)}: {section['settled']} settled of "
                f"{section['fired']} fired — {section['wins']} won"
                + (f", {section['pushes']} pushed" if section.get("pushes") else "")
                + f" · flat-$1 P&L ${section['pnl']:+.2f} ({roi:+.1f}% ROI)"
                + (f" · mean CLV {clv:+.1f}% over {section['clv_n']}"
                   if clv is not None else "")
                + _sig_note(section))
        elif section.get("fired"):
            lines.append(f"{labels.get(kind, kind)}: {section['fired']} fired, "
                         "awaiting results")
    if arbs["fired"]:
        lines.append(
            f"Arbs: {arbs['fired']} fired · {arbs['still_takeable']} still takeable "
            f"when re-checked · locked profit if taken ${arbs['locked_profit']:.2f}"
        )
    for kind, stats in sorted(report["other"].items()):
        lines.append(f"{kind}: {stats['fired']} fired · "
                     f"{stats['still_value']} still live when re-checked")
    attribution = report.get("attribution") or {}
    for section, dims in sorted(attribution.items()):
        for dim_label, dim_key in (("odds", "by_odds"), ("edge", "by_edge"),
                                   ("book", "by_book")):
            buckets = dims.get(dim_key)
            if not buckets:
                continue
            parts = [f"{k} n={v['settled']} {v['roi_pct']:+.0f}%"
                     for k, v in sorted(buckets.items())]
            lines.append(f"  {section} by {dim_label}: " + " · ".join(parts))
    if len(lines) == 1:
        lines.append("No alerts fired this period.")
    for tip in report.get("suggestions") or []:
        lines.append(f":wrench: {tip}")
    lines.append("_Racing/BSP and prediction settle at printed Kelly; model "
                 "value and stats edges settle at flat $1 against final "
                 "scores (h2h, totals, lines; pushes returned) with mean "
                 "closing-line value; stat_value awaits player actuals._")
    return "\n".join(lines)
