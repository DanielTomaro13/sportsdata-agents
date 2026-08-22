# Fantasy: letting an agent touch a real team

An agent that can set your lineup can also ruin your season. This document describes the
four things that stand between a model's opinion and a change on your actual team, and
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

**Approving executes it.** Not "queues it for the next run" — that was the original
design and it was a dead end: nothing ever read the APPROVED state, so the owner said
yes, the agent proposed the same thing again next run, and the approval sat there until
it expired. An approval queue that never drains is worse than no queue, because it looks
like consent was honoured.

`agents fantasy approve` therefore carries the change out immediately and prints the
result, and the scheduler tick sweeps anything approved out-of-band (from a phone, from
another shell). Expiry is re-checked at the last moment: an approval that sat unexecuted
past its deadline is never honoured late.

The write is rebuilt from the proposal — the record of what was *agreed* — rather than
recomputed. If the world has moved on, the expiry catches it; the agent does not quietly
substitute different picks under an old approval.

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

## 4. Running the season

Two pieces turn the above from built into running. Both are deterministic — no LLM in
this path — and both live in the scheduler's `fantasy` job, every 30 minutes.

### The staleness alarm

The plan calls this the highest-value reliability work in the whole build, and the reason
is an asymmetry: a cookie that expired on Tuesday is a two-minute chore if you hear about
it on Tuesday, and a lost gameweek if you hear about it at 17:29 on Friday.

So the credential is verified **through the same call the agent uses** (`fpl_my_team` —
a check that exercises a different code path can pass while the write fails), and the
urgency scales with time to the deadline rather than being one flat alarm:

| time to deadline | priority | re-alerts at most |
|---|---|---|
| more than 72h | *silent* | — there is genuinely time |
| 24–72h | default | daily |
| 6–24h | high | every 6h |
| under 6h | **urgent** | hourly |

A state *change* always speaks, whatever the cooldown — working→expired is news. A
transient failure (`unknown`) **never** pages: alarming on a network blip makes the real
expiry indistinguishable from a bad afternoon at FPL, and an alarm you mute is an alarm
you have already lost.

```bash
agents fantasy check
```

### The run trigger

Wakes the agent **once per gameweek**, inside the window its own policy defines
(`act_within_hours_of_deadline`) — so widening the window moves the run earlier and there
is no second setting to keep in sync. Without this, a 30-minute job inside a 6-hour
window would be a dozen billed LLM runs per gameweek, all proposing the same thing.

`last_run_event` is stamped **on success only**, so a crashed run is retried on the next
tick rather than silently costing you the gameweek. A broken credential stops the run
before it starts — waking an agent that cannot authenticate spends money to produce a 403.

Teams are watched because they have a saved policy. Setting a policy is the opt-in; there
is no separate subscription to forget about.

---

## ESPN

The second platform, and the reason the plane is now genuinely platform-agnostic rather
than FPL with a coat of paint. Policy, proposals, expiry, read-back and the refusal to
retry are shared; `fantasy/adapters.py` holds everything that differs, which is four
questions per platform: which tool writes a lineup, which writes a roster move, how the
squad is read back, and what shape a "pick" is.

### Setting a team up

```bash
sportsdata-mcp connect espnfantasy
agents fantasy policy 4 --platform espn \
  --set context.leagueId=899098157 --set context.seasonId=2026 --set context.game=ffl \
  --set lineup=auto --set transfers=auto --set max_hit=15
```

`context` is not optional: an ESPN team is `(league, season, game, teamId)`, and a policy
without it is refused at construction rather than failing later against a real roster.
The policy key includes the league for the same reason — every ESPN league numbers its
teams from 1, so a bare `espn:4` would let one league's policy govern another's team.

### What is different about ESPN, and why the code cares

| | FPL | ESPN |
|---|---|---|
| Deadline | one hard lock per gameweek | none — the week rolls, players lock at their own kickoff |
| Captain | yes | **no** — asserting one would fail every correct write |
| Formation | one rule, 15/11 | per sport **and** per league; the league's settings are the authority |
| Cost of a move | points hit | FAAB budget on waivers |
| Roster size after a move | always constant | may legitimately change (an unpaired ADD) |

The deadline row is load-bearing. ESPN's horizon is a rolling `now + N`, so hours-left
never counts down — applying FPL's "don't act until you're close" rule there is a
constant, not a window, and it blocked the ESPN agent permanently. Platforms without a
hard deadline skip that rule; the once-per-scoring-period run trigger is the real bound.

### Waivers

`bidAmount` is checked against the team's actual remaining budget **before** the policy
sees it, and it is what `max_hit` governs. A model reasoning about a budget it has not
read will bid 40 out of 12. A waiver also does **not** change the roster immediately — it
is processed on the league's waiver run — so a read-back straight after a claim correctly
shows no change, and the agent is told to say which kind of move it made.

### Trades are not automated, and that is deliberate

`TRADE_PROPOSE` sends a message to another person. That is a different category of act
from moving your own bench player, and no policy setting turns it on.

