"""`--coverage`: how much of the board each book is actually reaching.

"The board looks thin" has no symptom of its own. A venue normalisation that quietly
stops matching removes a book's column and looks exactly like a quiet racing day; the
prices are all still there, they simply stop joining. That is the same shape as market-
dictionary drift, and it needs the same answer — a number, printed on demand.

It also distinguishes the two reasons a book can be missing from a race, which look
identical on the board and are not remotely the same problem:

    absent     the book does not have that race at all — a catalogue limit, nothing
               to fix. Dabble is AU-focused, so a card full of US harness is simply
               not theirs.
    unmatched  the book HAS the venue and the race did not join — a resolver gap, and
               the thing worth acting on.

Run it with:  python -m sportsdata_agents.interfaces.racingboard --coverage
"""

from __future__ import annotations

import datetime as dt

from .corporate import CorporateSource, TabBook, build_books
from .engine import SportsDataEngine
from .models import RaceRef
from .spine import cluster_races, discover_races
from .venues import venue_tokens


def _reason(book, race: RaceRef) -> str:
    """Why this book is not on this race."""
    if book.handle_for(race) is not None:
        return "covered"
    want = venue_tokens(race.venue)
    # Does the book have this venue AND this race number at a compatible time? If the
    # race number is absent from its card the race genuinely is not there — the most
    # common case being a book that also lists tomorrow, whose R8 is a different race.
    for br in book.races:
        if br.code != race.code or not (venue_tokens(br.venue) & want):
            continue
        if br.race_no is not None and br.race_no != race.race_no:
            continue
        if br.start is not None and race.start_epoch is not None:
            if abs(br.start - race.start_epoch) > 3600:
                continue
        return "unmatched"
    return "absent"


async def report(date: str | None = None) -> int:
    date = date or dt.date.today().isoformat()
    engine = SportsDataEngine()
    books = [TabBook(), *build_books()]
    source = CorporateSource(books=books)
    await source.refresh_indices(engine, date)

    everything = cluster_races([(b.name, br) for b in books for br in b.races])
    active = await discover_races(engine, date, books)

    print(f"\nracing board coverage — {date}\n")
    print("  indexed per book")
    for b in books:
        by: dict[str, int] = {}
        for r in b.races:
            by[r.code] = by.get(r.code, 0) + 1
        codes = " ".join(f"{c}={by.get(c, 0):<4}" for c in "RGH")
        print(f"    {b.name:<10} {len(b.races):>5}   {codes}")

    by_code: dict[str, int] = {}
    for r in everything:
        by_code[r.code] = by_code.get(r.code, 0) + 1
    print(f"\n  UNION SPINE  {len(everything)} races  "
          f"({' '.join(f'{c}={by_code.get(c, 0)}' for c in 'RGH')})")
    tab_less = sum(1 for r in everything if "tab" not in r.books)
    print(f"    of which TAB does not carry: {tab_less}  "
          f"— the races the old TAB-only spine could never show")

    if not active:
        print("\n  nothing inside the horizon right now (try during racing hours)\n")
        return 0

    print(f"\n  inside the {len(active)}-race horizon:")
    print(f"    {'book':<10} {'covered':>8} {'absent':>8} {'unmatched':>10}")
    worst = 0
    for b in books:
        counts = {"covered": 0, "absent": 0, "unmatched": 0}
        for r in active:
            counts[_reason(b, r)] += 1
        worst = max(worst, counts["unmatched"])
        pct = counts["covered"] / len(active)
        print(f"    {b.name:<10} {counts['covered']:>5} {pct:>5.0%} "
              f"{counts['absent']:>8} {counts['unmatched']:>10}")

    per_race = [sum(1 for b in books if b.handle_for(r) is not None) for r in active]
    dist: dict[int, int] = {}
    for n in per_race:
        dist[n] = dist.get(n, 0) + 1
    print(f"\n    books per race: {dict(sorted(dist.items()))}")
    print(f"    races with no book at all: {dist.get(0, 0)}\n")
    return worst
