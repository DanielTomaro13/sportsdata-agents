"""Operator coverage preferences: where the EXPENSIVE collection budgets go.

The hot tier keeps its cheap wide net (primary-market listings feed event
resolution and the cross-book boards); what this module gates is the
books-tier DETAIL spend — per-event firehoses of hundreds of markets — plus
the racing feeds' geography. A user picks the sports and competitions they
actually bet; everything else still gets primaries, just not the firehose.

Preferences are a JSON object {sport_family: [competition tokens]} — an empty
token list means EVERY competition of that sport; a sport absent from the map
gets no detail budget at all. The shipped default is the operator's own
selection; override with the ``SPORTSDATA_AGENTS_COVERAGE`` env var (same
shape). Tokens match case-insensitively as substrings of the book's own
competition/league label ("nba" matches "NBA", "NBA Summer League" — narrow
with longer tokens if that's too wide).
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

# The operator's selection (2026-07-07). Sports not listed get NO detail
# budget; empty lists mean every competition of that sport.
DEFAULT_COVERAGE: dict[str, list[str]] = {
    "baseball": ["mlb", "major league baseball"],
    "basketball": ["nba", "wnba", "nbl"],
    "australian_rules": ["afl"],  # the AFL token also covers AFLW labels
    "rugby_league": ["nrl", "origin"],  # NRLW carries the NRL token
    "ice_hockey": ["nhl"],
    "tennis": [],  # every tour — doubles are filtered per fixture instead
    "mma": ["ufc"],
    "golf": [],
    "darts": [],
    "snooker": [],
    # racing: every AU/NZ code (geography is RACING_COUNTRIES, not tokens)
    "horse_racing": [],
    "thoroughbred_racing": [],
    "greyhound_racing": [],
    "harness_racing": [],
}

# NZ meetings ride the AU tote circuit and every AU book cards them — they
# count as "Australian racing" for coverage purposes.
RACING_COUNTRIES = ("AUS", "NZL", "NZ")

# book sport labels -> the family keys DEFAULT_COVERAGE speaks (labels the
# sink's _CANON_SPORT doesn't already fold arrive here raw)
_FAMILY_ALIASES = {
    "aussie_rules": "australian_rules",
    "afl": "australian_rules",
    "aussie_rules_football": "australian_rules",
    "hockey": "ice_hockey",
    "ufc": "mma",
    "ufc_mma": "mma",
    "martial_arts": "mma",
    "mixed_martial_arts": "mma",
    "nba": "basketball",
    "wnba": "basketball",
    "nbl": "basketball",
    "mlb": "baseball",
    "nhl": "ice_hockey",
    "pga": "golf",
    "racing": "horse_racing",
}

_DOUBLES = re.compile(r"\w\s*/\s*\w")  # "Krejcikova/Siniakova v …"


@lru_cache(maxsize=1)
def _prefs() -> dict[str, list[str]]:
    raw = os.environ.get("SPORTSDATA_AGENTS_COVERAGE", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            return {str(k).lower(): [str(t).lower() for t in (v or [])]
                    for k, v in parsed.items()}
        except (ValueError, AttributeError, TypeError) as e:
            logger.warning("SPORTSDATA_AGENTS_COVERAGE unparseable (%s) — "
                           "using the shipped default", e)
    return DEFAULT_COVERAGE


def _family(sport_label: str) -> str:
    label = str(sport_label or "").strip().lower().replace(" ", "_")
    return _FAMILY_ALIASES.get(label, label)


def sport_covered(sport_label: str) -> bool:
    """Does this sport get ANY detail budget?"""
    return _family(sport_label) in _prefs()


def competition_covered(sport_label: str, competition: str) -> bool:
    """Does this competition get detail budget? Unknown sports: no. A sport
    with an empty token list: every competition."""
    tokens = _prefs().get(_family(sport_label))
    if tokens is None:
        return False
    if not tokens:
        return True
    comp = str(competition or "").lower()
    return any(t in comp for t in tokens)


def fixture_covered(sport_label: str, fixture_name: str) -> bool:
    """Per-fixture refinements: tennis is SINGLES only (a doubles pairing
    names its teams "A/B v C/D")."""
    return not (_family(sport_label) == "tennis"
                and _DOUBLES.search(str(fixture_name or "")))


_VENUE_COUNTRY = re.compile(r"\(([A-Za-z]{2,3})\)")


def racing_event_covered(event_name: str) -> bool:
    """AU/NZ racing only — international meetings (Betfair cards the world)
    carry their country in the venue label: "Ascot (GB) 7th Jul". A label
    with no tag is a local book's card, always covered."""
    tag = _VENUE_COUNTRY.search(str(event_name or ""))
    return tag is None or tag.group(1).upper() in RACING_COUNTRIES
