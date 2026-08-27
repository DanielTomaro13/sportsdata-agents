"""Turning a scored candidate into the body one bookmaker's placement tool wants.

Four books, four unrelated shapes. Each adapter is small and boring on purpose: the
resolution work — matching a leg to a book's own market and outcome ids — already
happened in the quoter, which surfaces it as `quote["placement"]`. Re-resolving here
would mean doing the expensive part twice and risking two different answers.

## The one that is genuinely different

TAB cannot be built from a quote. Its placement requires **decoTokens**, and only the
account-tier `tab_price_slip` issues them — the anonymous `tab_sgm_price` used for
comparison does not. A token is what binds a leg to a price TAB actually quoted, and a
stale one from an earlier enquiry is a different quote. So `tab_payload` takes the
re-price response rather than the comparison quote, and the executor's re-price step is
where TAB's real payload is born.

This is a feature, not an awkwardness: TAB is the only book of the four that makes the
quote a *thing you hold* rather than a number you assert. Sportsbet and Entain take a
price the client states, which is why their adapters carry a price at all.

## Money is a caller's argument, never a default

No adapter has a default stake. Every one takes it explicitly, because a stake that
defaults is a stake nobody chose.
"""

from __future__ import annotations

import uuid
from typing import Any

from .scanner import Candidate


class AdapterError(ValueError):
    """The candidate cannot be turned into a placement body — a missing resolution,
    usually, which means the quote came from a path that does not support placing."""


def _placement(candidate: Candidate) -> dict[str, Any]:
    block = candidate.quote.get("placement")
    if not isinstance(block, dict) or not block:
        raise AdapterError(
            f"{candidate.book} quote carries no `placement` block, so the book's own "
            f"identifiers were never resolved — re-quote through sgm_books before placing"
        )
    return block


def sportsbet_payload(candidate: Candidate, *, stake: float) -> dict[str, Any]:
    """A Sportsbet SGM is ONE leg with several `parts`, betType "SGL".

    Not a multi-leg bet — that is the trap. The combined price rides as priceNum/priceDen
    replicated onto the leg's parts, and the external-id space (103/17131, not the
    internal 50/4165 from topicLink) is the one the quoter already looked up.
    """
    p = _placement(candidate)
    parts = [
        {
            "partNo": i + 1,
            "outcome": "W",
            "priceType": "L",
            "partDesc": "ALLMARKETS",
            "priceNum": p["priceNum"],
            "priceDen": p["priceDen"],
            "marketExternalId": part["marketExternalId"],
            "outcomeExternalId": part["outcomeExternalId"],
        }
        for i, part in enumerate(p["parts"])
    ]
    return {
        "betItems": [{
            "betNo": 1,
            "betType": "SGL",
            "stakePerLine": round(stake, 2),
            "numLines": 1,
            "legs": [{
                "legNo": 1,
                "legSort": "IM",
                "legType": "W",
                "legDesc": "EXT_PP",
                "isPYPSingle": False,
                "classExternalId": p["classExternalId"],
                "competitionExternalId": p["competitionExternalId"],
                "eventExternalId": p["eventExternalId"],
                "parts": parts,
            }],
        }],
        "checkBalance": True,
        # Needed to read back betId — without it there is no receipt to confirm against.
        "fullDetails": True,
    }


def entain_payload(candidate: Candidate, *, stake: float) -> dict[str, Any]:
    """An Entain SGM is several `legs` in ONE bet, plus a `prices` object keyed by EVENT
    id. `market_id`/`entrant_id` are the same identifiers the pricer used."""
    p = _placement(candidate)
    event_id = p["event_id"]
    odds = p.get("odds") or {}
    legs = [
        {
            "selections": [{
                "position": i,
                "market_id": sel["market_id"],
                "event_id": event_id,
                "entrant_id": sel["entrant_id"],
            }],
        }
        for i, sel in enumerate(p["selections"])
    ]
    return {
        "stake": round(stake, 2),
        "bets": [{
            "legs": legs,
            # Keyed by event id — the shape entain_place_bet documents for an SGM.
            "prices": {str(event_id): {"valid": True, "odds": odds}},
        }],
    }


def unibet_payload(candidate: Candidate, *, stake: float,
                   allow_odds_change: bool = False) -> dict[str, Any]:
    """A Kambi coupon: ONE couponRow whose `group.groups[]` nests the legs.

    `odds` is in THOUSANDTHS (3400 = 3.40) — the raw form the pricer returned, carried
    through rather than re-derived from the rounded decimal.

    `allow_odds_change` defaults to False: the plane has already run its own drift gate,
    and letting the book move the price after that would make the gate pointless.
    """
    p = _placement(candidate)
    ids = p["outcome_ids"]
    if not ids:
        raise AdapterError("Unibet quote resolved no outcome ids")
    thousandths = p.get("odds_thousandths")
    if not isinstance(thousandths, int | float):
        raise AdapterError("Unibet quote carries no thousandths price to place at")

    flag = "true" if allow_odds_change else "false"
    return {
        "body": {
            "couponRows": [{
                "index": 0,
                "odds": int(thousandths),
                "group": {
                    "operation": "COMBINATION",
                    "groups": [{"operation": "COMBINATION", "outcomeIds": [oid]} for oid in ids],
                },
                "type": "COMBINATION",
            }],
            "bets": [{"couponRowIndexes": [0], "eachWay": False, "stake": round(stake, 2)}],
            "allowOddsChange": flag,
            "allowOddsChangeLive": flag,
            "allowOddsChangePreMatch": flag,
            "requestId": uuid.uuid4().hex,
            "channel": "Internet",
        }
    }


