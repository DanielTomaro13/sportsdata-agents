# The betting plane (Phase A)

Placing bets from the agent platform, and the safety architecture that replaced the one
this feature had to remove.

## What changed, and why it needed a decision

The platform was built with a **structural no-money invariant**. `docs/history/PLAN.md`
states it plainly:

> The MCP tool catalogue exposed to agents is filtered to exclude any placement/deposit/
> withdrawal tool; agent specs cannot grant one; the runtime denies them even if
> requested. Advisory-only is enforced by capability, not just by prompt.

It was enforced in three places: a name-matching deny-filter in `mcp/manager.py` that
hid and refused matching tools, an authoring-time rejection in `agents/spec.py`, and a
construction-time check in `agents/harness.py`.

All three rested on a premise stated in the manager's own docstring — *"the MCP has no
placement/deposit/account tools at source"*. On **2026-08-27** that premise stopped being
true: the data plane gained `sportsbet_place_bet`, `tab_place_bet`, `entain_place_bet`
and `unibet_place_bet`, each captured from a real bet placed by the account holder.

The filter was **removed deliberately**, not worked around. Deleting a safety control
quietly is how a codebase ends up with a comment describing a guarantee it no longer
provides.

## What replaced it

A name filter answers "does this tool sound like money?". The replacement answers the
question that actually matters: **"should this specific bet be placed, at this size,
right now?"** That is `betting/policy.py`, and it is deterministic code operating on
typed numeric fields.

| | Old deny-filter | New policy gate |
|---|---|---|
| Decides on | the tool's **name** | edge, price, stake, budget, book, clock |
| Can be talked out of it | no | no — it reads no free text |
| Blocks reads too | yes (balance, price-slip, cash-out) | no |
| Knows about money limits | nothing | daily cap, per-bet cap, open exposure |
| Human in the loop | n/a | configurable per book |

### The cost, stated plainly

The filter could not be reasoned with; a policy can only be as good as its own
arithmetic. The scanner reads bookmaker pages and API responses, which are
**attacker-controlled content** — a book, or anything injected into its payload, can
contain text aimed at whatever is reading it. The risk the filter used to blunt is now
carried by two things: the policy (which touches no free text, so no injected string can
widen a limit) and **group scoping** (a session not started with `<provider>.write` has
no placement tool in it at all).

### What it bought

The old filter matched `balance`, `cashout`, `betslip` and `stake`, which are **reads**.
That cost real functionality: account balance is where Kelly sizing gets a bankroll, and
`*_price_slip` is the quote you are *supposed* to take immediately before placing. Those
are available again. `moves_money()` survives as a narrow **classifier** — it labels
money-movers so they are logged loudly at every layer, but it gates nothing.

## The four modes

Configured globally and overridable per book:

| Mode | What happens |
|---|---|
| `paper` | the full pipeline runs, every decision is recorded, **nothing reaches a bookmaker** |
| `ask` | the bet is built and sized, then handed to a human to approve |
| `auto` | placed unattended, inside every limit below |
| `never` | not even proposed at this book |

**The default is `paper` everywhere.** Autonomy is opted into book by book: the failure
mode of a plane that acts too freely is money that is gone, while the failure mode of one
that records too much is a longer ledger.

## The two rules that cannot be configured away

Both raise at construction, so they cannot be reached by accident — or by an agent
editing the config file, which `load_policy` re-validates for exactly that reason.

1. **A book whose placement path has never been round-tripped cannot be `auto`.**
   Sportsbet and TAB were driven end to end against real accounts. Unibet and Entain were
   not: their contracts were captured from real placements made *in a browser*, which
   proves the request shape and nothing about whether a stored credential alone is
   accepted — Entain additionally sits behind Kasada with a config-derived token endpoint.
   `ask` is allowed, and watching one go through is exactly how a book graduates.
   `VERIFIED_BOOKS` is a record of evidence, not a preference.

2. **`min_ev` cannot be zero or negative.** A plane willing to place at zero edge donates
   the vig on every bet it can find, forever, at machine speed.

## The money path

Everything goes through `execute.run_intent`, so six things happen in the same order
every time:

1. the **policy decides** before any request is built
2. anything not cleared for unattended placement is **recorded and stops**
3. the price is **fetched again** and the drift gate re-checks the edge
4. the bet is placed **once — never retried**
5. the book's answer is read as a **verdict, not an HTTP status**
6. the outcome is **written to the ledger** whatever it was

### Step 3 — the drift gate is one-sided

Only movement *against* the bettor kills a bet. If the price drifted out, the edge grew
and there is nothing to protect against; abandoning there would quietly discard the best
opportunities. The bet is always placed at the **freshly quoted** number, never the
remembered one — Sportsbet and Entain take a price the client asserts, so a stale number
is not merely optimistic, it is what gets sent.

### Step 4 — why there is no retry

None of Sportsbet, Entain or Unibet gives a usable idempotency key: their receipts are
*returned*, not sent, so a resent request is a **second bet**, not a retry. Only TAB
issues a key that makes a resend safe. A placement that times out is left alone and
escalated — "did that land?" is answered by reading the account, never by asking again
with money.

### Step 5 — a 200 is not a placement

Entain answers 200 and puts the verdict in `status`. Kambi (Unibet) does the same, and
its *success* body was never observed, so only its rejection envelope is trusted and
anything else is reported as placed-but-unconfirmed. Sportsbet answers **202 Accepted**,
meaning taken for processing — the `betId` is the receipt, and confirmation is a separate
read of `sportsbet_bet_history`.

## The ledger

Append-only JSONL, and the single source of truth for spend. `staked_today` and
`open_exposure` are **derived from it** rather than kept in a counter alongside: a
counter and a log can disagree after a crash, and when they disagree about money the log
is right.

**Refusals are recorded, not discarded.** A ledger of placements alone answers "did I
win", which is the less useful question. One that also holds *"edge 1.8%, below the 3%
floor"* answers "is my floor in the right place".

**Only `placed` rows spend budget.** A `paper` row is a decision, not a stake — if paper
running consumed real budget, a week of it would lock out the first real bet.

## Status

Built and tested (72 tests): policy, drift, executor, ledger. **Not yet wired to a live
MCP session** — `run_intent` takes an abstract `ToolCaller`, which is what makes the
whole money path testable without a bookmaker. Still to come: the scanner that produces
`Intent`s from the existing quant plane, the per-book payload adapters, and the approval
transport for `ask` mode.
