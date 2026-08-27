"""Placing bets without handing a language model the chequebook.

Seven pieces, in the order a candidate bet moves through them:

    scanner    → what is this price worth, measured against the other books?
    policy     → may it be placed unattended, must it ask, or is it paper only?
    adapters   → what does THIS book's placement request actually look like?
    approvals  → the proposal a human says yes to, and the expiry that keeps it honest
    drift      → is the price still the one the edge was computed on?
    execute    → the one path from an intent to a placement, and never a retry
    ledger     → what was decided and what happened, including everything declined

    runner     → the only module that knows the order

The separation is the point. A model may decide WHAT looks like a good bet; none of
these modules have an opinion about that. They decide whether a bet is allowed to happen
unattended, at what size, and whether it actually happened — three questions a language
model should not be trusted to answer about its own actions, particularly when the
prices it reasoned over were fetched from a page a bookmaker controls.
"""

from .adapters import AdapterError, payload_for
from .approvals import BetProposal, Store, new_proposal
from .approvals import State as ProposalState
from .drift import DriftResult
from .drift import check as check_drift
from .execute import Intent, Outcome, run_intent
from .ledger import Entry, Ledger
from .policy import BettingPolicy, Decision, Mode, Verdict, load_policy, save_policy
from .runner import ScanResult, place_approved, scan_fixture
from .scanner import Candidate, candidates_from_comparison, consensus_of, edge_of

__all__ = [
    "AdapterError",
    "BetProposal",
    "BettingPolicy",
    "Candidate",
    "Decision",
    "DriftResult",
    "Entry",
    "Intent",
    "Ledger",
    "Mode",
    "Outcome",
    "ProposalState",
    "ScanResult",
    "Store",
    "Verdict",
    "candidates_from_comparison",
    "check_drift",
    "consensus_of",
    "edge_of",
    "load_policy",
    "new_proposal",
    "payload_for",
    "place_approved",
    "run_intent",
    "save_policy",
    "scan_fixture",
]
