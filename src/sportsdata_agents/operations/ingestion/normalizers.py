"""Feed payload → ``PricePoint`` normalizers (M2.1+).

Each upstream feed has its own shape; a normalizer is a PURE function from one raw
payload to flat price points — deterministic, fixture-testable, no I/O.

Capture policy (decided 2026-06-11): **everything, always**. No normalizer filters
markets by name — every market a payload carries becomes points under the book's own
naming, so a new market/sport/competition needs ZERO code changes. The only mapping
layer is :func:`canonical_market`, which RENAMES the big cross-book families
(h2h/spread/total) onto shared keys and passes everything else through untouched —
normalization, never exclusion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PricePoint:
    """One (feed, book, event, market, selection) price observation."""

    provider: str
    book: str
    sport: str
    event_external_id: str
    market: str
    selection: str
    odds: float
    event_name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.provider, self.book, self.event_external_id, self.market, self.selection)


# ─── canonical market/sport families (normalization, NEVER exclusion) ────
# Books name the same market differently ("Match Result" / "Moneyline" / "Head To
# Head"); cross-book math needs one key per family. The mapping is DATA — the
# packaged market_dictionary.json seed merged with a local overrides file that the
# market_steward agent maintains — so extending it needs no code change, and
# anything unmapped flows through under the book's own name.

_SIDES = ("home", "away", "draw", "over", "under")
_alias_cache: dict[str, dict[str, str]] | None = None


def _norm_name(name: Any) -> str:
    return " ".join(str(name).strip().lower().replace("_", " ").split())


def _load_dictionary() -> dict[str, dict[str, str]]:
    """alias → family reverse maps for markets and sports (seed + local overrides)."""
    import json
    import os
    from importlib import resources

    seed = json.loads(
        resources.files("sportsdata_agents.operations.resolution")
        .joinpath("market_dictionary.json")
        .read_text(encoding="utf-8")
    )
    overrides_path = os.environ.get(
        "SPORTSDATA_AGENTS_DICTIONARY_OVERRIDES", "market_dictionary.local.json"
    )
    if os.path.isfile(overrides_path):
        try:
            with open(overrides_path, encoding="utf-8") as fh:
                local = json.load(fh)
            for section in ("markets", "sports"):
                for family, aliases in (local.get(section) or {}).items():
                    seed.setdefault(section, {}).setdefault(family, [])
                    seed[section][family] = list(seed[section][family]) + list(aliases)
        except (OSError, ValueError) as e:
            logger.warning("dictionary overrides unreadable (%s): %s", overrides_path, e)
    out: dict[str, dict[str, str]] = {}
    for section in ("markets", "sports"):
        reverse: dict[str, str] = {}
        for family, aliases in (seed.get(section) or {}).items():
            reverse[_norm_name(family)] = family
            for alias in aliases or []:
                reverse[_norm_name(alias)] = family
        out[section] = reverse
    return out


def _dictionary() -> dict[str, dict[str, str]]:
    global _alias_cache
    if _alias_cache is None:
        _alias_cache = _load_dictionary()
    return _alias_cache


def reload_dictionary() -> None:
    """Drop the cache (the steward calls this after editing the overrides file)."""
    global _alias_cache
    _alias_cache = None


def canonical_market(name: str) -> str:
    """The family key for recognised market names; the book's own name otherwise."""
    n = _norm_name(name)
    return _dictionary()["markets"].get(n, n)


def canonical_sport(name: str) -> str:
    """The family key for recognised sport labels (event-resolution grouping)."""
    n = _norm_name(name)
    return _dictionary()["sports"].get(n, n).replace(" ", "_")


def _line_suffix(line: Any) -> str:
    if line in (None, "", 0, 0.0):
        return ""
    if isinstance(line, int | float) and float(line) == int(line):
        return f" {int(line)}"
    return f" {line}"


def _odds_ok(value: Any) -> float | None:
    try:
        odds = float(value or 0)
    except (TypeError, ValueError):
        return None
    return odds if odds >= 1.01 else None


def _side_from_event_name(outcome_name: str, event_name: str) -> str | None:
    """home/away by matching the outcome against an "X v Y" / "X vs Y" event name
    (BetR uses " v ", Entain " vs ")."""
    sep = " vs " if " vs " in event_name else " v " if " v " in event_name else None
    if sep is None:
        return None
    home, away = (part.strip() for part in event_name.split(sep, 1))
    o = outcome_name.strip().lower()
    if o and (o == home.lower() or o in home.lower() or home.lower() in o):
        return "home"
    if o and (o == away.lower() or o in away.lower() or away.lower() in o):
        return "away"
    return None


def american_to_decimal(price: float) -> float:
    """+194 → 2.94; -257 → 1.389 (Pinnacle quotes American odds)."""
    if price >= 100:
        return 1.0 + price / 100.0
    if price <= -100:
        return 1.0 + 100.0 / abs(price)
    raise ValueError(f"not an American price: {price}")


class _Sink:
    """Dedup + collect (a book repeating a key in one capture keeps the first)."""

    def __init__(self) -> None:
        self.points: list[PricePoint] = []
        self._seen: set[tuple[str, str, str, str, str]] = set()

    def add(self, point: PricePoint) -> None:
        if point.key not in self._seen:
            self._seen.add(point.key)
            self.points.append(point)


