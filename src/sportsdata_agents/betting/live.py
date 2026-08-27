"""Wiring the plane to a real data plane: scoped sessions and per-book price readers.

Everything above this module takes an abstract `ToolCaller`, which is what lets the whole
money path be tested without a bookmaker. This is where that abstraction meets an actual
`MCPManager`, and it is deliberately the smallest module in the package.

## Two sessions, not one

Reading prices and placing bets use SEPARATE sessions, scoped to different groups:

    read  → <book>.sport         the comparator's pricers, all anonymous
    write → <book>.write         placement only, and only for books being placed at

The MCP registers exactly the groups it is started with, so a session that was never
given `<book>.write` has no placement tool in it at all — not hidden, absent. That makes
"this scan cannot place anything" a property of the process rather than a promise, which
is worth the extra subprocess.

`scope_for` builds the write groups from the policy, so a book left at `paper` or `never`
never appears in a placeable session in the first place.

## Reading a re-quoted price

Each book answers its re-price call in its own shape and its own units, and the executor
refuses to place blind rather than guess. These readers are small on purpose: a reader
that "helpfully" falls back to another field is a reader that can return a price from a
different bet.
"""

from __future__ import annotations

import logging
from typing import Any

from .policy import BettingPolicy

log = logging.getLogger(__name__)


class PriceUnreadable(ValueError):
    """The book answered, but not with a price this reader recognises. Never guess —
    the executor treats this as "do not place"."""


def read_sportsbet_price(quote: Any) -> float:
    """`sportsbet_price_slip` → betBuilds[].betCombinations[].betEnhancedPrice.

    The fraction on `enhancedOdds` is what placement wants, but for the DRIFT check a
    decimal is what compares; the adapter carries the fraction separately.
    """
    builds = (quote or {}).get("betBuilds") or []
    if not builds:
        raise PriceUnreadable("sportsbet_price_slip returned no betBuilds")
    combos = builds[0].get("betCombinations") or []
    for combo in combos:
        price = combo.get("betEnhancedPrice")
        if isinstance(price, int | float) and price > 1.0:
            return float(price)
    odds = builds[0].get("enhancedOdds") or []
    for entry in odds:
        price = entry.get("priceDecimal")
        if isinstance(price, int | float) and price > 1.0:
            return float(price)
    raise PriceUnreadable(f"no usable price in sportsbet_price_slip response: {sorted(builds[0])}")


def read_tab_price(quote: Any) -> float:
    """`tab_price_slip` → bets[].legs[] odds, multiplied out.

    TAB prices per leg rather than giving a combined figure on the slip, so the multi
    price is the product. Odds are STRINGS on the wire.
    """
    bets = (quote or {}).get("bets") or []
    if not bets:
        raise PriceUnreadable("tab_price_slip returned no bets")
    legs = bets[0].get("legs") or []
    if not legs:
        raise PriceUnreadable("tab_price_slip returned a bet with no legs")
    total = 1.0
    for leg in legs:
        try:
            total *= float(leg["odds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PriceUnreadable(f"unreadable leg odds in tab_price_slip: {leg!r}") from exc
    if total <= 1.0:
        raise PriceUnreadable(f"tab legs multiplied to {total}, which is not a price")
    return total


def read_entain_price(quote: Any) -> float:
    """`entain_sgm_price` → the priced entry's odds.

    ENTAIN QUOTES FRACTIONS: decimal = numerator/denominator + 1. The `decimal` field is
    present and is the one to use, but the +1 relationship is why this reader exists
    rather than a generic one — forgetting it gives a price that is silently one short,
    which is small enough to look plausible and wrong on every single bet.
    """
    for entry in (quote or {}).values() if isinstance(quote, dict) else []:
        if not isinstance(entry, dict):
            continue
        odds = entry.get("odds") or {}
        decimal = odds.get("decimal")
        if isinstance(decimal, int | float) and decimal > 1.0:
            return float(decimal)
        num, den = odds.get("numerator"), odds.get("denominator")
        if isinstance(num, int | float) and isinstance(den, int | float) and den:
            return float(num) / float(den) + 1.0
    raise PriceUnreadable(f"no usable odds in entain_sgm_price response: {type(quote).__name__}")


def read_unibet_price(quote: Any) -> float:
    """`unibet_validate_coupon` → the coupon's odds, in KAMBI THOUSANDTHS.

    3400 IS 3.40. Returning the raw number would be a price a thousand times too long,
    which is the loudest possible wrong answer and therefore the one most likely to be
    caught — but only if someone divides.
    """
    if not isinstance(quote, dict):
        raise PriceUnreadable("unibet_validate_coupon returned no object")
    if "message" in quote and quote.get("status") not in (None, "", 0):
        raise PriceUnreadable(f"kambi refused the coupon: {quote.get('status')}")
    rows = quote.get("couponRows") or []
    for row in rows:
        odds = row.get("odds")
        if isinstance(odds, int | float) and odds > 1000:
            return float(odds) / 1000.0
    raise PriceUnreadable("no couponRows odds in unibet_validate_coupon response")


#: Which reader belongs to which book. The executor refuses to place without one rather
#: than sending a bet at a price nobody read.
READERS = {
    "sportsbet": read_sportsbet_price,
    "tab": read_tab_price,
    "entain": read_entain_price,
    "unibet": read_unibet_price,
}


def reader_for(book: str):
    reader = READERS.get(book)
    if reader is None:
        raise PriceUnreadable(f"no re-price reader for {book!r}")
    return reader


def scope_for(policy: BettingPolicy) -> list[str]:
    """The MCP groups a session needs to place under THIS policy.

    A book left at `paper` or `never` produces no write group, so it is absent from the
    session rather than merely declined by the policy — two independent reasons a bet
    cannot happen, which is the point.
    """
    return sorted(
        f"{book}.write"
        for book in policy.KNOWN_BOOKS
        if policy.mode_for(book) in ("ask", "auto")
        and (book in policy._eligible_books())
    )


def read_groups(books: list[str] | None = None) -> list[str]:
    """Groups for the anonymous pricing side."""
    from .policy import BettingPolicy as _P

    return sorted(f"{b}.sport" for b in (books or sorted(_P.KNOWN_BOOKS)))
