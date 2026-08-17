# Fantasy: letting an agent touch a real team

An agent that can set your lineup can also ruin your season. This document describes the
three things that stand between a model's opinion and a change on your actual team, and
why each of them exists.

The model decides *what* is a good move. None of the machinery below has an opinion about
that. It decides whether a move may happen unattended, and whether it actually happened —
two questions a language model should not be trusted to answer about its own actions.

## The path a decision takes

```
intent ──► policy ──┬─► SKIP  nothing happens
                    ├─► ASK   proposal saved + notification, and it stops there
                    └─► ACT   write once ──► read back ──► compare ──► report
```

Everything goes through `fantasy.execute.run_intent`. That is the only place a write is
issued, which is what makes the ordering guaranteed rather than customary.

## 1. Policy — what may happen without asking

`agents fantasy policy <entry> --set lineup=auto --set max_hit=4`

| setting | meaning | default |
|---|---|---|
| `lineup` | set the XI before each deadline | `ask` |
| `captain` | move the armband | `ask` |
| `transfers` | `auto` / `auto_if_free` / `ask` / `never` | `ask` |
| `chips` | wildcard, free hit, bench boost, triple captain | `ask` |
| `max_hit` | points spendable without asking | `0` |
| `quiet_hours` | never act unattended inside this window | `23:00–07:00` |
| `max_actions_per_gameweek` | ceiling on autonomous actions | `3` |
| `act_within_hours_of_deadline` | act only this close to the deadline | `6.0` |

**Everything defaults to `ask`.** The failure mode of an agent that acts too freely is a
season; the failure mode of one that asks too often is a notification. You opt into
autonomy action by action.

**`chips` can never be `auto`.** Not a default — a rule the constructor rejects. There are
four chips in a season, each unrecoverable once played, and nobody should be able to
configure their way into "the agent played my wildcard on a blank gameweek" by accident.

Two more gates apply to every automatic action. It will not act days early, because team
news is still moving; and it will not act inside quiet hours, because an unattended write
at 3am has nobody to catch it if it goes wrong.

## 2. Approvals — and the expiry that makes them safe

When policy says ASK, a proposal is written to `fantasy-proposals.json` and a notification
goes out (`FANTASY_ALERT_CHANNEL`, default `log`).

```bash
agents fantasy pending          # what is waiting on you
agents fantasy show <id>        # the diff and the exact payload
agents fantasy approve <id>
agents fantasy reject <id>
```

Every proposal carries an `expires_at` — normally the gameweek deadline — and **an expired
proposal can never be approved**. This is the property that matters most. "Transfer Salah
in" means nothing three hours after the gameweek locked, and acting on a stale approval is
worse than not acting at all. Expiry is applied when proposals are *listed* as well as when
they are approved, so one that lapsed while nobody was looking cannot be revived.

Approval does not execute anything. The next agent run does, and only while the proposal is
still inside its window.

## 3. Read-back — because a 200 is not proof

FPL's write endpoints are undocumented. They can accept a request, return 200, and do
something other than what you asked: a pick dropped for an illegal formation, a captain
that did not move, a transfer applied at a different price. You find out on Saturday.

So after every write the squad is re-read and compared against the intent. The comparison
checks only what a lineup write actually sets — slot, captaincy, multiplier — and ignores
what the provider owns, because a price difference is not evidence of a failed write.

Three cases get named explicitly rather than left to be inferred from a field diff:

- **no captain at all** — the single most expensive silent failure
- **an illegal XI** — checked only on a complete 15-man squad, so the verifier does not
  cry wolf on a partial pick list
- **a move nobody requested** — a player who arrived or left without being in the intent,
  which means the write did something other than what was approved

A failed verification pages at urgent priority. A successful one is silent: a notification
per confirmed lineup trains you to swipe them away, and then the one that matters gets
swiped with the rest.

## What is deliberately not retried

A write that raises is reported as failed and left alone. A transfer that timed out may
still have been applied, and sending it again is how you pay two points hits for one move.
The engine enforces the same rule one layer down — `sportsdata-mcp` retries POSTs on 429
only, never on 5xx.

## Credentials

`sportsdata-mcp connect fpl` collects the session cookie and `csrftoken` from the local
browser, verifies them against a live call, and stores them 0600. Nothing in this package
handles a password, and no credential is ever printed.