# ─── NBA CDN (NOT REGISTERED since 2026-06-11: aggregator, not a book) ────


def normalize_nba_odds(payload: Any) -> list[PricePoint]:
    """The NBA CDN odds feed: {games:[{gameId, markets:[{name, books:[{name, outcomes}]}]}]}.

    NOT REGISTERED: this is an AGGREGATOR'S affiliate view of book prices — the
    warehouse captures books directly. Kept for fixture parity and any historical
    nba_cdn series already stored.
    """
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for game in payload.get("games", []) or []:
        game_id = str(game.get("gameId", ""))
        if not game_id:
            continue
        for market in game.get("markets", []) or []:
            market_name = str(market.get("name", "?"))
            for book in market.get("books", []) or []:
                for outcome in book.get("outcomes", []) or []:
                    odds = _odds_ok(outcome.get("odds"))
                    if odds is None:
                        continue
                    side = str(outcome.get("type", "?"))
                    selection = f"{side}{_line_suffix(outcome.get('spread'))}"
                    sink.add(PricePoint(
                        provider="nba_cdn", book=str(book.get("name", "?")), sport="nba",
                        event_external_id=game_id, market=market_name, selection=selection,
                        odds=odds,
                        meta={"trend": outcome.get("odds_trend"), "opening": outcome.get("opening_odds")},
                    ))
    return sink.points


# ─── Sportsbet (shapes captured live 2026-06-11) ──────────────────────────


def _sportsbet_market(
    sink: _Sink, *, sport: str, event_id: str, event_name: str, start: Any, market: dict[str, Any]
) -> None:
    side_by_result = {"H": "home", "A": "away", "D": "draw"}
    market_key = "h2h" if market.get("marketSort") == "HH" else canonical_market(market.get("name", "?"))
    for sel in market.get("selections", []) or []:
        odds = _odds_ok((sel.get("price") or {}).get("winPrice"))
        if odds is None:
            continue
        side = side_by_result.get(str(sel.get("resultType", "")))
        selection = (side or str(sel.get("name", "?")).lower()) + _line_suffix(sel.get("unformattedHandicap"))
        sink.add(PricePoint(
            provider="sportsbet", book="Sportsbet", sport=sport,
            event_external_id=event_id, event_name=event_name,
            market=market_key, selection=selection, odds=odds,
            meta={"start_time": start, "team": sel.get("name")},
        ))


def normalize_sportsbet_matches(payload: Any, *, sport: str) -> list[PricePoint]:
    """Hot tier — ``sportsbet_competition_matches``: a LIST of groups, each
    {groupName, events:[{id, displayName, bettingStatus, primaryMarket}]}. The list
    route inlines the primary market only; the books tier captures the other ~290."""
    sink = _Sink()
    for group in (payload if isinstance(payload, list) else []):
        if not isinstance(group, dict):
            continue
        for event in group.get("events", []) or []:
            if event.get("bettingStatus") != "PRICED":
                continue
            event_id = str(event.get("id", ""))
            market = event.get("primaryMarket") or {}
            if not event_id or not market:
                continue
            _sportsbet_market(
                sink, sport=sport, event_id=event_id,
                event_name=str(event.get("displayName") or event.get("name") or ""),
                start=event.get("startTime"), market=market,
            )
    return sink.points


def normalize_sportsbet_books(payload: Any) -> list[PricePoint]:
    """Books tier — every market of every fetched fixture (``sportsbet_event_markets``,
    ~2.5MB / ~293 markets per event; same market/selection shape as primaryMarket)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for entry in payload.get("events", []) or []:
        for market in entry.get("markets", []) or []:
            if isinstance(market, dict):
                _sportsbet_market(
                    sink, sport=str(entry.get("sport", "?")), event_id=str(entry.get("event_id", "")),
                    event_name=str(entry.get("event_name", "")), start=entry.get("start"), market=market,
                )
    return sink.points


# ─── TAB (shapes captured live 2026-06-11) ────────────────────────────────


def _tab_match(sink: _Sink, *, sport: str, match: dict[str, Any]) -> None:
    match_id = str(match.get("spectatorBettingId") or match.get("id") or "")
    if not match_id:
        return
    event_name = str(match.get("name") or "")
    for market in match.get("markets", []) or []:
        market_key = canonical_market(market.get("betOption", "?"))
        for prop in market.get("propositions", []) or []:
            if str(prop.get("bettingStatus", "")) not in ("Open", "Live"):
                continue
            odds = _odds_ok(prop.get("returnWin"))
            if odds is None:
                continue
            position = str(prop.get("position", "")).lower()
            side = position if position in _SIDES else None
            selection = side or str(prop.get("name", "?")).lower()
            sink.add(PricePoint(
                provider="tab", book="TAB", sport=sport,
                event_external_id=match_id, event_name=event_name,
                market=market_key, selection=selection, odds=odds,
                meta={"start_time": match.get("startTime"), "team": prop.get("name")},
            ))


def normalize_tab_competition(payload: Any, *, sport: str) -> list[PricePoint]:
    """Hot tier — ``tab_competition`` (numTopMarkets≥1): the inline top markets.
    The books tier (``tab_match``) carries the other ~230 per fixture."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for match in payload.get("matches", []) or []:
        if isinstance(match, dict):
            _tab_match(sink, sport=sport, match=match)
    return sink.points


