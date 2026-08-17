"""Managing a fantasy team without handing an agent the keys.

Four pieces, in the order a decision moves through them:

    policy      → may the agent do this at all, or must it ask?
    approvals   → the proposal an owner approves, and the expiry that keeps it honest
    execute     → the one path from an approved intent to a write
    verify      → read back afterwards, because a 200 is not proof

The separation matters. The model decides WHAT is a good move; none of these modules
have an opinion about that. They decide whether a move is allowed to happen unattended,
and whether it actually happened — two questions a language model should not be trusted
to answer about its own actions.
"""

from .approvals import Proposal, State, Store, new_proposal
from .policy import Decision, LeaguePolicy, Mode, Verdict, load_policies, save_policy
from .verify import VerifyResult, verify_lineup, verify_transfers

__all__ = [
    "Decision",
    "LeaguePolicy",
    "Mode",
    "Proposal",
    "State",
    "Store",
    "Verdict",
    "VerifyResult",
    "load_policies",
    "new_proposal",
    "save_policy",
    "verify_lineup",
    "verify_transfers",
]
