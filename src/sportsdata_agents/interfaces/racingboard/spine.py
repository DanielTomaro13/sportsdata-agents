"""The race universe: the union of every book's card, not TAB's alone.

`discover_races` used to read `tab_racing_meetings` and nothing else, so the board
could never show a race TAB did not carry — and TAB carries the smallest card of the
five (413 races against PointsBet's 1,200, and the smallest in every one of R, G and
H). That was a ceiling on coverage that no amount of extra price sources could lift,
because a race that never entered the spine was never asked about.

Here every book contributes. TAB becomes one contributor among several rather than the
authority, and its `venueMnemonic` — which does not exist for a race TAB does not carry
— goes back to being TAB's own handle instead of the board's identity.

## What a race is identified by

`(code, venue, race_no, date)`, with the venue resolved through `venues.py` rather than
compared as a string. Two books naming `Northfield Park` and `Northfield Pk` are one
race; `Woodbine` and `Woodbine Mohawk Park` are two, and the code gate keeps them apart
even before the names are considered.

## Why Dabble prices but does not discover

Dabble publishes no race number — its fixtures carry a race NAME and an
`advertisedStart` and nothing ordering them within the meeting. A race discovered only
by Dabble could therefore not be numbered, and a board row with no race number is not
usable. So Dabble joins existing races by start time (see `CorporateBook.handle_for`)
and contributes prices to all three codes, while the four numbered books define the
universe. It loses nothing in practice: Dabble's meetings overlap the others almost
entirely — what it adds is a fifth price, which is what it is here for.
"""

from __future__ import annotations

import time
from typing import Iterable

from .config import settings
from .corporate import BookRace, CorporateBook
from .engine import SportsDataEngine
from .models import RaceRef
from .venues import unique_venue_match, venue_tokens


def _pick_display_venue(names: list[str]) -> str:
    """The most informative spelling in a cluster.

    Longest by token count, then alphabetical for determinism. `Woodbine Mohawk Park`
    is a better label than `MOHAWK`, and picking deterministically matters because the
    display name is what the venue resolver is handed on every later lookup.
    """
    return sorted(names, key=lambda n: (-len(venue_tokens(n)), -len(n), n))[0]


def _split_by_start(members: list[tuple[str, BookRace]]) -> list[list[tuple[str, BookRace]]]:
    """Separate two meetings at one track, which share a venue AND a race number.

    Books agree closely on a scheduled time, and two cards at one track are hours
    apart, so a simple gap-based split is unambiguous rather than a fine judgement.
    Entries with no start ride along with the earliest group: they cannot be placed,
    and dropping them would lose a book that simply did not publish a time.
    """
    timed = sorted([m for m in members if m[1].start is not None], key=lambda m: m[1].start or 0)
    untimed = [m for m in members if m[1].start is None]
    if not timed:
        return [members] if members else []

    groups: list[list[tuple[str, BookRace]]] = [[timed[0]]]
    for m in timed[1:]:
        prev = groups[-1][-1][1].start or 0
        if (m[1].start or 0) - prev <= settings.start_tolerance_seconds:
            groups[-1].append(m)
        else:
            groups.append([m])
    groups[0].extend(untimed)
    return groups


def cluster_races(book_races: Iterable[tuple[str, BookRace]]) -> list[RaceRef]:
    """Fold every book's races into one canonical list.

    `book_races` is (book_name, BookRace) pairs. Only races carrying a race number take
    part — see the module docstring on Dabble.

    Clustering runs inside one `(code, race_no)` bucket at a time, which is what makes
    it safe: two tracks can only ever be confused with each other if they also share a
    code and a race number, and the venue rules then have to agree as well.
    """
    buckets: dict[tuple[str, int], list[tuple[str, BookRace]]] = {}
    for book, br in book_races:
        if br.race_no is None:
            continue
        buckets.setdefault((br.code, br.race_no), []).append((book, br))

    out: list[RaceRef] = []
    for (code, race_no), entries in buckets.items():
        # Exact token-set groups first. `RICCARTON` and `Riccarton Park` reduce to the
        # same tokens, so they land together here and never reach the ambiguous path.
        groups: dict[frozenset[str], list[tuple[str, BookRace]]] = {}
        for book, br in entries:
            toks = venue_tokens(br.venue)
            if toks:
                groups.setdefault(toks, []).append((book, br))

        # Then merge a group into another when its name is uniquely contained by it —
        # `MOHAWK` into `Woodbine Mohawk Park`. Unique or not at all: if two groups
        # could absorb it, it stays separate rather than being guessed into one.
        keys = sorted(groups, key=lambda k: (-len(k), sorted(k)))
        merged: dict[frozenset[str], frozenset[str]] = {}
        for k in keys:
            others = [(" ".join(sorted(o)), o) for o in keys if o != k and o not in merged]
            target = unique_venue_match(" ".join(sorted(k)), others)
            if target is not None and k < target:
                merged[k] = target

        clusters: dict[frozenset[str], list[tuple[str, BookRace]]] = {}
        for k, members in groups.items():
            clusters.setdefault(merged.get(k, k), []).extend(members)

        for members in clusters.values():
            # One track can hold TWO meetings in a day — Townsville's day and night
            # greyhound cards each have a race 1 — so a venue cluster is split again by
            # start time. Without this the two would fold into one race and the board
            # would show one card's prices under the other's runners.
            for group in _split_by_start(members):
                names = [br.venue for _, br in group]
                starts = [br.start for _, br in group if br.start is not None]
                venue = _pick_display_venue(names)
                mnem = next((br.handle[1] for book, br in group
                             if book == "tab" and isinstance(br.handle, tuple)), None)
                out.append(RaceRef(
                    race_key="",  # assigned below, once the canonical venue is settled
                    code=code,
                    venue=venue,
                    venue_mnem=mnem,
                    race_no=race_no,
                    race_name=next((br.name for _, br in group if br.name), ""),
                    start_time="",
                    date="",
                    start_epoch=min(starts) if starts else None,
                    books=sorted({book for book, _ in group}),
                ))
    return out


async def discover_races(
    engine: SportsDataEngine, date: str, books: list[CorporateBook]
) -> list[RaceRef]:
    """Every race any book has for `date`, inside the jump horizon.

    Indices are assumed already refreshed by the caller (the poller does this on the
    discovery loop), so this is pure assembly and does no I/O of its own.
    """
    from datetime import datetime

    pairs: list[tuple[str, BookRace]] = [
        (b.name, br) for b in books for br in b.races
    ]
    races = cluster_races(pairs)

    now = time.time()
    horizon = now + settings.horizon_minutes * 60
    kept: list[RaceRef] = []
    for r in races:
        if r.start_epoch is None:
            continue
        # Upcoming and inside the horizon, plus a grace window so a race stays on the
        # board through the jump rather than vanishing at the moment it matters most.
        if r.start_epoch < now - settings.past_grace_seconds or r.start_epoch > horizon:
            continue
        r.date = date
        r.start_time = datetime.fromtimestamp(r.start_epoch).astimezone().isoformat()
        r.race_key = f"{r.code}:{r.venue_key}:{r.race_no}:{date}"
        kept.append(r)

    kept.sort(key=lambda r: r.start_epoch or 0)
    return kept