def normalize_tab_books(payload: Any) -> list[PricePoint]:
    """Books tier — full match books (``tab_match``: ~0.8MB / ~238 markets each)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for entry in payload.get("matches", []) or []:
        match = entry.get("payload")
        if isinstance(match, dict):
            _tab_match(sink, sport=str(entry.get("sport", "?")), match=match)
    return sink.points


# ─── Unibet / Kambi (shapes captured live 2026-06-11) ─────────────────────

_KAMBI_BASE_LABELS = ("", "regular time", "including overtime", "full time")


def _kambi_offers(sink: _Sink, *, sport: str, event: dict[str, Any], offers: list[Any]) -> None:
    side_by_type = {"OT_ONE": "home", "OT_TWO": "away", "OT_CROSS": "draw"}
    event_id = str(event.get("id", ""))
    if not event_id:
        return
    for offer in offers or []:
        base = canonical_market((offer.get("betOfferType") or {}).get("name", "?"))
        label = str((offer.get("criterion") or {}).get("label", "")).lower()
        market_key = base if label in _KAMBI_BASE_LABELS else f"{base} - {label}"
        for outcome in offer.get("outcomes", []) or []:
            if outcome.get("status") not in (None, "OPEN"):
                continue
            raw = outcome.get("odds")
            if not isinstance(raw, int | float) or raw < 1010:
                continue
            line_raw = outcome.get("line")
            line = (line_raw / 1000.0) if isinstance(line_raw, int | float) else None
            side = side_by_type.get(str(outcome.get("type", "")))
            out_label = str(outcome.get("label", "")).lower()
            base_sel = side or out_label or str(outcome.get("participant", "?")).lower()
            sink.add(PricePoint(
                provider="unibet", book="Unibet", sport=sport,
                event_external_id=event_id, event_name=str(event.get("name") or ""),
                market=market_key, selection=f"{base_sel}{_line_suffix(line)}",
                odds=round(raw / 1000.0, 3),
                meta={"start_time": event.get("start"), "team": outcome.get("participant")},
            ))


def normalize_unibet_matches(payload: Any, *, sport: str, only_group: str | None = None) -> list[PricePoint]:
    """Hot tier — Kambi listView: matches + the main offers it inlines. The books
    tier (``event_betoffer``) carries the full ~512-offer book per fixture."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for item in payload.get("events", []) or []:
        event = item.get("event") or {}
        if only_group and str(event.get("group", "")) != only_group:
            continue
        _kambi_offers(sink, sport=sport, event=event, offers=item.get("betOffers") or [])
    return sink.points


def normalize_unibet_books(payload: Any) -> list[PricePoint]:
    """Books tier — full Kambi event books (``betoffer/event/{id}``, ~0.6MB each)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for entry in payload.get("events", []) or []:
        _kambi_offers(sink, sport=str(entry.get("sport", "?")), event=entry.get("event") or {},
                      offers=entry.get("betOffers") or [])
    return sink.points


# ─── BetR (shape captured live 2026-06-11) ────────────────────────────────


def normalize_betr_category(payload: Any, *, sport: str) -> list[PricePoint]:
    """BetR sports/master category: MasterEvents→Markets where a row is ONE outcome
    ({EventName "Market Name (X v Y)", OutcomeName, Price, MarketDesc}). Every row is
    captured; the market key is the EventName minus its "(X v Y)" suffix. This IS the
    full book the data plane exposes for BetR (no per-event route is specced)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for master_cat in payload.get("MasterCategories", []) or []:
        for category in master_cat.get("Categories", []) or []:
            for event in category.get("MasterEvents", []) or []:
                event_id = str(event.get("MasterEventId", ""))
                event_name = str(event.get("MasterEventName") or "")
                if not event_id:
                    continue
                for row in event.get("Markets", []) or []:
                    odds = _odds_ok(row.get("Price"))
                    if odds is None:
                        continue
                    raw_market = str(row.get("EventName", "?"))
                    if "(" in raw_market:
                        raw_market = raw_market.split("(", 1)[0].strip()
                    market_key = canonical_market(raw_market)
                    desc = str(row.get("MarketDesc", "")).lower()
                    if desc not in ("", "win"):
                        market_key = f"{market_key} {desc}"
                    side = _side_from_event_name(str(row.get("OutcomeName", "")), event_name)
                    selection = (side or str(row.get("OutcomeName", "?")).lower()) + _line_suffix(
                        row.get("Points")
                    )
                    sink.add(PricePoint(
                        provider="betr", book="BetR", sport=sport,
                        event_external_id=event_id, event_name=event_name,
                        market=market_key, selection=selection, odds=odds,
                        meta={"start_time": event.get("MinAdvertisedStartTime"),
                              "team": row.get("OutcomeName")},
                    ))
    return sink.points


# ─── Entain / Ladbrokes (shape captured live 2026-06-11) ──────────────────


