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
    "player performance",
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
_MARKET_PROP = re.compile(rf"^(?P<player>{_NAME})\s*[-\u2013:]?\s*(?P<stat>{_STATS_ALT})"
                          rf"(?:\s+(?P<mline>\d{{1,3}}(?:\.5)?))?$")
_OU_SELECTION = re.compile(r"^(?P<side>over|under)\s+(?P<line>\d{1,3}(?:\.5)?)$")

# selection "<player> N+ <stat>" — the N-plus ladder form
_NPLUS_SELECTION = re.compile(
    rf"^(?P<player>{_NAME})\s+(?P<n>\d{{1,3}})\+\s+(?P<stat>{_STATS_ALT})$")

# market "N+ <stat>" / "to kick N+ goals" with the PLAYER as the selection —
# TAB's ladder shape (market "25+ Disposals", selection "Nick Daicos")
_NPLUS_MARKET = re.compile(
    rf"^(?:to (?:kick|score|get|have) )?(?P<n>\d{{1,3}})\+\s+(?P<stat>{_STATS_ALT})$")

# market "<stat> - N+" with the PLAYER as the selection — Ladbrokes' shape
# ("Disposals - 15+" / "Koltyn Thorstrup"). Segment variants ("1st Half
# Disposals - 10+") must NOT tag: a half line pooled with full-game groups
# poisons every fair, and the leading words fail this anchor by design.
_STAT_NPLUS_MARKET = re.compile(
    rf"^(?P<stat>{_STATS_ALT})\s*[-\u2013]\s*(?P<n>\d{{1,3}})\+$")

# a cheap pre-filter: no stat word anywhere → not a prop, skip the regexes
_ANY_STAT = re.compile(rf"\b(?:{_STATS_ALT})\b")

# PointsBet buries the stat in the MARKET (often SINGULAR: "pick your own
# mark", "to get disposals", "to kick goals", "player disposals over/under")
# and prints selections "<player> N+ N" / "<player> over 24.5 24.5" with a
# trailing echo of the line
_MARKET_STAT_SINGULAR = {
    "mark": "marks", "tackle": "tackles", "goal": "goals", "kick": "kicks",
    "handball": "handballs", "disposal": "disposals", "hitout": "hitouts",
    "clearance": "clearances", "try": "tries", "point": "points",
}
_MARKET_ANY_STAT = re.compile(
    rf"\b(?:{_STATS_ALT}|{'|'.join(_MARKET_STAT_SINGULAR)})\b")
_PLAYER_NPLUS_ECHO = re.compile(
    rf"^(?P<player>{_NAME})\s+(?P<n>\d{{1,3}})\+(?:\s+\d{{1,3}}(?:\.\d+)?)?$")
_PLAYER_OU_ECHO = re.compile(
    rf"^(?P<player>{_NAME})\s+(?P<side>over|under)\s+(?P<line>\d{{1,3}}(?:\.5)?)"
    rf"(?:\s+\d{{1,3}}(?:\.\d+)?)?$")


def _stat_from_market(market_l: str) -> str | None:
    hits = _MARKET_ANY_STAT.findall(market_l)
    if not hits:
        return None
    # verb phrases put the stat LAST ("to KICK goals" is a goals market)
    word = hits[-1]
    return _MARKET_STAT_SINGULAR.get(word, word)


# the operator's canonical stat names — books' labels fold onto these
_STAT_CANON = {"player performance": "player performance points",
               "player-performance": "player performance points",
               "180s": "one eighties", "three pointers": "threes"}

# segment qualifiers become part of the STAT ("disposals 1h") so half and
# quarter ladders pool cross-book WITHOUT contaminating full-game fairs
_SEGMENT_QUAL = (
    ("1st half", "1h"), ("first half", "1h"), ("2nd half", "2h"),
    ("second half", "2h"), ("1st quarter", "1q"), ("first quarter", "1q"),
    ("2nd quarter", "2q"), ("3rd quarter", "3q"), ("4th quarter", "4q"),
)

# "anytime goalscorer"/"anytime tryscorer" IS the 0.5-line ladder rung —
# every book prints it, the deepest cross-book prop pool there is. Order-
# dependent variants (first/last/2nd scorer, team-scoped) never tag.
_ANYTIME = re.compile(r"^anytime (goal|try) ?scorer$")


def _canon_stat(stat: str) -> str:
    return _STAT_CANON.get(stat, stat)


