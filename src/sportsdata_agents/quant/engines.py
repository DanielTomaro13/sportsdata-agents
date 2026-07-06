"""Pricing-engine seam: model fair prices as an optional plug-in.

The platform runs fully without an engine — everything here degrades to
"no engine configured" cleanly. When one IS available, agents gain model
fair prices for whole boards (with Monte Carlo error bars), which powers
the model-value watch and the derivative-market value scout.

Two backends behind one protocol:

- **local** — imports the ``sportsdata_engines`` package if it is installed
  in this environment (operator machines). The import is lazy and optional;
  this repo neither depends on nor vendors it.
- **remote** — a thin client for a hosted pricing API (key-authenticated).
  The client shape ships now; the service itself is a later milestone, so
  until it is live this backend simply reports unavailable.

Select with ``SPORTSDATA_AGENTS_ENGINE_BACKEND`` = ``none`` (default) |
``local`` | ``remote`` (+ ``ENGINE_API_URL`` / ``ENGINE_API_KEY``).

Quote payloads are per-sport, mirroring what any book quotes:

- racing: ``{"win_odds": {runner: decimal_odds, ...}}``
- score-process codes (afl / rugby_league / rugby_union / basketball / nfl /
  baseball / soccer / ice_hockey):
  ``{"h2h": [home_odds, away_odds], "total": [line, over_odds, under_odds]}``
- tennis: ``{"h2h": [one_odds, two_odds], "total_games": [line, over, under]}``

Prices come back with ``std_error`` where the engine simulated (None where
closed-form); consumers MUST treat differences inside the error band as
noise, never as edge.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import get_settings

__all__ = [
    "FOOTY_SPORTS",
    "EnginePrice",
    "EngineUnavailable",
    "LocalEngineBackend",
    "PricingEngine",
    "RemoteEngineBackend",
    "resolve_engine",
]

FOOTY_SPORTS = (
    "afl", "rugby_league", "rugby_union",
    "basketball", "nfl", "baseball", "soccer", "ice_hockey",
)


class EngineUnavailable(RuntimeError):
    """No engine can serve this request (not installed / not configured)."""


@dataclass(frozen=True)
class EnginePrice:
    """One model fair price for a selection."""

    market: str
    selection: str
    fair_probability: float
    line: float | None = None
    std_error: float | None = None

    @property
    def fair_odds(self) -> float:
        return float("inf") if self.fair_probability <= 0.0 else 1.0 / self.fair_probability


class PricingEngine(Protocol):
    """What the platform needs from any pricing engine."""

    def sports(self) -> list[str]:
        """Sports this engine can price right now."""
        ...

    def price_board(self, sport: str, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        """Model fair prices for a fixture's whole board, seeded from quotes."""
        ...

    def sgm_quote(self, sport: str, fixture_id: str, quotes: dict[str, Any],
                  legs: list[dict[str, Any]]) -> dict[str, Any]:
        """Joint same-game-multi quote with the correlation lift explicit.

        Returns the wire shape: fair_probability, fair_odds,
        independent_probability, correlation_lift, std_error, joint_legs,
        independent_legs, warnings."""
        ...

    def stake_plan(self, picks: list[dict[str, Any]], bankroll: float,
                   **caps: float) -> list[dict[str, Any]]:
        """Error-aware Kelly stakes for (label, p_win, odds, std_error) picks."""
        ...