def normalize_entain_events(
    payload: Any, *, sport: str, only_competition: str | None = None
) -> list[PricePoint]:
    """Entain event-request: parallel maps {events{}, markets{}, entrants{}, prices{}}
    joined by UUIDs; prices keyed "<entrant_id>:<product>:" with fractional odds.
    EVERY market on every open event is captured (futures included) — the bulk call
    is Entain's full book."""
    if not isinstance(payload, dict):
        return []
    events = payload.get("events") or {}
    markets = payload.get("markets") or {}
    entrants = payload.get("entrants") or {}
    prices = payload.get("prices") or {}
    odds_by_entrant: dict[str, float] = {}
    for key, price in prices.items():
        entrant_id = str(key).split(":", 1)[0]
        if entrant_id in odds_by_entrant:
            continue
        odds_obj = (price or {}).get("odds") or {}
        num, den = odds_obj.get("numerator"), odds_obj.get("denominator")
        if isinstance(num, int | float) and isinstance(den, int | float) and den:
            odds_by_entrant[entrant_id] = 1.0 + num / den
    # entrants grouped per market in ONE pass (markets x entrants must not be O(n*m))
    entrants_by_market: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for entrant_id, entrant in entrants.items():
        entrants_by_market.setdefault(str(entrant.get("market_id")), []).append(
            (str(entrant_id), entrant)
        )
    sink = _Sink()
    for market_id, market in markets.items():
        event = events.get(str(market.get("event_id"))) or {}
        if event.get("match_status") not in (None, "BettingOpen", "Open"):
            continue
        if only_competition and str((event.get("competition") or {}).get("name", "")) != only_competition:
            continue
        event_name = str(event.get("name") or "")
        market_key = canonical_market(market.get("name", "?"))
        for entrant_id, entrant in entrants_by_market.get(str(market_id), []):
            odds = odds_by_entrant.get(entrant_id)
            if odds is None or odds < 1.01:
                continue
            side = _side_from_event_name(str(entrant.get("name", "")), event_name)
            sink.add(PricePoint(
                provider="entain", book="Ladbrokes", sport=sport,
                event_external_id=str(market.get("event_id")), event_name=event_name,
                market=market_key, selection=side or str(entrant.get("name", "?")).lower(),
                odds=round(odds, 3),
                meta={"team": entrant.get("name")},
            ))
    return sink.points


# ─── Pinnacle (shape captured live 2026-06-11) ────────────────────────────


def normalize_pinnacle_league(payload: Any, *, sport: str) -> list[PricePoint]:
    """Pinnacle (via the matchup-markets fetchers): EVERY straight market — periods
    and alternate lines included, suffixed (" p1", " alt") so nothing collides.
    moneyline→h2h; American odds → decimal. The sharpest book: its close is the CLV
    benchmark."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    markets_by_id = payload.get("markets") or {}
    for matchup in payload.get("matchups", []) or []:
        matchup_id = str(matchup.get("id", ""))
        if not matchup_id:
            continue
        participants = matchup.get("participants", []) or []
        names = {str(p.get("alignment", "")): str(p.get("name", "")) for p in participants}
        by_pid = {str(p.get("id", "")): str(p.get("name", "")) for p in participants}
        if names.get("home") and names.get("away"):
            event_name = f"{names['home']} v {names['away']}"
        else:  # outright/special matchups carry no sides — name them honestly
            special = matchup.get("special") or {}
            event_name = str(
                special.get("description")
                or (matchup.get("league") or {}).get("name")
                or "outright"
            )
        matchup_sport = str(matchup.get("_sport") or sport)
        for market in markets_by_id.get(matchup_id, []) or []:
            if market.get("status") not in (None, "open"):
                continue
            base = canonical_market(str(market.get("type", "?")))
            period = market.get("period") or 0
            market_key = base + (f" p{period}" if period else "") + (
                " alt" if market.get("isAlternate") else ""
            )
            for price in market.get("prices", []) or []:
                try:
                    odds = american_to_decimal(float(price.get("price")))
                except (TypeError, ValueError):
                    continue
                designation = str(price.get("designation", "")).lower()
                participant = by_pid.get(str(price.get("participantId", "")), "")
                base_sel = designation if designation in _SIDES else (
                    participant.lower() or designation or "?"
                )
                sink.add(PricePoint(
                    provider="pinnacle", book="Pinnacle", sport=matchup_sport,
                    event_external_id=matchup_id, event_name=event_name,
                    market=market_key, selection=f"{base_sel}{_line_suffix(price.get('points'))}",
                    odds=round(odds, 3),
                    meta={"start_time": matchup.get("startTime"),
                          "team": names.get(designation) or participant or None},
                ))
    return sink.points


# ─── PointsBet (shape captured live 2026-06-11) ───────────────────────────

_POINTSBET_MARKET_FIELDS = (
    "fixedOddsMarkets", "featuredMarkets", "insightMarkets", "featuredInPlayFixedOddsMarkets"
)


def normalize_pointsbet_events(payload: Any, *, sport: str) -> list[PricePoint]:
    """PointsBet events (listing or ~5MB details — same market row shape): EVERY
    market in every carried field. US sports name their h2h "Moneyline", AU sports
    "Match Result" — canonical_market folds both."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for event in payload.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("key", ""))
        if not event_id:
            continue
        home, away = str(event.get("homeTeam") or ""), str(event.get("awayTeam") or "")
        event_sport = "_".join(
            str(event.get("sportName") or event.get("className") or sport).strip().lower().split()
        )
        event_name = str(event.get("name") or f"{home} v {away}")
        for field_name in _POINTSBET_MARKET_FIELDS:
            for market in event.get(field_name, []) or []:
                market_key = canonical_market(
                    str(market.get("eventClass") or market.get("eventName") or "?")
                )
                for outcome in market.get("outcomes", []) or []:
                    odds = _odds_ok(outcome.get("price"))
                    if odds is None:
                        continue
                    team = str(outcome.get("name", "?"))
                    if home and team.lower() == home.lower():
                        side: str | None = "home"
                    elif away and team.lower() == away.lower():
                        side = "away"
                    else:
                        side = _side_from_event_name(team, f"{home} v {away}")
                    sink.add(PricePoint(
                        provider="pointsbet", book="PointsBet", sport=event_sport,
                        event_external_id=event_id, event_name=event_name,
                        market=market_key,
                        selection=(side or team.lower()) + _line_suffix(outcome.get("points")),
                        odds=odds,
                        meta={"start_time": event.get("startsAt") or event.get("advertisedStartTime"),
                              "team": team},
                    ))
    return sink.points