def tab_payload(
    slip: dict[str, Any],
    *,
    stake: float,
    account_number: str,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    """Built from a `tab_price_slip` RESPONSE, not from a comparison quote — see the
    module docstring.

    `transaction_id` is TAB's idempotency key and the only real one of the four books.
    Generate it ONCE per bet you intend to place and keep it: resending with the same id
    asks TAB whether that bet landed; a fresh id places a second bet. It is returned in
    the payload so the caller can store it before the request goes out.

    Stake and odds are STRINGS in TAB's wire format.
    """
    bets = slip.get("bets") or []
    if not bets:
        raise AdapterError("tab_price_slip returned no priced bets")
    priced = bets[0]
    legs = priced.get("legs") or []
    if not legs:
        raise AdapterError("tab_price_slip returned a bet with no legs")

    tokens = [leg.get("decoToken") for leg in legs]
    if not all(tokens):
        raise AdapterError(
            "a leg came back without a decoToken — TAB will not accept it, and a token "
            "from an older enquiry is a different quote"
        )
    return {
        "accountNumber": account_number,
        "transactionId": transaction_id or uuid.uuid4().hex,
        "decoTokens": tokens,
        "bets": [{
            "type": "FIXED_ODDS",
            "stake": f"{stake:.2f}",
            "legs": [{
                "decoToken": leg["decoToken"],
                "type": leg.get("type", "WIN"),
                "propositionId": leg["propositionId"],
                "odds": str(leg["odds"]),
            } for leg in legs],
            "enableToteGuarantee": False,
            "enableMultiplier": False,
        }],
    }


#: Books whose payload can be built straight from a comparison quote. TAB is absent on
#: purpose — it needs an account-tier re-price first.
FROM_QUOTE = {
    "sportsbet": sportsbet_payload,
    "entain": entain_payload,
    "unibet": unibet_payload,
}


def payload_for(candidate: Candidate, *, stake: float) -> dict[str, Any]:
    """Build the placement body for a candidate, or say plainly why it cannot be built."""
    build = FROM_QUOTE.get(candidate.book)
    if build is None:
        if candidate.book == "tab":
            raise AdapterError(
                "TAB payloads are built from a tab_price_slip response, not a comparison "
                "quote — only the account-tier slip issues the decoTokens placement needs"
            )
        raise AdapterError(f"no placement adapter for {candidate.book!r}")
    return build(candidate, stake=stake)


# ─── re-pricing the same bet ────────────────────────────────────────────
#
# The drift gate needs to ask the book "what is this bet worth NOW", which means
# rebuilding the same request against the book's pricing tool rather than its placement
# one. Same resolved identifiers, different envelope — so it belongs here beside the
# payload builders, not in the executor, which should stay ignorant of bookmaker shapes.


def sportsbet_reprice_args(candidate: Candidate, *, stake: float) -> dict[str, Any]:
    """`sportsbet_price_slip` takes the same betItems the placement does."""
    return {"betItems": sportsbet_payload(candidate, stake=stake)["betItems"]}


def entain_reprice_args(candidate: Candidate, *, stake: float) -> dict[str, Any]:
    """`entain_sgm_price` takes the same selections the quoter used."""
    p = _placement(candidate)
    eid = p["event_id"]
    return {"same_game_multies": {eid: {"event_id": eid, "selections": p["selections"]}}}


def unibet_reprice_args(candidate: Candidate, *, stake: float) -> dict[str, Any]:
    """`unibet_validate_coupon` takes the coupon itself — the anonymous go/no-go that
    answered 400 rather than 401 with no session cookie."""
    return unibet_payload(candidate, stake=stake)


REPRICE_ARGS = {
    "sportsbet": sportsbet_reprice_args,
    "entain": entain_reprice_args,
    "unibet": unibet_reprice_args,
}


def reprice_args_for(candidate: Candidate, *, stake: float) -> dict[str, Any]:
    """Args for the book's re-price tool, or {} when the book cannot be re-priced from a
    comparison quote.

    An empty dict means the executor skips the drift gate for this book, which is only
    correct where a re-price is genuinely impossible — TAB, whose account-tier slip is
    what issues its tokens in the first place. Returning {} for a book that COULD be
    re-priced would silently disarm the gate.
    """
    build = REPRICE_ARGS.get(candidate.book)
    return build(candidate, stake=stake) if build else {}
