"""Book-independent fair prices: ratings from results, form from race form.

``record_slate`` (quant.slate) prices boards CALIBRATED to a book's own
anchors — the consistency fair. This module records the OTHER fair price:
what the engine thinks with **no book input at all**.

- Team sports (footy family): ratings are fitted from the warehouse's own
  settled results (``event_results`` carries "score": "H-A" from the league
  scoreboards) and turned into expected-margin/expected-total levers.
- Racing: each runner's past placings (``race_form``, the TAB form capture)
  become decayed form probabilities per the engine's form model.

Both record predictions under ``engine-ratings:{sport}`` / ``engine-form:racing``
artifacts — alongside ``engine:{sport}`` (the anchored fair), the warehouse
holds BOTH fair prices per market, and the backtest/CLV loop grades each
independently. Sparse early data degrades cleanly: too few results or no
form rows means the sport is skipped and the report says so.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sportsdata_agents.data.models import EventResult, OddsSnapshot, Prediction, RaceForm
from sportsdata_agents.data.repository import TenantScope

logger = logging.getLogger(__name__)

__all__ = ["RATINGS_SPORTS", "record_ratings_slate"]

# engine sport -> warehouse labels; the footy-family ratings model fits any
# sport whose results carry scores, so this list grows with results coverage
RATINGS_SPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("afl", ("australian_rules", "afl")),
    ("rugby_league", ("rugby_league",)),
    ("rugby_union", ("rugby_union",)),
    # the ratings model is margin/total math, not footy-specific — every spine
    # sport whose results carry scores fits the same way. The MIN_RESULTS floor
    # keeps each sport OFF until its history is deep enough to be an opinion.
    ("basketball", ("basketball", "nba")),
    ("baseball", ("baseball", "mlb")),
    ("nfl", ("nfl", "american_football")),
    ("soccer", ("soccer",)),
    ("ice_hockey", ("ice_hockey", "nhl")),
    ("cricket", ("cricket",)),
)

MIN_RESULTS = 40  # a ratings fit off a weekend of scores is noise, not opinion

# LEAGUE POOLS: a women's competition scores differently (AFLW totals run ~60
# to AFL's ~170) — one pooled fit corrupts both. Pool membership reads from
# the result's competition meta and the team names; rep sides (Origin) are
# blocked separately by the market-sanity gate, and cricket formats stay
# unsegmented until results carry a format label.
_WOMENS = re.compile(r"\b(aflw|nrlw|wnba|nblw|women|women's|womens|w-league)\b",
                     re.IGNORECASE)


def _pool_of(*labels: str) -> str:
    return "women" if _WOMENS.search(" ".join(str(x) for x in labels)) else "open"
_VS_SPLIT = re.compile(r"\s+(?:v|vs|vs\.|@)\s+", re.IGNORECASE)
_SCORE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


def _match_rated(name: str, rated: list[str]) -> str | None:
    """The rated team this book-side name refers to — the resolver's token
    matching (drop stopwords, subset/overlap tolerant), so "Brisbane Broncos"
    finds "Broncos". None when unknown OR ambiguous: a wrong team match would
    price the wrong game, which is worse than not pricing."""
    from sportsdata_agents.operations.resolution.resolver import _token_match

    best: str | None = None
    for candidate in rated:
        if _token_match(name, candidate):
            if best is not None and candidate != best:
                return None
            best = candidate
    return best


def _parse_result(name: str, score: str, winner: str) -> tuple[str, str, int, int] | None:
    """(home, away, home_score, away_score) from a result row — None when the
    name or score doesn't parse, or the score contradicts the graded winner
    (a swapped scoreline poisons the fit worse than a missing one)."""
    parts = _VS_SPLIT.split(name.strip())
    match = _SCORE.match(score.strip())
    if len(parts) != 2 or not match:
        return None
    home, away = (p.strip() for p in parts)
    home_score, away_score = int(match.group(1)), int(match.group(2))
    graded = "home" if home_score > away_score else "away" if away_score > home_score else "draw"
    if winner in ("home", "away") and graded != winner:
        return None
    return home, away, home_score, away_score


def parse_last_starts(text: str, days_since_run: Any) -> list[Any]:
    """PastRun rows from a compact form string like "f2134152662".

    The string is recent-first finishing positions: digits 1-9 are placings,
    "0" reads as 10th, letters (f/x/p/…) are non-finishes — scored as a
    tail-of-field run. Field size and exact spacing aren't in the string, so
    the model sees the honest approximation: a typical field of 10 and a
    start every ~14 days behind the known days_since_run. The decay does the
    rest — recent digits dominate."""
    from sportsdata_engines.ratings.racing import PastRun

    field_size = 10
    try:
        first_gap = float(days_since_run)
    except (TypeError, ValueError):
        first_gap = 14.0
    runs: list[Any] = []
    for index, char in enumerate(str(text or "").strip()):
        if char.isdigit():
            position = 10 if char == "0" else int(char)
        elif char.isalpha():
            position = field_size  # fell/pulled up — a run, and a bad one
        else:
            continue
        runs.append(PastRun(position=min(position, field_size), field_size=field_size,
                            age_days=first_gap + 14.0 * index))
    return runs


async def _fit_footy(
    session: AsyncSession, labels: tuple[str, ...], now: dt.datetime
) -> dict[str, Any]:
    """Per-POOL ratings from this sport's settled results ({"open": …,
    "women": …}) — a pool below MIN_RESULTS is absent rather than noisy."""
    from sportsdata_engines.ratings.footy import MatchResult, fit_footy_ratings

    rows = (await session.execute(
        select(EventResult).where(EventResult.sport.in_(labels))
    )).scalars().all()
    pools: dict[str, list[Any]] = {}
    for row in rows:
        meta = row.meta or {}
        parsed = _parse_result(str(meta.get("event_name") or ""),
                               str(meta.get("score") or ""),
                               str(row.winning_selection))
        if parsed is None:
            continue
        home, away, home_score, away_score = parsed
        stamp = row.start_time or row.settled_at
        if stamp is None:
            continue
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)
        age_days = max(0.0, (now - stamp).total_seconds() / 86_400.0)
        pool = _pool_of(str(meta.get("competition") or ""), home, away)
        pools.setdefault(pool, []).append(
            MatchResult(home=home, away=away, home_score=home_score,
                        away_score=away_score, age_days=age_days))
    return {pool: fit_footy_ratings(results)
            for pool, results in pools.items() if len(results) >= MIN_RESULTS}


async def _market_main_total(
    session: AsyncSession, event_id: str, provider: str | None = None
) -> float | None:
    """The market's most balanced total line for an event — the sanity anchor
    the ratings totals are held against. None when no paired total exists.
    Provider-scoped: numeric event ids collide across feeds."""
    from sportsdata_agents.data.models import Price
    from sportsdata_agents.operations.monitoring import _market_family, _split_selection

    stmt = select(Price).where(Price.event_external_id == event_id)
    if provider:
        stmt = stmt.where(Price.provider == provider)
    rows = (await session.execute(
        stmt.order_by(Price.changed_at.desc()).limit(400)
    )).scalars().all()
    totals: dict[float, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if (row.market, row.selection) in seen:
            continue
        seen.add((row.market, row.selection))
        if _market_family(row.market) != "total":
            continue
        side, line = _split_selection(row.selection.lower())
        if side in ("over", "under") and line is not None:
            totals.setdefault(line, {})[side] = float(row.odds)
    paired = {ln: p for ln, p in totals.items() if len(p) == 2}
    if not paired:
        return None
    return min(paired, key=lambda ln: abs(1.0 / paired[ln]["over"] - 1.0 / paired[ln]["under"]))


async def _market_h2h_prob(
    session: AsyncSession, event_id: str, provider: str | None = None
) -> float | None:
    """The market's de-vigged HOME win probability from the freshest full
    h2h — the sanity anchor the ratings h2h fairs are held against. None
    when no complete h2h market exists. Provider-scoped: numeric event ids
    collide across feeds."""
    from sportsdata_agents.data.models import Price
    from sportsdata_agents.operations.monitoring import _market_family, _split_selection

    stmt = select(Price).where(Price.event_external_id == event_id)
    if provider:
        stmt = stmt.where(Price.provider == provider)
    rows = (await session.execute(
        stmt.order_by(Price.changed_at.desc()).limit(400)
    )).scalars().all()
    by_market: dict[str, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if (row.market, row.selection) in seen:
            continue
        seen.add((row.market, row.selection))
        if _market_family(row.market) != "h2h":
            continue
        side, line = _split_selection(row.selection.lower())
        if line is None and side in ("home", "away", "draw") and float(row.odds) > 1.0:
            by_market.setdefault(row.market, {})[side] = float(row.odds)
    for sides in by_market.values():
        if "home" in sides and "away" in sides:
            overround = sum(1.0 / o for o in sides.values())
            if 1.0 < overround <= 1.6:  # a real book's margin; else partial/stale
                return (1.0 / sides["home"]) / overround
    return None


async def _anchor_events(
    session: AsyncSession, provider: str, event_id: str
) -> list[tuple[str, str]]:
    """(provider, event id) pairs to hunt a sanity anchor in: the event's own
    feed FIRST, then every fixture sibling. Scoping the anchor to the own
    feed alone STARVED the gates — BetR's AFL events carry only alt-line
    'h2h [handicap]' rows and Dabble's only quarter derivatives, so the h2h
    gate found no anchor and waved a 47%-edge fitting artifact through
    (lived: Fremantle v Sydney, twice in one evening). The fixture's other
    books ARE the market the gate holds the model against."""
    from sportsdata_agents.data.models import Event

    pairs = [(provider, event_id)]
    mapping = (await session.execute(
        select(Event).where(Event.provider == provider,
                            Event.external_id == event_id)
    )).scalars().first()
    if mapping is not None and mapping.fixture_id is not None:
        siblings = (await session.execute(
            select(Event).where(Event.fixture_id == mapping.fixture_id,
                                Event.id != mapping.id)
        )).scalars().all()
        pairs.extend((s.provider, s.external_id) for s in siblings)
    return pairs


async def _anchored_main_total(
    session: AsyncSession, provider: str, event_id: str
) -> float | None:
    """_market_main_total across the fixture: first feed with a paired total
    answers (totals are frame-free)."""
    for prov, ev in await _anchor_events(session, provider, event_id):
        total = await _market_main_total(session, ev, prov)
        if total is not None:
            return total
    return None


async def _anchored_h2h_prob(
    session: AsyncSession, provider: str, event_id: str
) -> float | None:
    """_market_h2h_prob across the fixture, FRAME-TRANSLATED into the alerted
    event's home/away (a sibling listing the teams the other way round would
    hand the gate 1-p and invert the verdict). A sibling whose orientation
    can't be established never anchors."""
    from sportsdata_agents.quant.backtest import _event_name_for, _translate_side

    cache: dict[tuple[str, str], str] = {}
    own_name = await _event_name_for(session, cache, provider, event_id)
    for prov, ev in await _anchor_events(session, provider, event_id):
        home_prob = await _market_h2h_prob(session, ev, prov)
        if home_prob is None:
            continue
        if prov == provider and ev == event_id:
            return home_prob
        sib_name = await _event_name_for(session, cache, prov, ev)
        if not own_name or not sib_name:
            continue
        orientation = _translate_side("home", sib_name, own_name)
        if orientation == "home":
            return home_prob
        if orientation == "away":
            # the sibling's away is OUR home; with a draw in the market this
            # is approximate, but the gate compares at 15-point granularity
            return 1.0 - home_prob
    return None


