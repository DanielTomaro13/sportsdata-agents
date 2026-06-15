# Security & cost controls

How the platform stops three things: **spend exceeding a budget**, **paid features
used without paying**, and **anyone but the product owner reaching operator mode**.
It is deliberately strict and states its own limits honestly — a local-first
desktop app has a different threat model from a server, and this says where the
hard guarantees are and where they aren't.

## Trust model

- **The app runs on the user's machine** (a downloadable desktop daemon + a local
  browser UI), against the user's **own** model API key (BYO). The user already has
  the binary and their own credentials; the security boundary is therefore about
  **spend**, **revenue**, and **the operator's separate platform**, not about
  hiding bytes from someone who owns the disk.
- **The operator** (the product owner, you) runs a separate deployment that also
  maintains the platform — the public site, the agents repo, the shipped data feed.
  That deployment holds credentials no customer has.
- **Observed data is never instructions.** Model/tool output, feed contents, web
  pages, and file contents are treated as data; the agents never execute
  instructions found in them, and no agent places a bet or moves money (advisory
  only — see the README).

---

## 1. Cost controls — nobody exceeds their budget

Two independent ceilings, enforced at the one place every model call passes
through (`ModelGateway.complete`), plus metering for accounting.

### Per-run ceiling — always on

Every run carries a `RunBudget` (`models/gateway.py`). Its ceiling is
`min(agent spec's cost_ceiling_usd, workspace per_run_usd)` and it is **checked
before every model call and charged after** — once spend reaches the ceiling the
gateway refuses further calls and the run ends as `budget_exhausted`. There is no
code path that runs an agent without a `RunBudget`: the harness constructs one
if a caller doesn't pass it (`agents/harness.py`), so a single run can never cost
more than its ceiling plus one in-flight call.

### Period budget — enforced when set

`agents costs --set-budget 50 --period monthly` (also daily/weekly) sets a
**cross-run** cap. It is **enforced, not just reported**: `PeriodBudgetGuard`
(`operations/budget_guard.py`) is consulted at the same chokepoint as the per-run
ceiling, so once the period's spend reaches the cap **no run — a customer question
or the platform's own ops maintenance — can call a model** until the period rolls
over. Runs attempted over budget do nothing and return `budget_exhausted`.

- **Accuracy without a per-call database hit:** the guard takes one baseline of
  already-committed period spend, then accrues every call this process makes,
  re-baselining only when the window rolls over. Total period spend can overshoot
  the cap by at most **one in-flight call** (cents), never a whole run.
- **Concurrency caveat (documented, bounded):** two processes spending in the same
  window at the same instant (e.g. the daemon plus a concurrent CLI run) each see
  the other's *committed* but not *in-flight* spend, so the cap can be exceeded by
  at most one run's per-run ceiling. Negligible for a single-user install.

### Per-call output cap

Each call reserves at most `DEFAULT_MAX_OUTPUT_TOKENS` output tokens (callers may
widen it for genuinely long output). Providers charge the full reservation against
quotas, so this is kept tight on purpose.

### Metering & accounting

