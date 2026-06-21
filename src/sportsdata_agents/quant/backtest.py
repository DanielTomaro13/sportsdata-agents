"""Backtesting (M2.3, P8): replay the price series + results against predictions.

Strategy under test: flat-stake 1 unit on any prediction whose edge at the ENTRY
price clears ``min_edge_pct``. Settlement comes from ``event_results``; CLV compares
entry to the CLOSING price (the last change-point) — the §16.3 gold metric: a
strategy that beats the close has edge even when short-run results wobble.

Entry discipline (no lookahead): the entry price is what you could actually GET at
prediction time — the prevailing change-point at ``predicted_at``, or the first one
after it when the prediction predates every capture. Taking the first-ever captured
price regardless would credit the model with prices it never saw.

Settlement is resolution-aware: results land under whichever book reported them, so
a prediction keyed on another book's event id settles through the shared fixture
(events → fixture_id). Side-relative winners ("home"/"away") translate between the
two books' listing orders via name-token matching — and when orientation can't be
established, the bet stays UNSETTLED rather than guessing (a flipped side corrupts
ROI silently).
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, EventResult, OddsSnapshot, Prediction, Price
from sportsdata_agents.data.repository import TenantScope
from sportsdata_agents.operations.resolution.resolver import _side_ok, _tokens, split_sides

_SIDE_WINNERS = ("home", "away", "draw")


class _Maps(NamedTuple):
    fixture_by_pe: dict[tuple[str, str], uuid.UUID | None]
    fixture_by_ext: dict[str, uuid.UUID | None]
    result_by_pe: dict[tuple[str, str], EventResult]
    result_by_ext: dict[str, EventResult | None]  # None = ext id collides across providers
    result_by_fixture: dict[uuid.UUID, EventResult]
    events_by_fixture: dict[uuid.UUID, list[tuple[str, str]]]


async def _settlement_maps(session: AsyncSession) -> _Maps:
    """Fixture joins + results, loaded once per backtest instead of per prediction."""
    events = (await session.execute(select(Event))).scalars().all()
    fixture_by_pe: dict[tuple[str, str], uuid.UUID | None] = {
        (e.provider, e.external_id): e.fixture_id for e in events
    }
    fixture_by_ext: dict[str, uuid.UUID | None] = {}
    events_by_fixture: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for e in events:  # ext-id-only fallback; collisions across providers → unusable
        prior = fixture_by_ext.get(e.external_id, e.fixture_id)
        fixture_by_ext[e.external_id] = e.fixture_id if prior == e.fixture_id else None
        if e.fixture_id is not None:
            events_by_fixture.setdefault(e.fixture_id, []).append((e.provider, e.external_id))
    results = (
        await session.execute(
            select(EventResult).order_by(EventResult.settled_at.asc().nulls_first())
        )
    ).scalars().all()
    result_by_pe: dict[tuple[str, str], EventResult] = {}
    result_by_ext: dict[str, EventResult | None] = {}
    result_by_fixture: dict[uuid.UUID, EventResult] = {}
    for res in results:  # ascending order: the newest settlement overwrites
        result_by_pe[(res.provider, res.event_external_id)] = res
        # ext-only is a fallback for agent-recorded results (provider "") — five
        # providers share one numeric id namespace, so a cross-provider collision
        # poisons the ext key and settlement falls through to the fixture join
        known = result_by_ext.get(res.event_external_id, res)
        result_by_ext[res.event_external_id] = (
            res if known is not None and known.provider == res.provider else None
        )
        fixture = fixture_by_pe.get((res.provider, res.event_external_id)) or fixture_by_ext.get(
            res.event_external_id
        )
        if fixture is not None:
            result_by_fixture[fixture] = res
    return _Maps(fixture_by_pe, fixture_by_ext, result_by_pe, result_by_ext,
                 result_by_fixture, events_by_fixture)


async def _event_name_for(
    session: AsyncSession, cache: dict[tuple[str, str], str], provider: str, external_id: str
) -> str:
    """The event name a book published for (provider, external id) — orientation
    evidence for translating side-relative winners between books."""
    key = (provider, external_id)
    if key not in cache:
        stmt = select(func.max(OddsSnapshot.event_name)).where(
            OddsSnapshot.event_external_id == external_id
        )
        if provider:
            stmt = stmt.where(OddsSnapshot.provider == provider)
        cache[key] = str((await session.execute(stmt)).scalar() or "")
    return cache[key]


def _translate_side(winner: str, pred_name: str, result_name: str) -> str | None:
    """A side-relative winner in the RESULT book's frame → the PREDICTION book's
    frame ("home" flips when the books list the teams in opposite order). None when
    either name fails to split or the sides can't be aligned unambiguously."""
    if winner == "draw":
        return "draw"
    pred_sides, result_sides = split_sides(pred_name), split_sides(result_name)
    if not pred_sides or not result_sides:
        return None
    p_home, p_away = _tokens(pred_sides[0]), _tokens(pred_sides[1])
    r_home, r_away = _tokens(result_sides[0]), _tokens(result_sides[1])
    same = _side_ok(p_home, r_home) and _side_ok(p_away, r_away)
    swapped = _side_ok(p_home, r_away) and _side_ok(p_away, r_home)
    if same == swapped:  # neither or both — never guess
        return None
    return winner if same else {"home": "away", "away": "home"}[winner]


