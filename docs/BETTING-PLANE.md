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

## Everything is configurable

**No betting rule is locked.** Two settings default to the cautious side and warn loudly
when moved, because each is grounded in a measurement rather than a preference — but both
are the owner's to change:

1. **`allow_unverified_auto` (default `False`).** Sportsbet and TAB were driven end to end
   against real accounts. Unibet and Entain were not: their contracts were captured from
   real placements made *in a browser*, which proves the request shape and nothing about
   whether a stored credential alone is accepted — Entain additionally sits behind Kasada
   with a config-derived token endpoint. With the flag off, `auto` on those books is
   **downgraded to `ask`**, not refused, so a first live placement is watched; watching one
   go through is how a book earns its way into `VERIFIED_BOOKS`. Turning the flag on places
   unattended and logs a warning saying so.

2. **`min_ev` (default `0.03`).** Zero or negative is permitted and warns: at that floor
   every candidate clears, so the plane donates the vig on every bet it finds, at machine
   speed.

The only things that still raise are **arithmetic nonsense** (a negative cap, a
`kelly_fraction` outside `(0, 1]`) and **a book with no placement tool** — a capability
limit, not a policy one. `load_policy` runs the same construction, so a hand-edited file
fails on load rather than at placement time.

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

## Scoring a price — where it is easiest to fool yourself

An SGM price **cannot be de-vigged the ordinary way**: de-vigging needs a complete market
summing to one, and a single combination has no complementary set. So `quant.devig` and
`quant.value` do not apply. What is available instead is several books pricing the
identical bet with their own correlation models — models that disagree by far more than
the vig (-41% to +5% on one anchor leg, measured live), so the dispersion is signal.

`scanner.py` therefore scores each book **against the others**:

- **The consensus excludes the book being scored.** Otherwise an outlier drags the number
  it is measured against toward itself and partly hides.
- **Averaged in probability space, not odds space.** Odds are a reciprocal scale: the
  midpoint of 2.0 and 10.0 is 3.33 (30%), not 6.0 (16.7%). Getting this backwards flatters
  every longshot on the board.
- **Median, not mean** — one mismatched leg or capped payout moves a mean a long way.
- **Kambi's 1001.0 never enters a consensus.** It is a payout ceiling, not a price, and
  letting it in would invent an edge for every other book.

### Two bases, and a trap between them

`relative` (default) is `best_odds / consensus_odds - 1` — how much more this book pays
for the identical bet. It assumes only that books carry similar margin, so the margin
largely cancels. **It is not expected value.**

`ev` is `fair_probability * odds - 1`, needing `assumed_overround` to turn vig-inclusive
quotes into a fair probability.

> **At `assumed_overround = 0` the two are the same number** — algebraically, not
> approximately: `(1/c)·o − 1` *is* `o/c − 1`. Asking for `ev` without supplying a margin
> gets the relative figure wearing an EV label, which is the exact mislabelling
> `edge_basis` exists to prevent. That case logs a warning.

The basis is stored on the policy and written to **every ledger row**, because a ledger
mixing bases holds numbers that cannot be compared with each other.

## Per-book payloads

Four books, four unrelated shapes. Resolution happens once — in the quoter, which now
surfaces a `placement` block — so the adapters never re-resolve and cannot disagree with
the price that was quoted.

| Book | Shape of an SGM |
|---|---|
| Sportsbet | **ONE leg with several `parts`**, `betType: "SGL"` — not a multi-leg bet. External id space (103/17131), not the internal one from `topicLink`. |
| Entain | **Several `legs` in one bet**, plus a `prices` object keyed by event id — the mirror image of Sportsbet's trap. |
| Unibet | **One `couponRow`** whose `group.groups[]` nests the legs; odds in **thousandths** (3400 = 3.40); stake in `bets[]`. |
| TAB | **Cannot be built from a comparison quote** — it needs `decoToken`s, which only the account-tier `tab_price_slip` issues. Stake and odds are strings. |

`allowOddsChange` is sent as `false`: the plane runs its own drift gate, and letting the
book move the price afterwards would make that gate pointless.

## Approvals

`approvals.py` mirrors `fantasy.approvals` so there is only one approval system to learn,
with one deliberate difference: **a bet proposal goes stale in minutes, not at a
deadline.** Default TTL is 10 minutes, and two rules follow:

1. **An expired proposal is never executed** — and expiry is applied on *read* as well as
   on approve, so a stale proposal is never displayed as actionable.
2. **Approval does not skip the drift gate.** The human agreed to a *number*. `place_approved`
   runs the identical `run_intent` path, re-prices, and abandons the bet if the price moved
   against it — the proposal carries `reprice_args` for exactly this reason. (Written
   without them first, which made the gate pass vacuously; the test now proves the money
   call is never made.)

Notifications print from **typed fields only** and never echo free text from a bookmaker
payload — that string goes to a human who acts on it.

## The CLI

    agents bet policy              # your rules, and whether anything can place
    agents bet set min_ev=0.05     # validated before saving
    agents bet pending             # proposals waiting on you
    agents bet approve <id>        # still re-priced before it goes on
    agents bet reject <id>
    agents bet ledger              # what happened, refusals included

## Status

Built and tested (**131 tests**): scanner, policy, adapters, approvals, drift, executor,
ledger, runner, CLI. State lives under `betting/` in the app data dir — policy, proposals
and the ledger — kept apart from rotating ops state because it is a money record.

**Not yet wired to a live MCP session.** `run_intent` and `scan_fixture` take an abstract
`ToolCaller`, which is what makes the whole money path testable without a bookmaker; the
remaining work is a `MCPManager` session scoped to `<provider>.write` plus a per-book
`reprice` reader. After that, one supervised minimum-stake placement each on Unibet and
Entain is what moves them into `VERIFIED_BOOKS`.