def tag_prop(market: str, selection: str, meta: dict) -> dict:
    """The point's meta, with player/stat/stat_line/line_type added when the
    market/selection names carry a recognisable prop shape. Already-tagged
    points (Dabble) and non-props pass through untouched."""
    if meta.get("player"):
        stat = str(meta.get("stat") or "")
        canon = _canon_stat(stat)
        if stat and canon != stat:
            return {**meta, "stat": canon}
        return meta
    market_l = market.strip().lower()
    selection_l = selection.strip().lower()

    # segment-qualified props keep their qualifier IN the stat name
    qual = ""
    for phrase, tag in _SEGMENT_QUAL:
        for pattern in (f" - {phrase}", f"{phrase} "):
            if pattern in market_l or market_l.endswith(f" {phrase}"):
                qual = f" {tag}"
                market_l = (market_l.replace(f" - {phrase}", "")
                            .replace(f"{phrase} ", "").strip())
                break
        if qual:
            break

    anytime = _ANYTIME.match(market_l)
    if anytime and selection_l and not any(ch.isdigit() for ch in selection_l):
        stat = "goals" if anytime.group(1) == "goal" else "tries"
        return {**meta, "player": selection.strip().title(),
                "stat": _canon_stat(stat) + qual, "stat_line": 0.5,
                "line_type": "over", "prop_tagged": True}

    if not (_MARKET_ANY_STAT.search(market_l) or _ANY_STAT.search(selection_l)):
        return meta

    matched = _NPLUS_SELECTION.match(selection_l)
    if matched:
        # "20+" means 20 or more — the over side of a 19.5 line
        return {**meta, "player": matched.group("player").title(),
                "stat": _canon_stat(matched.group("stat")) + qual,
                "stat_line": float(matched.group("n")) - 0.5,
                "line_type": "over", "prop_tagged": True}

    market_nplus = _NPLUS_MARKET.match(market_l)
    if market_nplus and selection_l and not any(ch.isdigit() for ch in selection_l):
        # TAB inverts the ladder: the threshold IS the market, the player the
        # selection ("25+ Disposals" / "Nick Daicos")
        return {**meta, "player": selection.strip().title(),
                "stat": _canon_stat(market_nplus.group("stat")) + qual,
                "stat_line": float(market_nplus.group("n")) - 0.5,
                "line_type": "over", "prop_tagged": True}

    # PointsBet: stat in the market, player + threshold (+ echo) as selection
    market_stat = _stat_from_market(market_l)
    if market_stat and not any(seg in market_l for seg in
                               ("1st", "2nd", "3rd", "4th", "half", "quarter")):
        echo = _PLAYER_NPLUS_ECHO.match(selection_l)
        if echo:
            return {**meta, "player": echo.group("player").title(),
                    "stat": _canon_stat(market_stat) + qual,
                    "stat_line": float(echo.group("n")) - 0.5,
                    "line_type": "over", "prop_tagged": True}
        ou = _PLAYER_OU_ECHO.match(selection_l)
        if ou:
            return {**meta, "player": ou.group("player").title(),
                    "stat": _canon_stat(market_stat) + qual,
                    "stat_line": float(ou.group("line")),
                    "line_type": ou.group("side"), "prop_tagged": True}

    market_stat_nplus = _STAT_NPLUS_MARKET.match(market_l)
    if market_stat_nplus and selection_l and not any(ch.isdigit() for ch in selection_l):
        return {**meta, "player": selection.strip().title(),
                "stat": _canon_stat(market_stat_nplus.group("stat")) + qual,
                "stat_line": float(market_stat_nplus.group("n")) - 0.5,
                "line_type": "over", "prop_tagged": True}

    market_match = _MARKET_PROP.match(market_l)
    side_match = _OU_SELECTION.match(selection_l)
    if market_match and side_match:
        return {**meta, "player": market_match.group("player").title(),
                "stat": _canon_stat(market_match.group("stat")) + qual,
                "stat_line": float(side_match.group("line")),
                "line_type": side_match.group("side"), "prop_tagged": True}
    if (market_match and market_match.group("mline")
            and selection_l in ("over", "under")):
        # Dabble style: the line lives in the MARKET ("… player performance
        # 43.5") and the selection is a bare side
        return {**meta, "player": market_match.group("player").title(),
                "stat": _canon_stat(market_match.group("stat")) + qual,
                "stat_line": float(market_match.group("mline")),
                "line_type": selection_l, "prop_tagged": True}
    return meta