def _devig_fair_odds(odds_by_sel: dict[str, float], selection: str) -> float | None:
    """Remove the book's overround from a market's closing odds → the FAIR (no-vig)
    decimal odds for `selection`. None when the market is too thin/garbage to trust the
    de-vig (caller falls back to the raw price rather than reporting a bad number)."""
    o = odds_by_sel.get(selection)
    if not o or o <= 1.0:
        return None
    inv = [1.0 / v for v in odds_by_sel.values() if v and v > 1.0]
    if len(inv) < 2:  # need the full market to know the overround
        return None
    overround = sum(inv)
    if not (1.0 < overround <= 1.6):  # a real book overrounds modestly; else partial/stale
        return None
    fair_prob = (1.0 / o) / overround
    return 1.0 / fair_prob


async def _benchmark_close(
    session: AsyncSession,
    maps: _Maps,
    name_cache: dict[tuple[str, str], str],
    pred: Prediction,
    *,
    clv_book: str,
) -> tuple[float, bool] | None:
    """The benchmark book's closing price for this prediction's selection at the SAME
    fixture, DE-VIGGED to fair odds where possible (so CLV isn't biased by comparing a
    vigged entry to a differently-vigged benchmark — e.g. Pinnacle's low margin).

    Returns ``(odds, devigged)``: ``devigged=True`` when the full market was available and
    the overround removed; ``False`` when only the raw single-selection close was found
    (thin market). Side-relative selections translate into the benchmark frame first.
    ``None`` when the benchmark never priced it (caller falls back to the bet book's close)."""
    fixture = maps.fixture_by_pe.get(
        (pred.provider, pred.event_external_id)
    ) or maps.fixture_by_ext.get(pred.event_external_id)
    if fixture is None:
        return None
    pred_name = ""
    side_relative = pred.selection in _SIDE_WINNERS
    if side_relative:
        pred_name = await _event_name_for(session, name_cache, pred.provider, pred.event_external_id)
    for provider, external_id in maps.events_by_fixture.get(fixture, []):
        selection = pred.selection
        if side_relative:
            bench_name = await _event_name_for(session, name_cache, provider, external_id)
            # _translate_side maps FROM its third arg's frame INTO its second's
            translated = _translate_side(pred.selection, bench_name, pred_name)
            if translated is None:
                continue
            selection = translated
        # Pull the benchmark book's FULL market at close (all selections, latest each) so we
        # can de-vig — a vigged-entry-vs-vigged-benchmark CLV ratio is systematically biased.
        rows = (
            await session.execute(
                select(Price.selection, Price.odds)
                .where(
                    Price.book == clv_book,
                    Price.event_external_id == external_id,
                    Price.market == pred.market,
                )
                .order_by(Price.changed_at.desc())
            )
        ).all()
        latest: dict[str, float] = {}
        for sel, odds in rows:
            latest.setdefault(sel, float(odds))  # first seen = latest (desc order)
        if selection not in latest:
            continue
        fair = _devig_fair_odds(latest, selection)
        if fair is not None:
            return fair, True
        return latest[selection], False  # market too thin to de-vig — raw close
    return None


