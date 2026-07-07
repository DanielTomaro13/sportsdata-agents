"""The watch-parameter registry: every alert kind, every knob, one place.

``agents watches`` reads this to validate and document customization — a typo'd
param that would silently do nothing becomes an error naming the valid knobs,
and ``agents watches kinds`` renders the whole table as live documentation.
The defaults here mirror the ``sub.params.get(...)`` fallbacks in
``operations.monitoring`` (a drift test keeps them honest).
"""

from __future__ import annotations

from typing import Any

# param -> (default, help). None default = optional filter with no fallback.
Params = dict[str, tuple[Any, str]]

# Knobs every watch honours (implemented by the shared _fire/push path).
COMMON_PARAMS: Params = {
    "window_minutes": (60, "dedupe window — the same condition fires once per this many minutes"),
    "digest_hours": (0, "batch pushes into a digest every N hours instead of realtime (0 = realtime)"),
    "quiet_hours": ("", 'local hours to keep the phone silent, e.g. "23-08" — alerts are still recorded'),
    "tz": ("Australia/Melbourne", "IANA timezone for jump times and quiet hours (env SPORTSDATA_AGENTS_TZ overrides)"),
    "max_alerts_per_cycle": (5, "cap per monitoring pass — the firehose guard"),
}

# The row-filter params the change-point watches accept (line_move/steam/value).
_ROW_FILTERS: Params = {
    "sport": (None, "only this sport (warehouse label, e.g. tennis)"),
    "exclude_sports": (None, "sports to ignore, e.g. table_tennis,esports"),
    "exclude_markets": (None, "markets to ignore, e.g. place"),
    "markets": (None, "allowlist — ONLY these markets, prefix-matched so h2h covers "
                      '"h2h - match", e.g. win,h2h,total (unset = all markets)'),
    "market": (None, "only this market (e.g. h2h)"),
    "selection": (None, "only this selection"),
    "book": (None, "only this bookmaker"),
    "provider": (None, "only this feed provider"),
}