# ─── Betfair exchange (not registered — public key returns no prices from AU) ──


def normalize_betfair_by_event(payload: Any, *, sport: str) -> list[PricePoint]:
    """Betfair (via fetch_betfair_event_type): every marketNode's runners at best
    available BACK. Ready for an authenticated Exchange key (P4)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for event_type in payload.get("eventTypes", []) or []:
        for node in event_type.get("eventNodes", []) or []:
            event_id = str(node.get("eventId", ""))
            event_name = str((node.get("event") or {}).get("eventName") or "")
            for market in node.get("marketNodes", []) or []:
                if (market.get("state") or {}).get("status") not in (None, "OPEN"):
                    continue
                market_key = canonical_market(
                    str((market.get("description") or {}).get("marketType", "?"))
                )
                for i, runner in enumerate(market.get("runners") or []):
                    desc = runner.get("description") or {}
                    team = str(desc.get("runnerName") or runner.get("runnerName") or "")
                    backs = ((runner.get("exchange") or {}).get("availableToBack")) or []
                    if not backs:
                        continue
                    odds = _odds_ok(backs[0].get("price"))
                    if odds is None:
                        continue
                    side = _side_from_event_name(team, event_name) or (
                        "home" if i == 0 else "away" if i == 1 else None
                    )
                    lay = ((runner.get("exchange") or {}).get("availableToLay") or [{}])[0].get("price")
                    sink.add(PricePoint(
                        provider="betfair", book="Betfair", sport=sport,
                        event_external_id=event_id, event_name=event_name,
                        market=market_key, selection=side or team.lower(), odds=odds,
                        meta={"team": team, "lay": lay},
                    ))
    return sink.points


# ─── FanDuel US sportsbook (shape captured live 2026-06-11) ───────────────


def normalize_fanduel_pages(payload: Any, *, sport: str) -> list[PricePoint]:
    """FanDuel US event pages: EVERY market (the event page IS the full book);
    runners carry decimal odds directly (trueOdds.decimalOdds.decimalOdds) and
    result.type HOME|AWAY where applicable."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for page in payload.get("pages", []) or []:
        events = page.get("events") or {}
        page_sport = str(page.get("sport") or sport)
        for market in (page.get("markets") or {}).values():
            if market.get("marketStatus") not in (None, "OPEN"):
                continue
            event_id = str(market.get("eventId", ""))
            event = events.get(event_id) or events.get(f"EVENT:{event_id}") or {}
            market_key = canonical_market(
                str(market.get("marketType") or market.get("marketName") or "?")
            )
            for runner in market.get("runners", []) or []:
                if runner.get("runnerStatus") not in (None, "ACTIVE"):
                    continue
                odds = _odds_ok((((runner.get("winRunnerOdds") or {}).get("trueOdds") or {})
                                 .get("decimalOdds") or {}).get("decimalOdds"))
                if odds is None:
                    continue
                side = str((runner.get("result") or {}).get("type") or "").lower()
                base_sel = side if side in _SIDES else str(runner.get("runnerName", "?")).lower()
                sink.add(PricePoint(
                    provider="fanduel", book="FanDuel", sport=page_sport,
                    event_external_id=event_id, event_name=str(event.get("name") or ""),
                    market=market_key, selection=base_sel + _line_suffix(runner.get("handicap")),
                    odds=round(odds, 3),
                    meta={"start_time": market.get("marketTime"), "team": runner.get("runnerName")},
                ))
    return sink.points


# ─── FanDuel Racing / TVG (shape captured live 2026-06-11) ────────────────


