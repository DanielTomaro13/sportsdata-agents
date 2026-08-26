"""What this repo assumes about `sportsdata_engines`, written down.

TWO SEAMS, EACH MISSING WHAT THE OTHER HAS. The MCP seam is version-guarded — the
handshake reads the server's version and warns when it is older than the runtime
contracts assume. The engines seam is growth-native — sport dispatch lives inside the
engines package (`price_board_any`) and `SPORTS` is read at runtime, so a new sport
arrives with an upgrade and nothing here changes. Neither had both halves. This adds the
guard to the engines side.

WHY A GUARD IS NEEDED AT ALL. `sportsdata-engines` is a private, optional package that
versions independently of this repo, and the coupling is not a published API: this repo
imports 21 internal symbols across four modules — `racing.infer`, `ratings.footy`,
`replay`, `core.staking` and others — most of them bypassing the `price_board_any` seam
that exists precisely to avoid that. An engines refactor that moves any one of them
breaks a pricing call at RUNTIME, deep inside the work, with an ImportError naming a
module the user has never heard of. This repo's CI never imports engines, so nothing
catches it in between.

WHAT THIS FILE DOES, AND DOES NOT DO. It records the contract and checks the version at
the seam's entry point, so an incompatible install is caught at startup with a message
naming the mismatch. It deliberately does NOT rewrite the 21 call sites to route through
here: that code is correct today, engines is not installed in this repo's environment or
its CI, and a large refactor nothing can exercise trades a visible risk for an invisible
one. `EXPECTED_SYMBOLS` is the inventory instead — guarded by a test, so adding a 22nd
coupling is a deliberate act that updates this list rather than a quiet one.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: The minimum engines this repo's call sites assume. Mirrors MIN_MCP_VERSION on the
#: other seam, and warns rather than fails for the same reason: the two repos version
#: independently, a too-old engine should surface as a loud log line rather than a dead
#: platform, and pricing degrades to "unavailable" on its own.
MIN_ENGINES_VERSION = (1, 21, 0)

#: Every `sportsdata_engines` symbol this repo imports, by module path. This is a
#: PRIVATE-API inventory, not a public contract — engines owes it nothing, which is
#: exactly why it is worth writing down. Guarded by
#: `test_the_engines_import_surface_matches_the_declared_contract`.
EXPECTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    # The seam proper: sport dispatch lives inside engines, so new sports arrive with an
    # upgrade. Everything below this line is a deeper coupling than this one.
    # The package root, imported for a submodule by name (`from sportsdata_engines import
    # racing`). Found by the guard below on its first run — it was not in this inventory,
    # which is precisely the kind of coupling that goes unnoticed.
    "sportsdata_engines": ("racing",),
    "sportsdata_engines.service.pricing": ("SPORTS", "price_board_any", "sgm_quote_any", "stat_prices_any"),
    "sportsdata_engines.core": ("FixtureInputs",),
    "sportsdata_engines.core.types": ("SlipLeg", "FixtureInputs"),
    "sportsdata_engines.core.staking": ("stake_plan",),
    # Per-sport modules, reached directly by the legacy fallback and by ratings.
    "sportsdata_engines.racing": ("price_board", "win_levers", "win_probabilities_from_odds"),
    "sportsdata_engines.racing.infer": ("fit_margin_curve", "win_probabilities_from_marked"),
    "sportsdata_engines.tennis": ("anchors_from_quotes", "fit_levers", "price_board"),
    # Ratings + replay: separate subsystems, each with its own private surface.
    "sportsdata_engines.ratings.racing": ("PastRun", "form_win_probabilities"),
    "sportsdata_engines.ratings.footy": ("MatchResult", "fit_footy_ratings"),
    "sportsdata_engines.replay": ("ReplayFixture", "calibration_report"),
}

#: Modules allowed to import `sportsdata_engines` directly. Keeping the coupling in a
#: known set is what makes it auditable; a new file reaching into engines should be a
#: decision, not a discovery.
ALLOWED_IMPORTERS = (
    "quant/engines.py",
    "quant/engines_contract.py",
    "quant/ratings.py",
    "quant/racing_value.py",
    "quant/calibrate.py",
)


def _tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def installed_version() -> str | None:
    """The installed engines version, or None when the package is absent."""
    try:
        import sportsdata_engines
    except ImportError:
        return None
    version = getattr(sportsdata_engines, "__version__", None)
    if version:
        return str(version)
    try:
        from importlib.metadata import version as _v

        return _v("sportsdata-engines")
    except Exception:  # pragma: no cover - metadata absent in odd installs
        return None


def check_version() -> str | None:
    """Warn when the installed engines is older than this repo assumes.

    Called at the seam's entry point so the mismatch surfaces when the backend is
    selected, not several calls later inside a board price. Returns the version it saw
    (None when engines is absent, which is the normal case and not a problem — the
    platform runs without an engine and the seam reports unavailable).
    """
    version = installed_version()
    if version is None:
        return None
    if _tuple(version) < MIN_ENGINES_VERSION:
        want = ".".join(str(p) for p in MIN_ENGINES_VERSION)
        log.warning(
            "sportsdata-engines %s is older than the minimum %s this repo's call sites "
            "assume — pricing may fail on symbols that moved. Upgrade the engines "
            "package, or set SPORTSDATA_AGENTS_ENGINE_BACKEND=none to run without one.",
            version,
            want,
        )
    return version