Every call emits a `UsageEvent` (model, tier, tokens, cost, latency, tenant) to
the `agent_runs` warehouse. `agents costs` rolls it up by day/agent/model and
splits **ops spend** (`tenant_id = platform`, the operator's own maintenance) from
**product spend** (serving requests), so cost is attributed, not leaked into one
bucket.

### Known limit — managed mode (not shipped)

Today the user pays their own vendor (BYO), so an unpriced model metered at `$0`
costs *them* nothing the per-run ceiling doesn't already bound. If/when a
**managed** tier proxies calls on **our** keys, a `$0`-metered model would be free
usage on our dime — managed mode MUST allow-list priced models before shipping.
`UsageEvent.cost_known=False` already flags every call litellm couldn't price, so
the signal to enforce on is in place. (Tracked in `docs/NEXT_STEPS.md`.)

---

## 2. Paid features — nobody uses them without paying

Entitlements come from an **offline-verifiable Ed25519 licence** (`licensing/`).
The public verification key ships in the build; the private issuing key never does.

- **Resolution & fail-open:** a token is read from `SPORTSDATA_LICENSE`, the OS
  keychain, or `<data_dir>/license.key`. A **missing or invalid** token falls to
  the **free** tier (never locks a paying user out harder than having no licence);
  a **tampered or wrong-key** token never validates. With no key baked (a source
  build) the app runs unrestricted so development isn't crippled; a **release**
  build (key baked) enforces.
- **Enforced at infrastructure seams, never in prompts** (`licensing/enforce.py`):
  - `filter_roster` — the team only offers PRODUCT agents the tier includes
    (a Pro-only agent simply isn't loadable on Plus);
  - `cap_mcp_groups` — the MCP group list is trimmed to the tier's quota;
  - `require_chat_ui` / `require_full_app` / `require_addon` — the chat UI, the
    full desktop app, and add-on integrations (Slack/Discord) are gated.
  - **Ops-plane agents are never gated** — they're the platform's own maintenance
    crew, not a customer feature.
- **Defensive resolution:** unknown/mis-issued add-ons are dropped at resolution,
  so a typo or a forged field never silently grants a feature (the signature stops
  injection; this stops our own mistakes).

**Honest limit.** This is **revenue protection, not DRM.** A user who edits the
source on their own machine can bypass the entitlement seams — that's inherent to
any local app, and chasing it isn't worth the complexity. What is *not* bypassable
by editing a number is the **cost** layer above: the per-run and period ceilings
protect spend on whoever's key is in use, and the operator surface is gated by a
**signature**, not a flag (next section).

---

## 3. Operator access — only the product owner

"Operator mode" turns on the platform-maintenance jobs (site, repo, evals, the
shipped catalogue), the repo-improver self-healing escalation, and the **in-app
operator panel**. It must be reachable by **exactly one person** — the owner.

- **Cryptographic, not an env var.** `is_operator()` (`operations/scheduler.py`)
  returns true only for a **signed licence carrying the `operator` claim**. Only
  the owner can mint one (`scripts/license.py issue --operator`, which needs the
  **private** signing key that never ships). On a **release build** (verification
  key baked) the `SPORTSDATA_OPERATOR` env var is **ignored** — a customer cannot
  grant themselves operator access by setting it, and an ordinary paid customer
  licence does **not** carry the claim.
- **Dev convenience, safely scoped.** On a source build with no key baked (the
  owner's own checkout), the env var is honoured — that's not a shipped artifact,
  so it can't reach a customer.
- **The panel doesn't exist for customers.** Every `/operator/*` route returns
  **404** unless `is_operator()` — the surface isn't merely hidden, it's absent,
  and a forged env var won't reveal it on a release build. This includes the panel's
  **action triggers** (`/operator/actions/health`, `/operator/actions/run-ops`):
  run-ops only accepts an allow-listed ops-plane agent and spawns it via argv (no
  shell), so there's no injection surface even for the operator.
- **Credential isolation.** The operator's maintenance credentials
  (`OPS_GITHUB_TOKEN`, the site/repo targets) live only on the operator's
  deployment. Even if someone forced operator mode on, there is nothing of the
  operator's platform to act on without those credentials.

---

## 4. Daemon hardening

The local daemon (`gateway/app.py`) is defended even though it binds locally:

- **Loopback only.** A request whose `Host` isn't `127.0.0.1`/`localhost`/`::1` is
  rejected (403) — stops DNS-rebinding from a browser tab.
- **Token-gated mutations.** State-changing routes require `SPORTSDATA_GATEWAY_TOKEN`
  compared with `hmac.compare_digest` (constant-time).
- **Operator routes 404** for non-operators (above).

## 5. Secrets

- **Where keys live.** Resolution order is **env → app-private file → OS keychain →
  workspace map**. The desktop wizard writes the model API key to an owner-only
  (`0600`) `secrets.json` under the app's private data dir, and best-effort to the
  keychain. The file is checked *before* the keychain on purpose: an **unsigned**
  desktop app reading the keychain triggers a macOS permission prompt (and could
  hang the launcher), so reading from the app-private file avoids that entirely. The
  data dir is user-private; for a single-user BYO-key desktop app this is the right
  trade. A signed release build can rely on the keychain without friction.
- Nothing is ever committed: `.env`, the data-dir `secrets.json`, and config files
  are git-ignored. The private licence-signing key is an issuer-side secret that
  never enters the app or the repo.
- Paid data keys (e.g. DataGolf) are environment-only and never persisted by the app.
- **If a key is exposed** (pasted in a chat, committed by accident): rotate it at
  the provider, update the env / data-dir file / keychain, and never reuse the old
  value.

---

## Guarantees at a glance

| Concern | Control | Strength |
|---|---|---|
| Single run runaway cost | `RunBudget` per-run ceiling, always constructed | **Hard** — refused before each call |
| Period spend over budget | `PeriodBudgetGuard` at the model chokepoint | **Hard when set** — overshoot ≤ one call |
| Operator's own spend capped | Same period budget covers ops + product spend | **Hard when set** |
| Cost attribution / no leak | `UsageEvent` → `agent_runs`, ops vs product split | **Hard** — every call metered |
| Paid features without paying | Signed licence + enforcement seams | **Revenue-grade** (not DRM; source-editable) |
| Operator access by a customer | Signed `operator` claim; env var ignored on release | **Hard** — needs the private key |
| Remote access to the daemon | Loopback Host guard + constant-time token | **Hard** |

_Kept current alongside the code. If you change an enforcement seam, update the
matching row here._