def normalize_fanduel_races(payload: Any) -> list[PricePoint]:
    """FanDuel Racing race cards: bettingInterests' current win odds (fractional,
    null denominator = /1); tvgRaceId is globally unique; selections are saddle
    numbers (stable across odds changes)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for race in payload.get("races", []) or []:
        race_id = str(race.get("tvgRaceId", ""))
        if not race_id:
            continue
        track = (race.get("track") or {}).get("name") or race.get("trackName") or "?"
        sport = "greyhound_racing" if race.get("isGreyhound") else "horse_racing"
        for bi in race.get("bettingInterests", []) or []:
            runners = bi.get("runners") or []
            if runners and all(r.get("scratched") for r in runners):
                continue
            odds_obj = bi.get("currentOdds") or {}
            num, den = odds_obj.get("numerator"), odds_obj.get("denominator")
            if not isinstance(num, int | float):
                continue
            odds = 1.0 + float(num) / float(den if den else 1)
            if odds < 1.01:
                continue
            sink.add(PricePoint(
                provider="fanduel_racing", book="FanDuel", sport=sport,
                event_external_id=race_id, event_name=f"{track} R{race.get('raceNumber')}",
                market="win", selection=str(bi.get("biNumber")), odds=round(odds, 3),
                meta={"post_time": race.get("postTime"),
                      "runner": (runners[0].get("horseName") if runners else None)},
            ))
    return sink.points


# ─── discovery-feed wrappers: one combined payload → per-sport delegation ──


def _normalize_grouped(payload: Any, key: str, inner: Any, **kwargs: Any) -> list[PricePoint]:
    if not isinstance(payload, dict):
        return []
    points: list[PricePoint] = []
    for item in payload.get(key, []) or []:
        sport = str(item.get("sport") or "?")
        points.extend(inner(item.get("payload"), sport=sport, **kwargs))
    return points


def normalize_unibet_all(payload: Any) -> list[PricePoint]:
    """Every Kambi sport's listView (via fetch_unibet_all)."""
    return _normalize_grouped(payload, "sports", normalize_unibet_matches)


def normalize_entain_all(payload: Any) -> list[PricePoint]:
    """Every Entain sport category (via fetch_entain_all) — full books per call."""
    return _normalize_grouped(payload, "categories", normalize_entain_events)


def normalize_sportsbet_all(payload: Any) -> list[PricePoint]:
    """Every Sportsbet competition in the rotating window (via fetch_sportsbet_all)."""
    return _normalize_grouped(payload, "competitions", normalize_sportsbet_matches)


def normalize_tab_all(payload: Any) -> list[PricePoint]:
    """Every TAB competition in the rotating window (via fetch_tab_all)."""
    return _normalize_grouped(payload, "competitions", normalize_tab_competition)


def normalize_betr_all(payload: Any) -> list[PricePoint]:
    """Every BetR event type (via fetch_betr_all — prices ride the category call)."""
    return _normalize_grouped(payload, "types", normalize_betr_category)


# ─── AU-book racing (shapes captured live 2026-06-11) ─────────────────────
# Conventions: race = event, runner number = selection, win/place = markets,
# scratched runners skipped. Sports: horse_racing / greyhound_racing /
# harness_racing (each book's code mapped in fetchers._race_sport).


def normalize_tab_races(payload: Any) -> list[PricePoint]:
    """TAB racecards: runners[].fixedOdds {returnWin, returnPlace, bettingStatus}."""
    if not isinstance(payload, dict):
        return []
    type_sport = {"R": "horse_racing", "G": "greyhound_racing", "H": "harness_racing"}
    sink = _Sink()
    for entry in payload.get("races", []) or []:
        summary = entry.get("summary") or {}
        card = entry.get("card") or {}
        meeting = summary.get("meeting") or {}
        venue = str(meeting.get("venueMnemonic") or "?")
        # futures markets have NO race number (always 0) — the race NAME is the
        # only distinguisher ("Racing Futures R0" would collide every Cup market)
        race_no = summary.get("raceNumber")
        label = f"R{race_no}" if race_no else str(
            summary.get("raceName") or card.get("raceName") or "R?"
        )
        event_id = f"{meeting.get('meetingDate')}:{meeting.get('raceType')}:{venue}:{label}"
        event_name = f"{meeting.get('meetingName') or venue} {label}"
        sport = type_sport.get(str(meeting.get("raceType")), "horse_racing")
        for runner in card.get("runners", []) or []:
            fixed = runner.get("fixedOdds") or {}
            if str(fixed.get("bettingStatus", "")) not in ("Open", "Live", ""):
                continue
            number = runner.get("runnerNumber")
            # futures runners carry no saddle number yet — the horse name is the selection
            selection = str(number) if number else str(runner.get("runnerName", "?")).lower()
            for market_key, field_name in (("win", "returnWin"), ("place", "returnPlace")):
                odds = _odds_ok(fixed.get(field_name))
                if odds is None:
                    continue
                sink.add(PricePoint(
                    provider="tab_racing", book="TAB", sport=sport,
                    event_external_id=event_id, event_name=event_name,
                    market=market_key, selection=selection, odds=odds,
                    meta={"runner": runner.get("runnerName"),
                          "post_time": summary.get("raceStartTime")},
                ))
    return sink.points


