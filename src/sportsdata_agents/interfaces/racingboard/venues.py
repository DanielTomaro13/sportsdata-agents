"""Deciding when two books are naming the same track, and the same runner.

This is the join key for the whole board. Every book discovers its own races and
spells the venue its own way, so a race only gets a cross-book price line if the
names land together here. It used to be `_venue_compatible` — equal, or one an
at-least-5-character prefix of the other — which is why `MOHAWK` never reached
`Woodbine Mohawk Park` and harness read as 1-of-11 covered when the books had
essentially all of it.

## Why this is not simply "match more loosely"

`Woodbine` and `Woodbine Mohawk Park` are DIFFERENT TRACKS — one thoroughbred, one
harness, a few minutes apart in Ontario. A looser matcher that merges them shows a
price from the wrong race, which is worse than showing a gap: a gap is visible and a
wrong price is not. It is the same failure the fixture resolver had when a Women's
fixture merged with the men's and manufactured a 74% arbitrage out of nothing.

So the rule is EXACT FIRST, THEN UNIQUE SUBSET:

    1. A candidate whose token set is exactly the target's wins outright.
    2. Otherwise a candidate whose tokens are a superset (or subset) of the target's
       wins ONLY IF it is the only such candidate.
    3. Otherwise: no match. Ambiguity refuses.

That resolves both directions of the Woodbine problem with one rule and no special
cases. Matching `MOHAWK` finds no exact, and exactly one superset — `Woodbine Mohawk
Park` — so it joins. Matching `WOODBINE` finds an exact — `Woodbine` — and never
reaches the subset stage, so it cannot be captured by the longer name. Had both been
inexact and plural, it would refuse rather than guess.

The code (R/G/H) and race number are checked by the caller as a second, independent
gate, so a name collision across disciplines cannot merge even if the tokens agree.
"""

from __future__ import annotations

import re
from typing import Iterable, TypeVar

T = TypeVar("T")

#: Country and region tags books append. Stripped before tokenising: `Bathurst (AUS)`
#: and `Bathurst` are one track.
_COUNTRY_RE = re.compile(r"\((?:aus|nz|nzl|usa|us|can|gb|uk|ire|irl|fr|sa|hk|jpn|jp|sgp|uae)\)")

#: Discipline suffixes, which books use to disambiguate a shared name on THEIR side.
#: They carry real meaning, but the code gate already separates disciplines, and
#: keeping them would stop `Saratoga` meeting `Saratoga TB`.
_DISCIPLINE_TOKENS = frozenset({"tb", "tf", "gh", "hr", "th", "gr"})

#: Tokens that add no identity — a book writing `Riccarton Park` for `RICCARTON` is
#: the same track. Dropped so the two token sets compare equal rather than by subset,
#: which keeps them out of the ambiguity path entirely.
_GENERIC_TOKENS = frozenset({
    "park", "pk", "racecourse", "raceway", "raceway's", "course", "track",
    "downs", "racing", "races", "raceourse", "synthetic", "syn", "turf",
    "greyhounds", "greyhound", "harness", "trots", "paceway", "the",
    # Connectives, mostly from Spanish and French track names: `Club Hipico DE
    # Santiago` must reduce to the same identity a book spelling it `Santiago` does.
    "de", "du", "la", "le", "of", "at", "and",
    # Quarter-horse designations: `Los Alamitos Qh` and `LOS ALAMITOS QTR HORSE` are
    # one track spelled by two books.
    "qh", "qtr", "quarter", "horse", "horses",
})

#: Abbreviations books use in place of the full word.
_EXPANSIONS = {
    "pk": "park", "jnc": "junction", "jct": "junction", "st": "saint",
    "mt": "mount", "nth": "north", "sth": "south", "hgts": "heights",
    "vly": "valley", "crk": "creek", "spr": "springs", "gdns": "gardens",
}

#: Names tokens alone cannot bridge. Deliberately small and kept as data: every entry
#: is a place the general rules could not reach, so the list is the honest record of
#: where they fall short. Keys and values are both normalised before use.
_ALIASES: dict[str, str] = {
    "mohawk": "woodbine mohawk",
    "sportsbet sandown": "sandown",
    "sportsbet ballarat": "ballarat",
    "ladbrokes cannington": "cannington",
    "the meadows": "meadows",
    "the gardens": "gardens",
    "aquis park gold coast": "gold coast",
    "sunshine coast poly": "sunshine coast",
    # Contractions. Tokens cannot bridge these — `medw` is not a prefix of `meadows`,
    # it is a squeeze of it — and guessing at squeezes in general would merge tracks
    # that merely look alike. So they are listed, which is what the table is for.
    "medw prairie": "prairie meadows",
    "dwns prairie": "prairie meadows",
    "club hipico santiago": "santiago",
    "club hipico": "santiago",
    "downs remington": "remington park",
    "pk woodbine": "woodbine",
}