async def _upcoming_events(
    session: AsyncSession, labels: tuple[str, ...], now: dt.datetime, horizon_hours: float
) -> list[tuple[str, str, str, str]]:
    """(provider, book, event_id, event_name) for events jumping inside the
    horizon — from the freshest snapshot per event."""
    rows = (await session.execute(
        select(OddsSnapshot.provider, OddsSnapshot.book,
               OddsSnapshot.event_external_id, OddsSnapshot.event_name)
        .where(OddsSnapshot.sport.in_(labels),
               OddsSnapshot.start_time > now,
               OddsSnapshot.start_time < now + dt.timedelta(hours=horizon_hours))
        .distinct()
    )).all()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str, str]] = []
    for provider, book, event_id, name in rows:
        if (book, event_id) in seen or not name:
            continue
        seen.add((book, event_id))
        out.append((provider, book, event_id, name))
    return out


async def _dedupe_hit(session: AsyncSession, scope: TenantScope, artifact_id: Any,
                      book: str, event_id: str, now: dt.datetime, hours: float) -> bool:
    fresh = (await session.execute(
        select(Prediction.id).where(
            Prediction.tenant_id == scope.tenant_id,
            Prediction.workspace_id == scope.workspace_id,
            Prediction.model_id == artifact_id,
            Prediction.provider == book,
            Prediction.event_external_id == event_id,
            Prediction.predicted_at > now - dt.timedelta(hours=hours),
        ).limit(1)
    )).scalar_one_or_none()
    return fresh is not None


