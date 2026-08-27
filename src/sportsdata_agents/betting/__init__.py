"""Placing bets without handing a language model the chequebook.

Four pieces, in the order a candidate bet moves through them:

    policy   → may this be placed unattended, must it ask, or is it paper only?
    drift    → is the price still the one the edge was computed on?
    execute  → the one path from an intent to a placement, and never a retry
    ledger   → what was decided and what happened, including everything declined

The separation is the point. A model may decide WHAT looks like a good bet; none of
these modules have an opinion about that. They decide whether a bet is allowed to
happen unattended, at what size, and whether it actually happened — three questions a
language model should not be trusted to answer about its own actions, particularly
when the prices it reasoned over were fetched from a page a bookmaker controls.
"""

from .drift import DriftResult
from .drift import check as check_drift
from .execute import Intent, Outcome, run_intent
from .ledger import Entry, Ledger
from .policy import BettingPolicy, Decision, Mode, Verdict, load_policy, save_policy

__all__ = [
    "BettingPolicy",
    "Decision",
    "DriftResult",
    "Entry",
    "Intent",
    "Ledger",
    "Mode",
    "Outcome",
    "Verdict",
    "check_drift",
    "load_policy",
    "run_intent",
    "save_policy",
]
