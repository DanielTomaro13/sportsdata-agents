"""
Direct async client for Betfair's public read-only exchange API.

Same unauthenticated endpoints the Betfair site uses (public app key only). We
hit these directly with httpx rather than through the sportsdata engine because
they need no auth and we poll them hard.
"""

from __future__ import annotations

from typing import Any, Iterable

import httpx

APP_KEY = "nzIFcwyWhrlwYMrh"
NAV_URL = "https://scan-inbf.betfair.com.au/www/sports/navigation/v2/graph/bynode"
PRICES_URL = "https://ero.betfair.com.au/www/sports/exchange/readonly/v1/bymarket"

HORSE_RACING = "EVENT_TYPE:7"
GREYHOUND_RACING = "EVENT_TYPE:4339"

PRICE_TYPES = (
    "MARKET_STATE,MARKET_DESCRIPTION,EVENT,"
    "RUNNER_DESCRIPTION,RUNNER_STATE,RUNNER_EXCHANGE_PRICES_BEST"
)

_COMMON = {"_ak": APP_KEY, "alt": "json", "locale": "en", "currencyCode": "AUD"}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.betfair.com.au/exchange/plus/",
}


class BetfairClient:
    def __init__(self, timeout: float = 15.0) -> None:
        # http2 needs the optional `h2` package; the public endpoints answer
        # fine over http/1.1, so stay off it to avoid a vendored dependency.
        self._client = httpx.AsyncClient(timeout=timeout, headers=_HEADERS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        r = await self._client.get(url, params={**_COMMON, **params})
        r.raise_for_status()
        return r.json()

    async def navigation(
        self,
        node_ids: str,
        attachments: str = "MENU,EVENT",
        max_out_distance: int = 2,
        max_results: int = 500,
    ) -> dict[str, Any]:
        return await self._get(
            NAV_URL,
            {
                "nodeIds": node_ids,
                "attachments": attachments,
                "maxOutDistance": max_out_distance,
                "maxInDistance": 0,
                "maxResults": max_results,
            },
        )

    async def market_prices(self, market_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(market_ids)
        if not ids:
            return []
        data = await self._get(
            PRICES_URL,
            {
                "marketIds": ",".join(ids),
                "types": PRICE_TYPES,
                "rollupModel": "STAKE",
                "rollupLimit": 25,
            },
        )
        return data.get("eventTypes", [])