class LocalEngineBackend:
    """Prices with a locally installed engines package (optional import).

    Sport dispatch lives INSIDE the engines package
    (``sportsdata_engines.service.pricing.price_board_any``), so new sports
    and quote formats arrive with an engines upgrade — this seam never goes
    stale again. Older engines installs without that module fall back to
    the legacy three-family mapping below.
    """

    def __init__(self) -> None:
        try:
            import sportsdata_engines  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise EngineUnavailable(
                "engine backend 'local' selected but the engines package is not installed"
            ) from exc
        self._dispatch: Callable[[str, str, dict[str, Any]], list[Any]] | None
        try:
            from sportsdata_engines.service.pricing import SPORTS, price_board_any

            self._sports: list[str] = list(SPORTS)
            self._dispatch = price_board_any
        except ImportError:  # pragma: no cover - legacy engines install
            self._sports = ["racing", *FOOTY_SPORTS, "tennis"]
            self._dispatch = None

    def sports(self) -> list[str]:
        return list(self._sports)

    def price_board(self, sport: str, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        if self._dispatch is not None:
            try:
                return [_from_market_price(p) for p in self._dispatch(sport, fixture_id, quotes)]
            except ValueError:
                raise
            except RuntimeError as exc:  # CalibrationError and kin
                raise EngineUnavailable(str(exc)) from exc
        if sport == "racing":
            return self._racing(fixture_id, quotes)
        if sport in FOOTY_SPORTS:
            return self._footy(sport, fixture_id, quotes)
        if sport == "tennis":
            return self._tennis(fixture_id, quotes)
        raise EngineUnavailable(f"local engine does not price sport {sport!r} yet")

    def sgm_quote(self, sport: str, fixture_id: str, quotes: dict[str, Any],
                  legs: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            from sportsdata_engines.core.types import SlipLeg
            from sportsdata_engines.service.pricing import sgm_quote_any
        except ImportError as exc:
            raise EngineUnavailable(
                "SGM quoting needs sportsdata-engines >= 1.9 installed"
            ) from exc
        try:
            slip = [SlipLeg(str(leg["market"]), str(leg["selection"]), line=leg.get("line"))
                    for leg in legs]
            quote = sgm_quote_any(sport, fixture_id, slip, quotes)
        except (KeyError, TypeError) as exc:
            raise ValueError(f"malformed SGM leg: {exc}") from exc
        except RuntimeError as exc:  # CalibrationError and kin
            raise EngineUnavailable(str(exc)) from exc
        return {
            "fair_probability": quote.fair_probability,
            "fair_odds": quote.fair_odds,
            "independent_probability": quote.independent_probability,
            "correlation_lift": quote.correlation_lift,
            "std_error": quote.std_error,
            "joint_legs": list(quote.joint_legs),
            "independent_legs": list(quote.independent_legs),
            "warnings": list(quote.warnings),
        }

    def stake_plan(self, picks: list[dict[str, Any]], bankroll: float,
                   **caps: float) -> list[dict[str, Any]]:
        try:
            from sportsdata_engines.core.staking import stake_plan as plan
        except ImportError as exc:
            raise EngineUnavailable(
                "staking needs the sportsdata-engines package installed"
            ) from exc
        rows = [(str(p.get("label", f"pick-{i}")), float(p["p_win"]), float(p["odds"]),
                 float(p.get("std_error", 0.0))) for i, p in enumerate(picks)]
        staked = plan(rows, bankroll, **caps)
        return [{"label": s.label, "stake": s.stake, "p_win": s.p_win,
                 "odds": s.odds, "expected_profit": s.expected_profit} for s in staked]

    def _tennis(self, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        from sportsdata_engines.core import FixtureInputs
        from sportsdata_engines.tennis import anchors_from_quotes, fit_levers, price_board

        try:
            one, two = quotes["h2h"]
            line, over, under = quotes["total_games"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "tennis quotes need h2h: [one_odds, two_odds] and total_games: [line, over, under]"
            ) from exc
        fixture = FixtureInputs(
            sport="tennis", fixture_id=fixture_id,
            anchors=anchors_from_quotes(float(one), float(two), float(line), float(over), float(under)),
        )
        levers, report = fit_levers(fixture)
        if not report.converged:
            raise EngineUnavailable(
                f"engine calibration did not converge for {fixture_id} (residuals {report.residuals})"
            )
        fixture.levers.update(levers)
        return [_from_market_price(p) for p in price_board(fixture)]

    def _racing(self, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        from sportsdata_engines.core import FixtureInputs
        from sportsdata_engines.racing import price_board, win_levers, win_probabilities_from_odds

        win_odds = quotes.get("win_odds")
        if not isinstance(win_odds, dict) or len(win_odds) < 2:
            raise ValueError("racing quotes need win_odds: {runner: decimal_odds} for the full field")
        probabilities = win_probabilities_from_odds({str(k): float(v) for k, v in win_odds.items()})
        inputs = FixtureInputs(
            sport="racing",
            fixture_id=fixture_id,
            levers=win_levers(probabilities),
            participants=list(probabilities),
        )
        return [_from_market_price(p) for p in price_board(inputs)]

    def _footy(self, sport: str, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        import importlib

        from sportsdata_engines.core import FixtureInputs

        module = importlib.import_module(f"sportsdata_engines.{sport}")
        try:
            h2h_home, h2h_away = quotes["h2h"]
            total_line, over, under = quotes["total"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "footy quotes need h2h: [home_odds, away_odds] and total: [line, over_odds, under_odds]"
            ) from exc
        anchors = module.anchors_from_quotes(
            float(h2h_home), float(h2h_away), float(total_line), float(over), float(under)
        )
        fixture = FixtureInputs(sport=sport, fixture_id=fixture_id, anchors=anchors)
        levers, report = module.fit_levers(fixture)
        if not report.converged:
            raise EngineUnavailable(
                f"engine calibration did not converge for {fixture_id} (residuals {report.residuals})"
            )
        fixture.levers.update(levers)
        return [_from_market_price(p) for p in module.price_board(fixture)]


class RemoteEngineBackend:
    """Client for the hosted pricing API (service ships in a later milestone)."""

    def __init__(self, base_url: str, api_key: str) -> None:
        # base_url is OPERATOR-configured (env), so this is trusted config, not
        # user input: the bearer is sent only to the URL the operator set. Do
        # not wire ENGINE_API_URL to any request/tool-influenced value.
        if not base_url or not api_key:
            raise EngineUnavailable(
                "engine backend 'remote' needs ENGINE_API_URL and ENGINE_API_KEY configured"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def sports(self) -> list[str]:
        return self._get("/v1/sports")

    def price_board(self, sport: str, fixture_id: str, quotes: dict[str, Any]) -> list[EnginePrice]:
        payload = self._post("/v1/price-board", {"sport": sport, "fixture_id": fixture_id, "quotes": quotes})
        return [
            EnginePrice(
                market=row["market"],
                selection=row["selection"],
                fair_probability=row["fair_probability"],
                line=row.get("line"),
                std_error=row.get("std_error"),
            )
            for row in payload["prices"]
        ]

    def sgm_quote(self, sport: str, fixture_id: str, quotes: dict[str, Any],
                  legs: list[dict[str, Any]]) -> dict[str, Any]:
        payload = self._post("/v1/sgm", {"sport": sport, "fixture_id": fixture_id,
                                         "quotes": quotes, "legs": legs})
        return {k: payload.get(k) for k in (
            "fair_probability", "fair_odds", "independent_probability",
            "correlation_lift", "std_error", "joint_legs", "independent_legs", "warnings")}

    def stake_plan(self, picks: list[dict[str, Any]], bankroll: float,
                   **caps: float) -> list[dict[str, Any]]:
        payload = self._post("/v1/stake-plan", {"picks": picks, "bankroll": bankroll, **caps})
        return list(payload.get("stakes", []))

    def _get(self, path: str) -> Any:
        return self._request("GET", path, None)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        import httpx

        try:
            response = httpx.request(
                method,
                f"{self._base_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"remote engine API unreachable: {exc}") from exc
        try:
            return response.json()
        except ValueError as exc:  # gateway error pages etc. — not a crash path
            raise EngineUnavailable("remote engine API returned non-JSON") from exc


def _from_market_price(price: Any) -> EnginePrice:
    return EnginePrice(
        market=price.market,
        selection=price.selection,
        fair_probability=price.fair_probability,
        line=price.line,
        std_error=price.std_error,
    )


def resolve_engine() -> PricingEngine | None:
    """The configured engine, or None when the platform runs bare (default)."""
    settings = get_settings()
    backend = settings.engine_backend
    if backend == "none":
        return None
    if backend == "local":
        return LocalEngineBackend()
    if backend == "remote":
        key = settings.engine_api_key.get_secret_value() if settings.engine_api_key else ""
        return RemoteEngineBackend(settings.engine_api_url, key)
    raise ValueError(f"unknown engine backend {backend!r} (use none | local | remote)")
