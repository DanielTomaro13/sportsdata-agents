"""Tag player-prop price points with structured meta at the ingest door.

The stat-ladder machinery (stat_value watch, engine stat fairs) reads
``meta.player / meta.stat / meta.stat_line / meta.line_type`` — Dabble's feed
carries those natively, every other book buries the same information in
market/selection NAMES. This tagger recognises the two prop shapes AU books
actually use and adds the structured meta, so any book's props join the
ladder pipeline without per-book code:

- market "marcus bontempelli disposals", selection "over 24.5" / "under 24.5"
- market "player points", selection "nathan cleary 20+ points" (N-plus form:
  20+ means "20 or more", i.e. over 19.5)

Conservative by design: an untagged prop is a missed ladder; a MIS-tagged
market is a phantom ladder the fit will price — so anything ambiguous stays
untagged. The stat vocabulary is the engines' stat catalog families."""

from __future__ import annotations

import re

# the stat names books put in prop market/selection labels (single source of
# truth for what counts as a prop; longest first so "try assists" beats "tries")
_STAT_WORDS = sorted((
    "disposals", "kicks", "handballs", "marks", "tackles", "goals", "behinds",
    "hitouts", "clearances", "inside 50s", "fantasy points", "supercoach points",
    "tries", "try assists", "run metres", "tackle busts", "offloads",
    "line breaks", "kick metres", "all run metres",
    "points", "rebounds", "assists", "threes", "three pointers", "steals",
    "blocks", "turnovers",
    "passing yards", "rushing yards", "receiving yards", "receptions",
    "completions", "touchdowns",
    "strikeouts", "hits", "home runs", "total bases", "rbis", "stolen bases",
    "shots", "shots on target", "shots on goal", "saves", "passes", "crosses",
    "aces", "double faults", "games won",
    "one eighties", "180s", "centuries",
    "significant strikes", "takedowns", "knockdowns",
    "birdies", "eagles", "bogeys",
), key=len, reverse=True)
_STATS_ALT = "|".join(re.escape(s) for s in _STAT_WORDS)

# a player label: 2-4 capitalisable word tokens (letters, dots, hyphens,
# apostrophes), no digits — "nathan cleary", "t. trbojevic", "de goey".
# LAZY repetition: the stat alternation gets first claim on trailing words,
# so "latrell mitchell try assists" splits at "try assists", not "assists"
_NAME = r"[a-z][a-z.'-]*(?: [a-z][a-z.'-]*){1,3}?"

# market "<player> <stat>" (optional dash), selection "over/under N[.5]"
_MARKET_PROP = re.compile(rf"^(?P<player>{_NAME})\s*[-\u2013:]?\s*(?P<stat>{_STATS_ALT})$")
_OU_SELECTION = re.compile(r"^(?P<side>over|under)\s+(?P<line>\d{1,3}(?:\.5)?)$")

# selection "<player> N+ <stat>" — the N-plus ladder form
_NPLUS_SELECTION = re.compile(
    rf"^(?P<player>{_NAME})\s+(?P<n>\d{{1,3}})\+\s+(?P<stat>{_STATS_ALT})$")

# market "N+ <stat>" / "to kick N+ goals" with the PLAYER as the selection —
# TAB's ladder shape (market "25+ Disposals", selection "Nick Daicos")
_NPLUS_MARKET = re.compile(
    rf"^(?:to (?:kick|score|get|have) )?(?P<n>\d{{1,3}})\+\s+(?P<stat>{_STATS_ALT})$")

# a cheap pre-filter: no stat word anywhere → not a prop, skip the regexes
_ANY_STAT = re.compile(rf"\b(?:{_STATS_ALT})\b")


def tag_prop(market: str, selection: str, meta: dict) -> dict:
    """The point's meta, with player/stat/stat_line/line_type added when the
    market/selection names carry a recognisable prop shape. Already-tagged
    points (Dabble) and non-props pass through untouched."""
    if meta.get("player"):
        return meta
    market_l = market.strip().lower()
    selection_l = selection.strip().lower()
    if not (_ANY_STAT.search(market_l) or _ANY_STAT.search(selection_l)):
        return meta

    matched = _NPLUS_SELECTION.match(selection_l)
    if matched:
        # "20+" means 20 or more — the over side of a 19.5 line
        return {**meta, "player": matched.group("player").title(),
                "stat": matched.group("stat"),
                "stat_line": float(matched.group("n")) - 0.5,
                "line_type": "over", "prop_tagged": True}

    market_nplus = _NPLUS_MARKET.match(market_l)
    if market_nplus and selection_l and not any(ch.isdigit() for ch in selection_l):
        # TAB inverts the ladder: the threshold IS the market, the player the
        # selection ("25+ Disposals" / "Nick Daicos")
        return {**meta, "player": selection.strip().title(),
                "stat": market_nplus.group("stat"),
                "stat_line": float(market_nplus.group("n")) - 0.5,
                "line_type": "over", "prop_tagged": True}

    market_match = _MARKET_PROP.match(market_l)
    side_match = _OU_SELECTION.match(selection_l)
    if market_match and side_match:
        return {**meta, "player": market_match.group("player").title(),
                "stat": market_match.group("stat"),
                "stat_line": float(side_match.group("line")),
                "line_type": side_match.group("side"), "prop_tagged": True}
    return meta