async def run_backtest(
    session_factory: async_sessionmaker[AsyncSession],
    scope: TenantScope,
    *,
    model_id: str | None = None,
    min_edge_pct: float = 2.0,
    book: str | None = None,
    clv_book: str | None = None,
) -> dict[str, Any]:
    """Replay predictions vs captured prices + results → ROI / hit-rate / CLV / variance.

    clv_book (e.g. "Pinnacle") benchmarks CLV against THAT book's close for the
    same fixture/market/selection (orientation-translated) instead of the bet
    book's own close — beating the sharp close is the stronger edge signal. Falls
    back to the own-book close per bet when the benchmark has no series there."""
    async with session_factory() as session:
        stmt = select(Prediction).where(
            Prediction.tenant_id == scope.tenant_id,
            Prediction.workspace_id == scope.workspace_id,
        )
        if model_id:
            stmt = stmt.where(Prediction.model_id == uuid.UUID(model_id))
        predictions = (await session.execute(stmt)).scalars().all()

        maps = await _settlement_maps(session)
        fixture_by_pe, fixture_by_ext = maps.fixture_by_pe, maps.fixture_by_ext
        result_by_ext, result_by_fixture = maps.result_by_ext, maps.result_by_fixture
        name_cache: dict[tuple[str, str], str] = {}
        bets: list[dict[str, Any]] = []
        skipped = {"no_price": 0, "no_result": 0, "below_edge": 0}
        for pred in predictions:
            price_stmt = (
                select(Price)
                .where(
                    Price.event_external_id == pred.event_external_id,
                    Price.market == pred.market,
                    Price.selection == pred.selection,
                )
                .order_by(Price.changed_at)
            )
            if book:
                price_stmt = price_stmt.where(Price.book == book)
            series = (await session.execute(price_stmt)).scalars().all()
            if not series:
                skipped["no_price"] += 1
                continue
            winner: str | None = None
            result = maps.result_by_pe.get(
                (pred.provider, pred.event_external_id)
            ) or result_by_ext.get(pred.event_external_id)
            if result is not None:  # direct hit: same event id, same book frame
                winner = result.winning_selection
            else:  # settle through the shared fixture (result came from another book)
                fixture = fixture_by_pe.get(
                    (pred.provider, pred.event_external_id)
                ) or fixture_by_ext.get(pred.event_external_id)
                result = result_by_fixture.get(fixture) if fixture is not None else None
                if result is not None:
                    if result.winning_selection in _SIDE_WINNERS:
                        pred_name = await _event_name_for(
                            session, name_cache, pred.provider, pred.event_external_id
                        )
                        # scoreboard results carry their frame's name in meta —
                        # they have no odds snapshots to look it up from
                        result_name = str(
                            (result.meta or {}).get("event_name") or ""
                        ) or await _event_name_for(
                            session, name_cache, result.provider, result.event_external_id
                        )
                        winner = _translate_side(
                            result.winning_selection, pred_name, result_name
                        )
                    else:  # racing saddle numbers, team names — book-independent
                        winner = result.winning_selection
            if winner is None:
                skipped["no_result"] += 1
                continue

            entry_row = series[0]
            if pred.predicted_at is not None:
                prevailing = [r for r in series if r.changed_at <= pred.predicted_at]
                entry_row = prevailing[-1] if prevailing else series[0]
            entry, closing = float(entry_row.odds), float(series[-1].odds)
            benchmarked = False
            devigged = False
            if clv_book:
                bench = await _benchmark_close(
                    session, maps, name_cache, pred, clv_book=clv_book
                )
                if bench is not None:
                    closing, devigged = bench
                    benchmarked = True
            prob = float(pred.prob)
            edge_pct = (prob * entry - 1.0) * 100.0
            if edge_pct < min_edge_pct:
                skipped["below_edge"] += 1
                continue
            won = pred.selection == winner
            pnl = (entry - 1.0) if won else -1.0
            bets.append(
                {
                    "event": pred.event_external_id,
                    "selection": pred.selection,
                    "prob": prob,
                    "entry_odds": entry,
                    "closing_odds": closing,
                    "clv_benchmarked": benchmarked,
                    "clv_devigged": devigged,
                    "edge_pct": round(edge_pct, 2),
                    "clv_pct": round((entry / closing - 1.0) * 100.0, 2),
                    "won": won,
                    "pnl": round(pnl, 4),
                }
            )

    if not bets:
        return {"bets": 0, "skipped": skipped, "note": "no qualifying bets — nothing to report"}
    pnls = [b["pnl"] for b in bets]
    clvs = [(b["entry_odds"] / b["closing_odds"] - 1.0) * 100.0 for b in bets]
    mean_pnl = sum(pnls) / len(pnls)
    return {
        "bets": len(bets),
        "staked": float(len(bets)),  # flat 1-unit stakes
        "pnl": round(sum(pnls), 4),
        "roi_pct": round(sum(pnls) / len(bets) * 100.0, 2),
        "hit_rate_pct": round(sum(1 for b in bets if b["won"]) / len(bets) * 100.0, 2),
        "avg_clv_pct": round(sum(clvs) / len(clvs), 2),
        "pnl_variance": round(sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls), 4),
        "min_edge_pct": min_edge_pct,
        "clv_book": clv_book,
        "clv_benchmarked_bets": sum(1 for b in bets if b.get("clv_benchmarked")),
        "clv_devigged_bets": sum(1 for b in bets if b.get("clv_devigged")),
        "clv_note": (
            "CLV vs the benchmark book's DE-VIGGED fair close where the full market was "
            "available (clv_devigged_bets); the rest fall back to its raw close, which is "
            "vig-affected across books." if clv_book else
            "CLV vs the bet book's own close (same book → vig cancels in the ratio)."
        ),
        "skipped": skipped,
        "per_bet": bets,
    }
