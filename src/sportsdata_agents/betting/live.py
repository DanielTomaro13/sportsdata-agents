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
    """`unibet_sgm_price` → `selectedOdds.decimal`, in KAMBI THOUSANDTHS.

    3300 IS 3.30. Returning the raw number would be a price a thousand times too long,
    which is the loudest possible wrong answer — but only caught if someone divides.

    NOT `validate_coupon`. This reader used to parse `couponRows[].odds` out of the
    validation response, and validation does not echo a price: its reply is
    {status, validSession, rewardInfo}, with no couponRows anywhere. The reader found
    nothing, raised, and the executor refused every Unibet placement — a drift gate that
    was armed, wired, and structurally incapable of passing. Measured 2026-08-27.
    """
    if not isinstance(quote, dict):
        raise PriceUnreadable("unibet_sgm_price returned no object")
    odds = (quote.get("selectedOdds") or {}).get("decimal")
    if isinstance(odds, int | float) and odds > 1000:
        return float(odds) / 1000.0
    if quote.get("selectedOdds") is None and quote.get("combinableOutcomeIds") is not None:
        # A single leg returns the combinable list with NO price — a missing key here
        # means "not priced", not zero.
        raise PriceUnreadable("unibet_sgm_price returned no selectedOdds (single leg?)")
    raise PriceUnreadable(f"no usable selectedOdds in unibet_sgm_price response: {sorted(quote)[:6]}")


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
    groups: set[str] = set()
    for book in policy.KNOWN_BOOKS:
        if policy.mode_for(book) not in ("ask", "auto"):
            continue
        if book not in policy._eligible_books():
            continue
        groups.add(f"{book}.write")
        # ...and whatever the drift gate needs to re-price it. A session that can place
        # but cannot re-price is one that arms the gate and then starves it.
        groups.update(REPRICE_GROUPS.get(book, []))
    return sorted(groups)


#: The MCP groups holding each book's ANONYMOUS pricing tools.
#:
#: A LIST PER BOOK, NOT A STRING, and not derivable from the book name. Two things make
#: this a table rather than an f-string, both found on the first live run:
#:
#: 1. The names disagree on the obvious: `sportsbet.sports` and `tab.sports` are plural,
#:    `unibet.sport` and `betr.sport` are singular, and Entain's pricing sits in
#:    `entain.rest` with no "sport" in it at all. Guessing `f"{book}.sport"` was wrong for
#:    three of four books and surfaced as "Unknown tool: 'sportsbet_event_markets'" — a
#:    scan that silently priced nothing rather than erroring.
#: 2. ONE BOOK CAN NEED SEVERAL GROUPS. Sportsbet resolves markets from
#:    `sportsbet.sports` but its SGM pricer lives in `sportsbet.cross`, so a session with
#:    only the first resolves legs and then cannot price them.
#:
#: Read from the shipped specs on 2026-08-27. If a scan reports unknown tools, re-read
#: them rather than guessing which group a tool is in.
PRICING_GROUPS = {
    "sportsbet": ["sportsbet.sports", "sportsbet.cross"],
    "tab": ["tab.sports"],
    "unibet": ["unibet.sport"],
    "entain": ["entain.rest"],
    "betr": ["betr.sport"],
    "pointsbet": ["pointsbet.sports"],
}

#: The groups a book's RE-PRICE tool lives in — what the drift gate needs, which is not
#: the same as what placement needs.
#:
#: Sportsbet's `sportsbet_price_slip` and TAB's `tab_price_slip` are ACCOUNT-tier tools in
#: `<book>.account`, so a session holding only `<book>.write` can place a bet but cannot
#: check the price first — which would arm the gate and then starve it. Entain and Unibet
#: re-price through their anonymous pricers instead.
REPRICE_GROUPS = {
    "sportsbet": ["sportsbet.account"],
    "tab": ["tab.account"],
    "entain": ["entain.rest"],
    "unibet": ["unibet.sport"],
}


def read_groups(books: list[str] | None = None) -> list[str]:
    """Groups for the anonymous pricing side.

    Defaults to every book the COMPARATOR can quote, not just the placeable four — a
    book you cannot bet at still informs the consensus, and leaving it out would narrow
    the field the edge is measured against.
    """
    wanted = books or sorted(PRICING_GROUPS)
    missing = [b for b in wanted if b not in PRICING_GROUPS]
    if missing:
        raise PriceUnreadable(f"no pricing groups known for {missing} — see PRICING_GROUPS")
    return sorted({g for b in wanted for g in PRICING_GROUPS[b]})