### Verification status

The two write tools were transcribed from ESPN's own public JS bundle — the request
builder, the item shapes and the transaction envelope, quoted in `espnfantasy.yaml`. They
carry `shapes_verified: false` and say so in their tool descriptions, because no live 200
has been seen yet. The 27 read tools are unaffected: `shapes_verified` is now per
endpoint, so being honest about 2 tools does not slap a warning on 27 good ones.

---

## MyFantasyLeague

The third platform, and the first whose write contract is **documented by the vendor**.
FPL's and ESPN's came out of minified JavaScript; MFL publishes 79 request types with
named, described arguments. That does not make the writes verified — our transcription
still has to be proved against a live call — but it does mean the contract itself is
knowable rather than inferred.

### Setting a team up

```bash
sportsdata-mcp connect mfl
agents fantasy policy 1 --platform mfl \
  --set context.leagueId=12345 --set context.year=2026 \
  --set lineup=auto --set transfers=auto --set max_hit=20
```

`entry` is your franchise **number** (1 for franchise `0001`); the adapter zero-pads it,
because passing `1` to MFL matches nothing and reads as "you are not in this league".

### Three defaults that are dangerous, and what the code does about them

**A lineup write is a full replacement.** `STARTERS` is the entire starting lineup —
anyone omitted is benched, and MFL accepts that silently. `mfl_propose_lineup` therefore
refuses a lineup whose size does not match the league's own declared `starters` count,
read from `mfl_league` rather than assumed. MFL leagues are the most customisable in
fantasy football (superflex, two-QB, IDP, no kicker), so a remembered formation would be
wrong more often than right. Where a league declares a *range* (`"8-9"`), the count check
is skipped rather than guessed at.

**Waiver claims append by default.** Omit `REPLACE` and your claims are added to whatever
is already queued for that round — which is how a scheduled job that runs twice submits
the same claim twice and bids for it twice. The blind-bid tool always sends `REPLACE`
explicitly.

**`FRANCHISE_ID` means "act as another franchise"** and is commissioner-only. It is not in
any tool's schema, and the adapter strips it on the way out even if it appears — passing it
is the one way to rewrite a stranger's roster by accident.

### Lineup verification is partial, and says so

MFL has no lineup *export* — `lineup` is an import-only type — so after setting a lineup
there is nothing to read back that reports who starts. Both easy answers are wrong:
asserting equality fails every correct write, and skipping the check reports success
without looking.

So the check is narrowed to what the roster read can prove — that every named starter is
actually on the roster, which still catches a mistyped id or a player who isn't yours —
and the result states plainly that the starting slots were **not** verified. A partial
check described as partial is worth more than a total one that is a fiction.

### The engine grew XML for this

MFL's `/export` honours `JSON=1`; its `/import` answers XML regardless, and answers HTTP
200 whether the write succeeded or failed. Without a decoder an ordinary rejection surfaced
as "the body did not parse" — and a success looked identical to it. `response_format: xml`
decodes to the same shape MFL's own JSON mode produces, so one `error_signals` rule covers
both. Repeated tags always collapse to a list, so a one-row document and a many-row
document never differ in shape.

### A correction worth recording

MFL looked, from the outside, like the platform with an API key and no cookie chore. It is
not. The vendor states plainly that `APIKEY` "does not work for import requests", so writes
need a login-derived cookie exactly like FPL and ESPN. `APIKEY` is offered on the read
tools only and is absent from every write tool by design.

### Running unattended — and four bugs that made that a fiction

The MFL agent shipped able to propose and write, and unable to run on its own. A review
of the autonomous path found four faults, three of them silent:

**The week came from the wrong endpoint.** MFL's league export has no `currentWeek` —
verified against a real league — so the lookup fell through to `startWeek` and the agent
would have written a **week 1 lineup every week of the season**. The week now comes from
`nflSchedule`, which is documented to return the current one and is the only endpoint
that does.

**The credential check asked the wrong provider.** MFL fell through to the FPL branch, so
a team with a perfectly good MFL cookie was told "no FPL session cookie is configured" —
and a bad credential stops the run. MFL could never have run unattended, not once.

**The wake prompt was FPL's.** `mfl_manager` was told to call `fpl_propose_lineup` and
pick a captain, neither of which exists on this platform. A test now asserts every
platform gets a prompt naming its own tools.

**The horizon was invented.** It was `now + 12h`, which never counts down. It is now the
**next kickoff still in the future**, read from the live schedule — a real instant that
advances by itself through the week (Thursday night, then Sunday early, then Sunday
late), so it never goes stale and never goes negative. That also makes proposal expiry
mean something: a proposal now expires when the players start locking.

With a real countdown, MFL joins FPL in `HARD_DEADLINE`, so the "don't act days early"
rule applies honestly. It had to: the run trigger was waiting for the act window while
the policy would have acted 451 hours out, so any path other than the scheduler bypassed
the wait. A test now asserts the two gates open at the same moment.