WATCH_PARAMS: dict[str, Params] = {
    "arb": {
        "threshold_pct": (1.0, "minimum gross margin — best-price board sums under 1 by at least this %"),
        "hours": (1.0, "how far back to look for board prices"),
        "min_matched": (1000.0, "exchange legs need at least this much money traded"),
        "max_age_minutes": (20.0, "ignore quotes older than this — stale legs fake arbs"),
        "bankroll": (100.0, "stake sizing base for the printed split"),
    },
    "line_move": {
        "threshold_pct": (5.0, "single price move of at least this % fires"),
        "pre_match_only": (True, "skip events that have already started — in-play "
                                 "prices move for game reasons, not market ones"),
        "engine_gate": (False, "suppress when the engine fair is above the price "
                               "and no sharp book quotes under it"),
        "sharp_books": (["Pinnacle", "Betfair"], "books whose lower quote overrides the engine gate"),
        "min_engine_edge_pct": (None, "only alert with DEMONSTRATED value: the price must "
                                      "beat the engine fair by this % (no engine price = no alert)"),
        "engine_sports_only": (False, "ignore sports the engine cannot price (esports, "
                                      "table tennis, boxing...)"),
        "drift_value_only": (False, "suppress DRIFT alerts unless another book or the "
                                    "engine prices it shorter than the drifted-to price"),
        "exchange_min_matched": (1000.0, "exchange rows need this much traded — a "
                                         "near-untraded market's moves are stray orders"),
        "exchange_needs_book_prices": (True, "racing: skip exchange moves while every book "
                                             "is still SP-only — nothing is takeable until "
                                             "fixed odds post (the value scan catches the open)"),
        "bankroll": (100.0, "stake sizing base — printed when the price beats the engine fair"),
        "exchange_corroborators": (["Betfair", "FanDuel"],
                                   "exchanges whose shorter quote also counts as demonstrated "
                                   "value for min_engine_edge_pct (FanDuel = tote pool)"),
        "racing_max_lead_minutes": (None, "racing rows: ignore moves more than this many "
                                          "minutes before the jump — books have not opened "
                                          "or been captured yet, so there is no board to compare"),
        **_ROW_FILTERS,
    },
    "steam": {
        "min_moves": (3, "consecutive same-direction moves on one selection"),
        "pre_match_only": (True, "skip events that have already started — in-play "
                                 "prices move for game reasons, not market ones"),
        "engine_gate": (False, "suppress when the engine fair is above the price "
                               "and no sharp book quotes under it"),
        "sharp_books": (["Pinnacle", "Betfair"], "books whose lower quote overrides the engine gate"),
        "min_engine_edge_pct": (None, "only alert with DEMONSTRATED value: the price must "
                                      "beat the engine fair by this % (no engine price = no alert)"),
        "engine_sports_only": (False, "ignore sports the engine cannot price (esports, "
                                      "table tennis, boxing...)"),
        "drift_value_only": (False, "suppress DRIFT alerts unless another book or the "
                                    "engine prices it shorter than the drifted-to price"),
        "exchange_min_matched": (1000.0, "exchange rows need this much traded — a "
                                         "near-untraded market's moves are stray orders"),
        "exchange_needs_book_prices": (True, "racing: skip exchange moves while every book "
                                             "is still SP-only — nothing is takeable until "
                                             "fixed odds post (the value scan catches the open)"),
        "bankroll": (100.0, "stake sizing base — printed when the price beats the engine fair"),
        "exchange_corroborators": (["Betfair", "FanDuel"],
                                   "exchanges whose shorter quote also counts as demonstrated "
                                   "value for min_engine_edge_pct (FanDuel = tote pool)"),
        "racing_max_lead_minutes": (None, "racing rows: ignore moves more than this many "
                                          "minutes before the jump — books have not opened "
                                          "or been captured yet, so there is no board to compare"),
        **_ROW_FILTERS,
    },
    "value": {
        "min_edge_pct": (3.0, "recorded model edge at the latest price crossing this %"),
        "model": (None, 'fair-price family prefix ("engine:" anchored, "engine-ratings" '
                        "stats-only) — unset mixes families, newest-recorded wins"),
        "max_prediction_age_hours": (6.0, "recorded fairs older than this are stale, not opinions"),
        "pre_match_only": (True, "skip events that have already started — recorded "
                                 "probabilities do not know the score"),
        "bankroll": (100.0, "kelly stake sizing base"),
        **_ROW_FILTERS,
    },
    "scratching": {
        "stale_minutes": (20.0, "a runner whose prices stopped while the card moved on"),
        "sport": ("racing", "sport label prefix to scan"),
    },
    "model_value": {
        "sport": (None, "engine sport (e.g. afl) — required"),
        "pre_match_only": (True, "skip events that have already started — a live game's "
                                 "laggy derivatives are game state, not consistency edge"),
        "price_sport": (None, "warehouse sport label when it differs from the engine's"),
        "book": (None, "only check this bookmaker's boards"),
        "min_edge_pct": (3.0, "engine-vs-book disagreement beyond the noise band"),
        "error_multiple": (3.0, "how many model-error widths beyond the anchor to trust"),
        "max_age_minutes": (30.0, "board quotes older than this are skipped"),
        "derivative_ttl_hours": (24.0, "re-price derivatives at most this often"),
        "places": (None, "top-N places for finishing-position markets"),
        "sharp_books": (["Pinnacle", "Betfair"], "books listed first on the cross-book board"),
        "bankroll": (100.0, "kelly stake sizing base"),
    },
    "exchange_value": {
        "exchange_book": ("Betfair", "the sharp fair source — an exchange (Betfair) or a "
                                     "sharp bookmaker (Pinnacle)"),
        "min_edge_pct": (3.0, "book pays above the de-vigged sharp fair by this %"),
        "hours": (1.0, "how far back to look"),
        "min_matched": (1000.0, "exchange market must have this much traded"),
        "require_matched": (True, "set false when the fair source is a BOOK (Pinnacle) — "
                                  "books have no matched-money concept"),
        "bankroll": (100.0, "kelly stake sizing base"),
    },
    "racing_value": {
        "min_edge_pct": (8.0, "book pays above fair by at least this %"),
        "max_edge_pct": (60.0, "refuse implausible edges above this % — data artifacts, not bets"),
        "hours": (0.75, "how far back to look for race prices"),
        "exchange_book": ("Betfair", "fair source when liquid; else pack consensus"),
        "max_fair_odds": (12.0, "no longshot calls — de-vig lies out past this"),
        "max_staleness_minutes": (10.0, "drop books whose quotes lag the freshest by more"),
        "min_matched": (1000.0, "exchange race must have this much traded to be the fair"),
        "max_lead_minutes": (60.0, "only races jumping within this many minutes — hours-out "
                                   "boards are thin and the near-jump scan re-fires anyway"),
        "exclude_books": (["FanDuel"], "books never flagged (still feed the consensus)"),
        "min_consensus_books": (3, "consensus mode needs this many OTHER books on the race "
                                   "(lower it to cover thin international cards; Betfair "
                                   "mode is unaffected)"),
        "engine_gate": (False, "suppress consensus-mode alerts the engine fair disagrees "
                               "with (exchange-mode alerts are already corroborated)"),
        "require_sharp_fair": (True, "only alert when Betfair (the exchange fair) or the "
                                     "engine prices the runner SHORTER than the flagged "
                                     "book — consensus-only edges stay silent"),
        "sharp_books": (["Pinnacle", "Betfair"], "books listed first on the cross-book board"),
        "bankroll": (100.0, "kelly stake sizing base"),
    },
    "bsp_value": {
        "exchange_book": ("Betfair", "where the back price lives"),
        "min_edge_pct": (10.0, "form fair must beat the commission-adjusted exchange "
                               "price by this %"),
        "max_edge_pct": (50.0, "refuse implausible form edges above this % — a fair "
                               "3x under the whole market is a model artifact"),
        "lead_minutes": (45.0, "how close to the jump before form value is checked"),
        "min_matched": (2000.0, "exchange market must have this much traded"),
        "commission_pct": (5.0, "exchange commission on winnings"),
        "bankroll": (100.0, "kelly stake sizing base"),
    },
    "back_lay": {
        "exchange_book": ("Betfair", "lay side venue"),
        "hours": (1.0, "how far back to look"),
        "min_margin_pct": (1.0, "back-book vs lay-exchange margin after commission"),
        "min_matched": (1000.0, "lay market must have this much traded"),
        "commission_pct": (5.0, "exchange commission on lay winnings"),
        "bankroll": (100.0, "stake sizing base"),
    },
    "prediction_value": {
        "min_edge_pct": (10.0, "cross-venue probability gap worth acting on"),
        "q_threshold": (0.7, "match-quality floor for pairing markets across venues"),
        "min_prob": (0.05, "ignore contracts below this probability"),
        "max_prob": (0.95, "ignore contracts above this probability"),
        "min_volume": (100.0, "venue volume floor (both sides)"),
        "max_staleness_minutes": (90.0, "quotes older than this are skipped"),
        "hours": (6.0, "how far back to look"),
        "bankroll": (100.0, "stake sizing base"),
    },
    "stat_value": {
        "book": (None, "one book's ladders only; unset = every prop-tagged book"),
        "min_edge_pct": (5.0, "quoted rung pays above the fitted fair by this %"),
        "hours": (2.0, "how far back to look for ladder quotes"),
        "min_rungs": (3, "distinct thresholds needed before a fit is trusted"),
        "max_rmse_log": (0.08, "reject fits where the ladder disagrees with itself"),
    },
}


def params_for(kind: str) -> Params:
    """Every knob the kind honours: its own + the common set."""
    return {**WATCH_PARAMS[kind], **COMMON_PARAMS}


def validate_params(kind: str, updates: dict[str, Any]) -> list[str]:
    """Unknown-knob errors (with the valid list) — a typo must never silently
    configure nothing."""
    if kind not in WATCH_PARAMS:
        return [f"unknown watch kind {kind!r} — kinds: {', '.join(sorted(WATCH_PARAMS))}"]
    valid = params_for(kind)
    errors = []
    for key in updates:
        if key not in valid:
            errors.append(
                f"{kind} has no param {key!r} — valid: {', '.join(sorted(valid))}")
    return errors


def parse_value(raw: str) -> Any:
    """Typed value from a CLI key=value string: bool words, numbers, comma
    lists, else the string itself."""
    text = raw.strip()
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", ""):
        return None
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text