#: A month is only noise when it is part of a DATE. Stripping the bare word deleted
#: `Del Mar` down to nothing — March ate the track — and would have done the same to
#: `May`, `Augusta` and anything else starting with a month's letters. A venue that
#: normalises to an empty token set matches nothing and is silently absent from the
#: board, which is the worst shape a bug can take here.
_DATE_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?\b"
)


def _strip_noise(name: str) -> str:
    n = name.lower().strip()
    n = _COUNTRY_RE.sub(" ", n)
    # Any remaining bracketed qualifier: '(2nd Meeting)', '(AUS)'.
    n = re.sub(r"\([^)]*\)", " ", n)
    n = _DATE_RE.sub(" ", n)
    n = re.sub(r"\b\d+(?:st|nd|rd|th)?\b", " ", n)
    return n


def venue_tokens(name: str) -> frozenset[str]:
    """The identity-bearing words of a venue name.

    Generic and discipline words are dropped rather than kept-and-tolerated. That is
    what lets `RICCARTON` and `Riccarton Park` compare EQUAL instead of by subset —
    an exact match never enters the ambiguity path, so a third track sharing the stem
    cannot make a settled pair suddenly ambiguous.
    """
    n = _strip_noise(name)
    raw = [t for t in re.split(r"[^a-z]+", n) if t]
    raw = [_EXPANSIONS.get(t, t) for t in raw]
    tokens = {t for t in raw if t not in _GENERIC_TOKENS and t not in _DISCIPLINE_TOKENS}
    # An alias is applied on the reduced form so it survives punctuation differences.
    joined = " ".join(sorted(tokens))
    if joined in _ALIASES:
        tokens = {t for t in _ALIASES[joined].split() if t}
    return frozenset(tokens)


def norm_venue(name: str) -> str:
    """A stable, comparable string form — the canonical venue key.

    Joined with a hyphen, not a space, because this string becomes the middle of
    `race_key` and `race_key` becomes a URL path segment (`/api/race/{key}`). A
    space there is not merely ugly: urllib refuses the request outright, so every
    multi-word track — Prairie Meadows, Kentucky Downs, Woodbine Mohawk Park —
    would be unreachable for any client that builds a URL from the key.
    """
    return "-".join(sorted(venue_tokens(name)))


def venues_match(a: str, b: str) -> bool:
    """Same track? Exact token equality, or one set containing the other.

    Containment ALONE is not safe (`Woodbine` ⊂ `Woodbine Mohawk Park`), so this is
    only ever used by `unique_venue_match`, which additionally requires the containing
    match to be the only one. Exposed for tests and callers that already hold a
    single candidate.
    """
    ta, tb = venue_tokens(a), venue_tokens(b)
    if not ta or not tb:
        return False
    return ta == tb or ta <= tb or tb <= ta


def unique_venue_match(
    target: str, candidates: Iterable[tuple[str, T]]
) -> T | None:
    """The one candidate naming the same track as `target`, or None.

    `candidates` is (name, value) pairs. Returns the value, so callers can carry a
    book handle through. Refuses — returns None — whenever the answer is not unique,
    because a wrong join shows a price from the wrong race and a missing join only
    shows a gap.
    """
    tt = venue_tokens(target)
    if not tt:
        return None

    exact = [v for name, v in candidates if venue_tokens(name) == tt]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # the book lists the same name twice; nothing here can pick

    subset = [
        v for name, v in candidates
        if (tn := venue_tokens(name)) and (tt < tn or tn < tt)
    ]
    return subset[0] if len(subset) == 1 else None


# ─── runners ────────────────────────────────────────────────────────────

#: Country-of-origin suffixes on a horse's name — `Jadzia (NZ)`. A convention on
#: imported and international runners, which is exactly the coverage the multi-source
#: spine adds. The old normaliser stripped a leading saddlecloth number but NOT this,
#: so `Jadzia (NZ)` reduced to `jadzianz` and never met `Jadzia`.
_RUNNER_SUFFIX_RE = re.compile(r"\((?:[a-z]{2,4})\)")


def norm_runner(name: str) -> str:
    """'1. Chix Diggus' / 'CHIX DIGGUS' / 'Jadzia (NZ)' -> comparable form."""
    n = name.lower().strip()
    n = re.sub(r"^\s*\d+[.)]\s*", "", n)      # leading saddlecloth number
    n = _RUNNER_SUFFIX_RE.sub(" ", n)          # country of origin
    return re.sub(r"[^a-z0-9]", "", n)