def normalize_sportsbet_races(payload: Any) -> list[PricePoint]:
    """Sportsbet MultipleRacecards: full cards — every market parses through the
    same selection shape as the sports book (capture-everything applies)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    sports = payload.get("sports") or {}
    meetings = payload.get("meetings") or {}
    for event in payload.get("events", []) or []:
        event_id = str(event.get("id", ""))
        if not event_id:
            continue
        race_no = event.get("raceNumber")
        event_name = f"{meetings.get(event_id) or event.get('competitionName') or '?'} R{race_no}"
        sport = str(sports.get(event_id) or "horse_racing")
        for market in event.get("markets", []) or []:
            if not isinstance(market, dict):
                continue
            market_key = canonical_market(market.get("name", "?"))
            if market_key == "win or place":
                market_key = "win"  # Sportsbet's racing primary: win price with place option
            for sel in market.get("selections", []) or []:
                odds = _odds_ok((sel.get("price") or {}).get("winPrice"))
                if odds is None:
                    # racecards carry a prices[] array; fixed odds inline a winPrice
                    # (tote-only entries — MID/MDP — carry none, so off fixed-odds
                    # hours a card honestly yields nothing)
                    for row in sel.get("prices", []) or []:
                        odds = _odds_ok(row.get("winPrice"))
                        if odds is not None:
                            break
                if odds is None:
                    continue
                number = sel.get("runnerNumber") or sel.get("number")
                selection = str(number) if number is not None else str(sel.get("name", "?")).lower()
                sink.add(PricePoint(
                    provider="sportsbet_racing", book="Sportsbet", sport=sport,
                    event_external_id=event_id, event_name=event_name,
                    market=market_key, selection=selection, odds=odds,
                    meta={"runner": sel.get("name"), "post_time": event.get("startTime")},
                ))
    return sink.points


def normalize_betr_races(payload: Any) -> list[PricePoint]:
    """BetR racecards: Outcomes[].FixedPrices[] rows keyed by MarketTypeCode
    (WIN/PLC + whatever else the card carries — capture everything)."""
    if not isinstance(payload, dict):
        return []
    code_market = {"WIN": "win", "PLC": "place"}
    sink = _Sink()
    for entry in payload.get("races", []) or []:
        card = entry.get("card") or {}
        event_id = str(card.get("EventId", ""))
        if not event_id:
            continue
        event_name = str(card.get("EventName") or "")
        sport = str(entry.get("sport") or "horse_racing")
        for outcome in card.get("Outcomes", []) or []:
            number = str(outcome.get("OutcomeId") or "?")
            for row in outcome.get("FixedPrices", []) or []:
                odds = _odds_ok(row.get("Price"))
                if odds is None:
                    continue
                code = str(row.get("MarketTypeCode", "?"))
                market_key = code_market.get(code, code.lower())
                sink.add(PricePoint(
                    provider="betr_racing", book="BetR", sport=sport,
                    event_external_id=event_id, event_name=event_name,
                    market=market_key, selection=number, odds=odds,
                    meta={"runner": outcome.get("OutcomeName"),
                          "post_time": card.get("AdvertisedStartTime")},
                ))
    return sink.points


def normalize_pointsbet_races(payload: Any) -> list[PricePoint]:
    """PointsBet racecards: runners[].fluctuations.current is the live fixed win
    price; scratched runners skipped."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for card in payload.get("races", []) or []:
        if not isinstance(card, dict):
            continue
        event_id = str(card.get("raceId", ""))
        if not event_id:
            continue
        event_name = f"{card.get('venue') or '?'} R{card.get('number')}"
        sport = str(card.get("racingType") or "racing").strip().lower()
        sport = {"thoroughbred": "horse_racing", "greyhound": "greyhound_racing",
                 "harness": "harness_racing"}.get(sport, f"{sport}_racing" if sport else "racing")
        for runner in card.get("runners", []) or []:
            if runner.get("isScratched"):
                continue
            odds = _odds_ok((runner.get("fluctuations") or {}).get("current"))
            if odds is None:
                continue
            sink.add(PricePoint(
                provider="pointsbet_racing", book="PointsBet", sport=sport,
                event_external_id=event_id, event_name=event_name,
                market="win", selection=str(runner.get("number") or "?"), odds=odds,
                meta={"runner": runner.get("runnerName"),
                      "post_time": card.get("advertisedStartTimeUtc")},
            ))
    return sink.points


def normalize_unibet_races(payload: Any) -> list[PricePoint]:
    """Unibet racing (rsa GraphQL): competitors[].prices[] keyed by betType
    (FixedWin/FixedPlace), each with flucs — productType "Current" is the live one."""
    if not isinstance(payload, dict):
        return []
    bet_market = {"FixedWin": "win", "FixedPlace": "place"}
    sink = _Sink()
    for entry in payload.get("races", []) or []:
        card = entry.get("card") or {}
        data = card.get("data") or {}
        event = ((data.get("viewer") or {}).get("event")) or data.get("event") or {}
        event_key = str(entry.get("eventKey") or event.get("eventKey") or "")
        if not event_key:
            continue
        sport = str(entry.get("sport") or "horse_racing")
        event_name = str(event.get("name") or event_key)
        for competitor in event.get("competitors", []) or []:
            if competitor.get("scratched") or competitor.get("isScratched"):
                continue
            number = competitor.get("number") or competitor.get("startNumber")
            selection = str(number) if number is not None else str(competitor.get("name", "?")).lower()
            for price_row in competitor.get("prices", []) or []:
                market_key = bet_market.get(str(price_row.get("betType", "")),
                                            str(price_row.get("betType", "?")).lower())
                current = next((f for f in price_row.get("flucs", []) or []
                                if f.get("productType") == "Current"), None)
                # ante-post (futures) cards carry NO flucs — the row's direct
                # price is the live one there
                odds = _odds_ok(current["price"] if current else price_row.get("price"))
                if odds is None:
                    continue
                sink.add(PricePoint(
                    provider="unibet_racing", book="Unibet", sport=sport,
                    event_external_id=event_key, event_name=event_name,
                    market=market_key, selection=selection, odds=odds,
                    meta={"runner": competitor.get("name"),
                          "post_time": event.get("eventDateTimeUtc")},
                ))
    return sink.points


