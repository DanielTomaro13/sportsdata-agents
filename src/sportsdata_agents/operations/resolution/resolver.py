"""Event resolution (the milestone M0.3's fixtures/events tables were built for):
every book invents its own id for the same match — map them all onto ONE fixture so
cross-book best-price, cross-book CLV, and auto-settling backtests can join.

Deterministic (P8): team-token matching, no LLM. A book event resolves to an
existing fixture when sport family + date agree and both sides' name tokens overlap
(swap-tolerant — books disagree on home/away listing order for neutral venues);
otherwise it founds a new fixture. Racing fixtures key on the normalised race name
(track + race number) per day. Ambiguity (two candidate fixtures both match) is
counted and SKIPPED, never guessed — a wrong join poisons every downstream number.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sportsdata_agents.data.models import Event, Fixture, OddsSnapshot
from sportsdata_agents.operations.ingestion.normalizers import canonical_sport

logger = logging.getLogger(__name__)

_SEPARATORS = (" vs ", " v ", " - ", " @ ", " At ", " at ")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({"the", "fc", "afc", "club"})
MATCH_THRESHOLD = 0.3  # rank floor; the real gate is the per-side fuzzy-subset rule
# One-name events (racing, outrights) gate on the same fuzzy-subset rule — plain
# Jaccard merged "Argentina Markets 2026" with "Brazil Markets 2026" (the generic
# tokens dominate). Jaccard then only ranks: the floor keeps one-token names from
# subset-matching everything.
NAME_THRESHOLD = 0.4


def _tokens(name: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RE.findall(name.lower()) if t not in _STOPWORDS)


def split_sides(event_name: str) -> tuple[str, str] | None:
    """("X", "Y") for a two-sided event name under any book's separator; None for
    racing/futures names. '@'/'At' list away first (US convention) — normalised
    here. A name mis-split by a separator inside it just fails the per-side
    fuzzy-subset gate and stays book-local (the known " - " limit's class)."""
    for sep in _SEPARATORS:
        if sep in event_name:
            left, right = (part.strip() for part in event_name.split(sep, 1))
            if left and right:
                return (right, left) if sep in (" @ ", " At ", " at ") else (left, right)
    return None


def _token_match(a: str, b: str) -> bool:
    """Equal, prefix, or abbreviation-subsequence ("wst" ⊂ "western") — ≥3 chars."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 3:
        return False
    if long_.startswith(short):
        return True
    it = iter(long_)
    return all(ch in it for ch in short)


# A team VARIANT is a different team: "Blues Women" is not "Blues", "Australia
# U20" is not "Australia". The subset rule alone can't tell a variant marker
# from a nickname ("Adelaide Crows") because both ride the longer name — so
# markers are checked explicitly on BOTH names before the subset test. Found
# live: a Super Rugby Women's match fixture-merged with the men's game and
# manufactured a 74% "arb".
_VARIANT_WORDS = frozenset({"women", "womens", "woman", "ladies", "female", "girls",
                            "reserves", "academy", "youth", "amateur", "b"})
_VARIANT_RE = re.compile(r"^u\d{1,2}$")  # U17 / U20 / U23 age grades


def _variant_markers(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in tokens if t in _VARIANT_WORDS or _VARIANT_RE.match(t))


def _side_ok(x: frozenset[str], y: frozenset[str]) -> bool:
    """One side names the same team iff the SHORTER name's every token has a fuzzy
    partner in the longer ("Adelaide" ⊆ "Adelaide Crows"; "Wst Bulldogs" ≈
    "Western Bulldogs") — but "Sydney Swans" never matches "Sydney Roosters"
    (the nickname token has no partner), and a variant marker on either name
    ("Blues Women", "Australia U20") must appear on BOTH or the teams differ."""
    if _variant_markers(x) != _variant_markers(y):
        return False
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) == 1 and len(long_) >= 2:
        # a lone token can be the INITIALS of the multi-word name — "nsw" is
        # "New South Wales", "gws" is "Greater Western Sydney" (TAB abbreviates
        # rep sides; token subsequence can't see across word boundaries)
        (token,) = short
        if len(token) == len(long_) and "".join(sorted(u[0] for u in long_)) == "".join(sorted(token)):
            return True
    return bool(short) and all(any(_token_match(t, u) for u in long_) for t in short)


def _sides_score(a: tuple[frozenset[str], frozenset[str]], b: tuple[frozenset[str], frozenset[str]]) -> float:
    """0 unless BOTH sides fuzzy-subset match (swap-tolerant); the mean Jaccard
    otherwise (used only to RANK multiple candidates)."""

    def jac(x: frozenset[str], y: frozenset[str]) -> float:
        return len(x & y) / len(x | y) if x | y else 0.0

    def combo(p: tuple[frozenset[str], frozenset[str]], q: tuple[frozenset[str], frozenset[str]]) -> float:
        if not (_side_ok(p[0], q[0]) and _side_ok(p[1], q[1])):
            return 0.0
        return (jac(p[0], q[0]) + jac(p[1], q[1])) / 2

    return max(combo(a, b), combo(a, (b[1], b[0])))


def _fixture_day(fixture: Fixture) -> str:
    """The day a fixture is indexed under: the real start, else the end (the blessed
    day-window proxy), else the FOUNDING event's day persisted in ``meta`` — never the
    resolution run's wall clock, which silently drifts a start-less fixture out of its
    own matching window on every later pass (found: a same-day duplicate fixture where
    an ambiguity skip was expected)."""
    anchor = fixture.start_time or fixture.end_time
    if anchor is not None:
        return anchor.strftime("%Y-%m-%d")
    proxy = (fixture.meta or {}).get("day_proxy")
    return proxy if isinstance(proxy, str) else dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


async def resolve_events(
    session_factory: async_sessionmaker[AsyncSession], *, dry_run: bool = False
) -> dict[str, Any]:
    """Map every unresolved (provider, event id) seen in the warehouse onto a fixture."""
    async with session_factory() as session:
        mapped_keys = {
            (provider, external_id)
            for provider, external_id in (
                await session.execute(select(Event.provider, Event.external_id))
            ).all()
        }
        seen = (
            await session.execute(
                select(
                    OddsSnapshot.provider,
                    OddsSnapshot.event_external_id,
                    func.max(OddsSnapshot.event_name),
                    func.max(OddsSnapshot.sport),
                    func.min(OddsSnapshot.captured_at),
                    func.max(OddsSnapshot.start_time),
                    func.max(OddsSnapshot.end_time),
                ).group_by(OddsSnapshot.provider, OddsSnapshot.event_external_id)
            )
        ).all()
        fixtures = (await session.execute(select(Fixture))).scalars().all()
        fixtures_by_id: dict[uuid.UUID, Fixture] = {f.id: f for f in fixtures}
        # in-memory candidate index:
        # (sport_family, date) → [(fixture_id, sides|name tokens, start_time)]
        index: dict[tuple[str, str], list[tuple[uuid.UUID, Any, dt.datetime | None]]] = {}

        def fixture_day(fx_time: dt.datetime | None) -> str:
            return (fx_time or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%d")

        def add_to_index(
            fixture_id: uuid.UUID, sport: str, day: str, name: str,
            start: dt.datetime | None,
        ) -> None:
            sides = split_sides(name)
            keyed = (
                (_tokens(sides[0]), _tokens(sides[1])) if sides else _tokens(name)
            )
            index.setdefault((sport, day), []).append((fixture_id, keyed, start))

        for fixture in fixtures:
            add_to_index(fixture.id, fixture.sport, _fixture_day(fixture),
                         fixture.name, fixture.start_time)

        stats = {"examined": 0, "mapped": 0, "created": 0, "ambiguous": 0, "skipped_unnamed": 0}
        now = dt.datetime.now(dt.UTC)
        for provider, external_id, event_name, sport, first_seen, start_time, end_time in seen:
            if (provider, external_id) in mapped_keys:
                continue
            stats["examined"] += 1
            name = str(event_name or "").strip()
            if not name:
                stats["skipped_unnamed"] += 1
                continue
            family = canonical_sport(str(sport or "?"))
            # window on the ADVERTISED start when the feed carried one — futures are
            # captured months before they run, so capture day would scatter the same
            # outright across fixtures-by-capture-date (B3). Exchanges carry no real start,
            # only an END (resolution/expiry) — use it as the day-window PROXY here, but it
            # must NOT become the fixture's start_time (the arb gate would misread it as a
            # future kickoff for a live game). `start_time` stays the real start, or None.
            event_time = start_time or end_time or first_seen
            day = fixture_day(event_time)
            sides = split_sides(name)
            mine: Any = (_tokens(sides[0]), _tokens(sides[1])) if sides else _tokens(name)

            # near-term events disagree by at most a midnight; books PLACEHOLDER
            # far-future outright dates and disagree by days-to-weeks — widen the
            # window once nothing kicks off for a month (the name gate still rules)
            aware = event_time if event_time.tzinfo else event_time.replace(tzinfo=dt.UTC)
            offsets = range(-14, 15) if (aware - now).days > 30 else (-1, 0, 1)

            candidates: list[tuple[float, uuid.UUID]] = []
            near_term = (aware - now).days <= 30
            for offset in offsets:  # books disagree near midnight UTC
                day_key = (
                    (dt.datetime.strptime(day, "%Y-%m-%d") + dt.timedelta(days=offset)).strftime("%Y-%m-%d")
                )
                for fixture_id, theirs, fx_start in index.get((family, day_key), []):
                    # SAME-DAY DOUBLEHEADERS (MLB): identical teams, starts hours
                    # apart, are different games — when both advertise a real
                    # start and they disagree by >3h, never merge. BASEBALL
                    # ONLY: no other covered sport plays twice a day, and books
                    # flip between kickoff and placeholder stamps hours apart —
                    # the gate split State of Origin across four fixtures and
                    # every cross-book board read one book (lived: 2026-07-07).
                    # Far-future placeholder dates are exempt anyway.
                    if (near_term and family == "baseball"
                            and start_time is not None and fx_start is not None):
                        fx_aware = fx_start if fx_start.tzinfo else fx_start.replace(tzinfo=dt.UTC)
                        if abs((aware - fx_aware).total_seconds()) > 3 * 3600:
                            continue
                    if isinstance(mine, tuple) and isinstance(theirs, tuple):
                        score = _sides_score(mine, theirs)
                        threshold = MATCH_THRESHOLD
                    elif isinstance(mine, frozenset) and isinstance(theirs, frozenset):
                        if _side_ok(mine, theirs):  # every shorter-name token has a partner
                            union = mine | theirs
                            score = len(mine & theirs) / len(union) if union else 0.0
                        else:
                            score = 0.0
                        threshold = NAME_THRESHOLD
                    else:
                        continue  # two-sided vs one-name shapes never match
                    if score >= threshold:
                        candidates.append((score, fixture_id))

            distinct = {fid for _s, fid in candidates}
            if len(distinct) > 1:
                best = sorted(candidates, reverse=True)
                if best[0][0] - best[1][0] < 0.15:  # no clear winner → never guess
                    stats["ambiguous"] += 1
                    continue
                target = best[0][1]
            elif len(distinct) == 1:
                target = next(iter(distinct))
            else:
                fixture = Fixture(
                    sport=family,
                    external_id=f"{provider}:{external_id}",  # founding book's key
                    name=f"{sides[0]} v {sides[1]}" if sides else name,
                    # the REAL start only (None for an exchange-founded fixture), so the arb
                    # in-play gate can't be fooled by an end-as-start; `end_time` keeps the
                    # day-window proxy for matching.
                    start_time=start_time,
                    end_time=end_time,
                    # No time at all (no advertised start, no expiry): persist the
                    # founding event's day so later passes index this fixture where
                    # it was seen, not wherever "now" happens to be (_fixture_day).
                    meta={"day_proxy": day} if start_time is None and end_time is None else {},
                )
                session.add(fixture)
                await session.flush()
                target = fixture.id
                fixtures_by_id[target] = fixture
                add_to_index(target, family, day, fixture.name, fixture.start_time)
                stats["created"] += 1

            # Backfill a real start onto a fixture founded without one (e.g. an exchange
            # founded it, then a book with a real kickoff joins) so exchange-vs-book arbs
            # can pass the in-play gate.
            fx = fixtures_by_id.get(target)
            if fx is not None and fx.start_time is None and start_time is not None:
                fx.start_time = start_time
            session.add(Event(fixture_id=target, provider=str(provider), external_id=str(external_id)))
            mapped_keys.add((provider, external_id))
            stats["mapped"] += 1

        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return stats


async def map_events_to_fixtures(
    session_factory: async_sessionmaker[AsyncSession],
    items: list[dict[str, Any]],
) -> dict[str, int]:
    """Map externally-sourced events (scoreboard results) onto EXISTING fixtures —
    same matching rules as resolve_events, but never founds a fixture: a result for
    a game no book priced settles nothing and would only add noise.

    items: [{provider, external_id, event_name, sport, event_time?}]
    """
    async with session_factory() as session:
        mapped_keys = {
            (provider, external_id)
            for provider, external_id in (
                await session.execute(select(Event.provider, Event.external_id))
            ).all()
        }
        fixtures = (await session.execute(select(Fixture))).scalars().all()
        index: dict[tuple[str, str], list[tuple[uuid.UUID, Any]]] = {}
        for fixture in fixtures:
            sides = split_sides(fixture.name)
            keyed = (_tokens(sides[0]), _tokens(sides[1])) if sides else _tokens(fixture.name)
            index.setdefault((fixture.sport, _fixture_day(fixture)), []).append((fixture.id, keyed))

        stats = {"examined": 0, "mapped": 0, "ambiguous": 0, "unmatched": 0}
        for item in items:
            provider, external_id = str(item["provider"]), str(item["external_id"])
            if (provider, external_id) in mapped_keys:
                continue
            stats["examined"] += 1
            name = str(item.get("event_name") or "")
            family = canonical_sport(str(item.get("sport") or "?"))
            event_time = item.get("event_time") or dt.datetime.now(dt.UTC)
            sides = split_sides(name)
            mine: Any = (_tokens(sides[0]), _tokens(sides[1])) if sides else _tokens(name)

            candidates: list[tuple[float, uuid.UUID]] = []
            for offset in (-1, 0, 1):
                day_key = (event_time + dt.timedelta(days=offset)).strftime("%Y-%m-%d")
                for fixture_id, theirs in index.get((family, day_key), []):
                    if isinstance(mine, tuple) and isinstance(theirs, tuple):
                        score = _sides_score(mine, theirs)
                        threshold = MATCH_THRESHOLD
                    elif isinstance(mine, frozenset) and isinstance(theirs, frozenset):
                        score = (
                            len(mine & theirs) / len(mine | theirs)
                            if _side_ok(mine, theirs) and (mine | theirs) else 0.0
                        )
                        threshold = NAME_THRESHOLD
                    else:
                        continue
                    if score >= threshold:
                        candidates.append((score, fixture_id))

            distinct = {fid for _s, fid in candidates}
            if len(distinct) > 1:
                best = sorted(candidates, reverse=True)
                if best[0][0] - best[1][0] < 0.15:
                    stats["ambiguous"] += 1
                    continue
                target = best[0][1]
            elif len(distinct) == 1:
                target = next(iter(distinct))
            else:
                stats["unmatched"] += 1
                continue
            session.add(Event(fixture_id=target, provider=provider, external_id=external_id))
            mapped_keys.add((provider, external_id))
            stats["mapped"] += 1
        await session.commit()
    return stats


async def cross_book_prices(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fixture_id: str,
    market: str = "h2h",
) -> dict[str, Any]:
    """The latest price per (book, selection) for one fixture across every mapped
    book — the query event resolution exists to make possible."""
    from sportsdata_agents.data.models import Price

    async with session_factory() as session:
        mappings = (
            await session.execute(select(Event).where(Event.fixture_id == uuid.UUID(fixture_id)))
        ).scalars().all()
        fixture = await session.get(Fixture, uuid.UUID(fixture_id))
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for mapping in mappings:
            rows = (
                await session.execute(
                    select(Price)
                    .where(
                        Price.provider == mapping.provider,
                        Price.event_external_id == mapping.external_id,
                        Price.market == market,
                    )
                    .order_by(Price.changed_at)
                )
            ).scalars().all()
            for row in rows:  # ordered ascending — the last write per key wins
                latest[(row.book, row.selection)] = {
                    "book": row.book,
                    "selection": row.selection,
                    "odds": float(row.odds),
                    "changed_at": row.changed_at.isoformat(),
                }
    by_selection: dict[str, list[dict[str, Any]]] = {}
    for entry in latest.values():
        by_selection.setdefault(entry["selection"], []).append(entry)
    for entries in by_selection.values():
        entries.sort(key=lambda e: -e["odds"])  # best price first
    return {
        "fixture": fixture.name if fixture else fixture_id,
        "market": market,
        "books": len({m.provider for m in mappings}),
        "selections": by_selection,
    }