async def record_ratings_slate(
    session: AsyncSession,
    scope: TenantScope,
    *,
    now: dt.datetime,
    horizon_hours: float = 48.0,
    dedupe_hours: float = 12.0,
    max_events: int = 40,
) -> dict[str, Any]:
    """Record book-independent fair prices: ratings boards for team sports,
    form boards for racing. Returns {"recorded","events","skipped_*",...}."""
    try:
        from sportsdata_engines.core.types import FixtureInputs
    except ImportError:
        return {"recorded": 0, "events": 0,
                "error": "ratings pricing needs the local engines package"}
    import importlib

    from sportsdata_agents.tools.quant import _warehouse_key

    recorded = 0
    events_priced = 0
    skipped_dedupe = 0
    skipped_unrated = 0
    skipped_sanity = 0

    async def _record_board(artifact_name: str, sport: str, book: str, event_id: str,
                            board: list[Any]) -> int:
        nonlocal recorded
        added = 0
        for price in board:
            prob = float(getattr(price, "fair_probability", 0.0))
            if not 0.0 < prob < 1.0:
                continue
            key = _warehouse_key(price.market, price.selection, price.line)
            if key is None:
                continue
            market, selection = key
            session.add(Prediction(
                tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                model_id=artifacts[artifact_name], provider=book,
                event_external_id=event_id, market=market, selection=selection,
                prob=Decimal(str(round(prob, 5))), predicted_at=now,
            ))
            added += 1
        recorded += added
        return added

    artifacts: dict[str, Any] = {}

    async def _artifact(name: str, sport: str) -> None:
        """The auto-managed ratings artifact — its own name, never the anchored
        engine:{sport} one, so the two fair-price families grade separately."""
        if name in artifacts:
            return
        from sportsdata_agents.data.models import ModelArtifact

        found = (await session.execute(
            select(ModelArtifact).where(
                ModelArtifact.tenant_id == scope.tenant_id,
                ModelArtifact.workspace_id == scope.workspace_id,
                ModelArtifact.name == name,
            ).order_by(ModelArtifact.version.desc()).limit(1)
        )).scalar_one_or_none()
        if found is None:
            found = ModelArtifact(
                tenant_id=scope.tenant_id, workspace_id=scope.workspace_id,
                name=name, version=1, sport=sport, market="board",
                params={"backend": "ratings", "source": "ratings-slate"},
                calibration={"source": "ratings", "measured_by": "replay"},
                trained_at=dt.datetime.now(dt.UTC),
            )
            session.add(found)
            await session.flush()
        artifacts[name] = found.id
        await session.commit()

    # ── team sports: ratings from settled results ──────────────────────────
    for sport, labels in RATINGS_SPORTS:
        try:
            pools = await _fit_footy(session, labels, now)
        except ImportError:
            return {"recorded": recorded, "events": events_priced,
                    "error": "ratings pricing needs the local engines package"}
        if not pools:
            continue  # not enough scored results yet — accrues daily
        module = importlib.import_module(f"sportsdata_engines.{sport}")
        for provider, book, event_id, event_name in await _upcoming_events(
                session, labels, now, horizon_hours):
            if events_priced >= max_events:
                break
            parts = _VS_SPLIT.split(event_name.strip())
            if len(parts) != 2:
                continue
            # a women's fixture prices off the women's pool — one pooled fit
            # corrupted both (AFLW totals ~60 vs AFL ~170)
            pool = _pool_of(event_name)
            ratings = pools.get(pool)
            if ratings is None:
                skipped_unrated += 1
                continue
            name = f"engine-ratings:{sport}" + ("w" if pool == "women" else "")
            await _artifact(name, sport)
            rated_teams = list(ratings.attack)
            # book names and scoreboard names differ ("Brisbane Broncos" vs
            # "Broncos") — the RESOLVER's token matching already solves this
            # for fixtures, so team identity rides the same machinery here
            home = _match_rated(parts[0].strip(), rated_teams)
            away = _match_rated(parts[1].strip(), rated_teams)
            if home is None or away is None:
                skipped_unrated += 1  # unknown or ambiguous vs the results history
                continue
            try:
                margin, total = ratings.expected_margin_total(home, away)
            except (KeyError, ValueError):
                skipped_unrated += 1
                continue
            # MARKET SANITY: a stats total drastically off the market's is a
            # fitting artifact, not an opinion — rep teams (Origin) carry 1-2
            # games of history and fit wild ratings (lived: QLD v NSW read a
            # 61.7 fair total against the market's 34.5). No fair beats a
            # garbage fair.
            market_total = await _anchored_main_total(session, provider, event_id)
            if (market_total is not None
                    and abs(float(total) - market_total) > 0.2 * market_total):
                skipped_sanity += 1
                logger.info("ratings slate: %s %s total %.1f vs market %.1f — "
                            "outside the sanity band, not recorded",
                            sport, event_name, float(total), market_total)
                continue
            if await _dedupe_hit(session, scope, artifacts[name], book, event_id,
                                 now, dedupe_hours):
                skipped_dedupe += 1
                continue
            inputs = FixtureInputs(sport=sport, fixture_id=event_id,
                                   levers={"expected_margin": float(margin),
                                           "expected_total": float(total)})
            try:
                board = module.price_board(inputs)
            except (ValueError, RuntimeError) as e:
                logger.info("ratings slate: could not price %s/%s: %s", sport, event_id, e)
                continue
            # H2H SANITY, same spirit as the totals band: a stats model
            # 15+ probability points from the market's de-vigged h2h is a
            # fitting artifact, not a 30%+ edge on an efficient market
            # (lived: Fremantle v Sydney read away 55% against the whole
            # industry's ~38% incl. Pinnacle — a +45% "edge" alert)

            board_home = next(
                (float(p.fair_probability) for p in board
                 if _warehouse_key(p.market, p.selection, p.line) == ("h2h", "home")),
                None)
            if board_home is not None:
                market_home = await _anchored_h2h_prob(session, provider, event_id)
                if (market_home is not None
                        and abs(board_home - market_home) > 0.15):
                    skipped_sanity += 1
                    logger.info("ratings slate: %s %s h2h home %.2f vs market "
                                "%.2f — outside the sanity band, not recorded",
                                sport, event_name, board_home, market_home)
                    continue
            if await _record_board(name, sport, book, event_id, board):
                events_priced += 1
                await session.commit()

    # ── racing: form probabilities from the TAB form capture ───────────────
    try:
        from sportsdata_engines import racing as racing_module
        from sportsdata_engines.ratings.racing import form_win_probabilities
    except ImportError:
        return {"recorded": recorded, "events": events_priced,
                "skipped_dedupe": skipped_dedupe, "skipped_unrated": skipped_unrated,
            "skipped_sanity": skipped_sanity,
                "error": "ratings pricing needs the local engines package"}
    form_rows = (await session.execute(
        select(RaceForm).where(RaceForm.start_time > now,
                               RaceForm.start_time < now + dt.timedelta(hours=6.0))
    )).scalars().all()
    if form_rows:
        name = "engine-form:racing"
        await _artifact(name, "racing")
    # TAB and Sportsbet both store the SAME physical race under different
    # keys — price it ONCE, from whichever source exposed more runners
    def _runs_count(row: Any) -> int:
        return sum(1 for r in row.runners or [] if r.get("runs"))

    best_by_race: dict[tuple[int, Any], Any] = {}
    for race in form_rows:
        slot = race.start_time.replace(second=0, microsecond=0) if race.start_time else None
        key = (int(race.race_number), slot)
        held = best_by_race.get(key)
        if held is None or _runs_count(race) > _runs_count(held):
            best_by_race[key] = race
    form_rows = list(best_by_race.values())
    for race in form_rows:
        if events_priced >= max_events:
            break
        from sportsdata_engines.ratings.racing import PastRun

        history: dict[str, list[Any]] = {}
        numbers: dict[str, Any] = {}
        field = [r for r in (race.runners or []) if not r.get("scratched")]
        for runner in field:
            # STRUCTURED runs only (position/field_size/age_days parsed from a
            # racecard's run-by-run history). The compact-string approximation
            # is retired from pricing: it produced near-uniform probabilities
            # on thin fields ("fair 4.00" on a 20.0 dog) — recorded garbage.
            runs = [PastRun(position=int(r["position"]), field_size=int(r["field_size"]),
                            age_days=float(r["age_days"]))
                    for r in runner.get("runs") or [] if isinstance(r, dict)]
            if runs:
                label = str(runner.get("name") or runner.get("number"))
                history[label] = runs
                numbers[label] = runner.get("number")
        # a fair needs MOST of the field exposed — pricing 3 of 10 runners
        # overstates every probability
        if len(history) < 3 or (field and len(history) < 0.6 * len(field)):
            continue
        if await _dedupe_hit(session, scope, artifacts[name], "TAB", race.race_key,
                             now, dedupe_hours):
            skipped_dedupe += 1
            continue
        probabilities = form_win_probabilities(history)
        inputs = FixtureInputs(
            sport="racing", fixture_id=race.race_key,
            levers=racing_module.board.win_levers(probabilities),
            participants=list(probabilities),
        )
        try:
            board = racing_module.board.price_board(inputs)
        except (ValueError, RuntimeError) as e:
            logger.info("form slate: could not price %s: %s", race.race_key, e)
            continue
        # predictions keyed by SADDLE NUMBER where known — racing results
        # record the winning number, so number-keyed rows settle directly
        import dataclasses

        renamed = []
        for price in board:
            number = numbers.get(price.selection)
            if number is not None:
                price = dataclasses.replace(price, selection=str(number))
            renamed.append(price)
        if await _record_board(name, "racing", "TAB", race.race_key, renamed):
            events_priced += 1
            await session.commit()

    return {"recorded": recorded, "events": events_priced,
            "skipped_dedupe": skipped_dedupe, "skipped_unrated": skipped_unrated}