# ─── prediction markets: Kalshi / Polymarket (probability venues) ──────────
# Exchange quotes are probabilities, not bookmaker odds — captured as decimal
# odds (1/price) so the warehouse, monitor and cross-book math read them like
# any book. Both venues group "one outcome = one binary contract" under an
# event; the event title is the market key and each contract is a selection.


def _prob_to_odds(value: Any) -> float | None:
    """Decimal odds from a 0-1 probability; None outside the tradeable band."""
    try:
        prob = float(value or 0)
    except (TypeError, ValueError):
        return None
    if not 0.0 < prob < 1.0:
        return None
    return round(1.0 / prob, 4)


def _kalshi_prob(market: dict[str, Any], side: str) -> Any:
    """A side's ask as a probability — dollars field first, cents fallback."""
    dollars = market.get(f"{side}_dollars")
    if dollars not in (None, ""):
        return dollars
    cents = market.get(side)
    try:
        return float(cents) / 100.0 if cents not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_kalshi_all(payload: Any) -> list[PricePoint]:
    """Kalshi events-with-nested-markets pages: each contract's YES/NO asks become
    two selections under the EVENT title (the market in bookmaker terms); the
    contract's yes_sub_title names the outcome. Sport rides the event category
    (Kalshi's own naming — "sports" stays book-local until the dictionary maps it)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for page in payload.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        for event in page.get("events", []) or []:
            event_ticker = str(event.get("event_ticker", ""))
            event_name = str(event.get("title") or "")
            if not event_ticker:
                continue
            sport = canonical_sport(str(event.get("category") or "prediction"))
            market_key = canonical_market(event_name or "?")
            for mkt in event.get("markets", []) or []:
                if mkt.get("status") not in (None, "open", "active"):
                    continue
                subject = str(mkt.get("yes_sub_title") or mkt.get("title") or mkt.get("ticker") or "?")
                meta = {
                    "ticker": mkt.get("ticker"),
                    "close_time": mkt.get("close_time"),
                    "volume_24h": mkt.get("volume_24h_fp") or mkt.get("volume_24h"),
                    "open_interest": mkt.get("open_interest_fp") or mkt.get("open_interest"),
                }
                yes = _odds_ok(_prob_to_odds(_kalshi_prob(mkt, "yes_ask")))
                if yes is not None:
                    sink.add(PricePoint(
                        provider="kalshi", book="Kalshi", sport=sport,
                        event_external_id=event_ticker, event_name=event_name,
                        market=market_key, selection=subject.lower(), odds=yes, meta=meta,
                    ))
                no = _odds_ok(_prob_to_odds(_kalshi_prob(mkt, "no_ask")))
                if no is not None:
                    sink.add(PricePoint(
                        provider="kalshi", book="Kalshi", sport=sport,
                        event_external_id=event_ticker, event_name=event_name,
                        market=market_key, selection=f"no {subject}".lower(), odds=no, meta=meta,
                    ))
    return sink.points


def _json_list(value: Any) -> list[Any]:
    """Gamma encodes list fields as JSON strings ('["Yes","No"]'); accept both."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        import json

        try:
            return json.loads(value)
        except ValueError:
            return []
    return []


def normalize_polymarket_all(payload: Any) -> list[PricePoint]:
    """Polymarket Gamma event pages: each nested market's outcome prices become
    selections under the EVENT title; grouped markets (one team per contract) name
    the selection from groupItemTitle, plain binaries from the outcome itself.
    Sport is the most specific tag label (the portal-level "Sports" tag hides the
    code, so any other label wins)."""
    if not isinstance(payload, dict):
        return []
    sink = _Sink()
    for page in payload.get("pages", []) or []:
        for event in page if isinstance(page, list) else []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("id") or event.get("slug") or "")
            event_name = str(event.get("title") or "")
            if not event_id:
                continue
            labels = [str(t.get("label", "")) for t in event.get("tags", []) or [] if t.get("label")]
            specific = next((lbl for lbl in labels if lbl.lower() not in ("sports", "all")), "")
            sport = canonical_sport(specific or (labels[0] if labels else "prediction"))
            market_key = canonical_market(event_name or "?")
            for mkt in event.get("markets", []) or []:
                if mkt.get("closed") is True or mkt.get("active") is False:
                    continue
                subject = str(mkt.get("groupItemTitle") or "").strip()
                outcomes = _json_list(mkt.get("outcomes"))
                prices = _json_list(mkt.get("outcomePrices"))
                meta = {
                    "market_id": mkt.get("id"),
                    "end_date": mkt.get("endDate"),
                    "volume_24h": mkt.get("volume24hr"),
                    "liquidity": mkt.get("liquidity"),
                }
                for outcome_name, price in zip(outcomes, prices, strict=False):
                    odds = _odds_ok(_prob_to_odds(price))
                    if odds is None:
                        continue
                    name = str(outcome_name)
                    # grouped: the contract's subject is the selection; plain
                    # binary: the outcomes ARE the selections
                    selection = (subject if name.lower() == "yes" else f"{name} {subject}") if subject else name
                    sink.add(PricePoint(
                        provider="polymarket", book="Polymarket", sport=sport,
                        event_external_id=event_id, event_name=event_name,
                        market=market_key, selection=selection.lower(), odds=odds, meta=meta,
                    ))
    return sink.points
