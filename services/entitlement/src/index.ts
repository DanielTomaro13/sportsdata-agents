// sportsdata entitlement service (Phase 1):
//   POST /stripe/webhook  — Stripe subscription events → upsert entitlement, issue licence
//   GET  /entitlement     — licence key (Authorization: Bearer) → signed grants
//   GET  /healthz
// The DataGolf/TAB proxy is the next addition (Phase 1b) — see README.

import { validateAssignment } from "./catalogue";
import { sendLicenceEmail } from "./email";
import { handleProxy } from "./proxy";
import { signLicence } from "./sign";
import { chargeCustomerId, subscriptionGrant, verifyStripeSignature } from "./stripe";

// Cloudflare Workers Rate Limiting binding (configured in wrangler.jsonc).
export interface RateLimit {
  limit(options: { key: string }): Promise<{ success: boolean }>;
}

export interface Env {
  DB: D1Database;
  STRIPE_SECRET_KEY: string;
  STRIPE_WEBHOOK_SECRET: string;
  SIGNING_KEY_PKCS8_B64: string;
  // Proxy credentials (Phase 1b) — optional; a provider's proxy is inert without them.
  DATAGOLF_KEY?: string;
  TAB_CLIENT_ID?: string;
  TAB_CLIENT_SECRET?: string;
  // DataGolf proxy rate limiters (protect our shared key's 45 req/min upstream cap).
  DATAGOLF_GLOBAL_RL?: RateLimit;
  DATAGOLF_KEY_RL?: RateLimit;
  // Fulfilment email (Phase 5) — optional; inert without RESEND_API_KEY.
  RESEND_API_KEY?: string;
  LICENCE_FROM_EMAIL?: string;
  LICENCE_DOWNLOAD_URL?: string; // installer download link in the email (default: GH releases)
  LICENCE_FEEDS_URL?: string; // Manage-feeds page link in the email (default: the site)
}

const LIVE_STATUSES = new Set(["active", "trialing", "past_due"]);

const ENTITLEMENT_TTL = 7 * 24 * 3600; // signed licence TTL == the MCP's offline grace
const PERIOD_GRACE = 24 * 3600; // tolerance past current_period_end for clock skew / renewal lag

// CORS: feeds.html (on GitHub Pages) calls /assignment cross-origin with an Authorization
// header, which triggers a preflight. The key gates access, so a permissive origin is fine
// (there are no cookies). Applied to every response + an OPTIONS preflight handler below.
const CORS: Record<string, string> = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-max-age": "86400",
};

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });

function newLicenceKey(): string {
  const b = crypto.getRandomValues(new Uint8Array(20));
  return "sd_live_" + [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}

async function syncSubscription(subId: string, env: Env): Promise<void> {
  const r = await subscriptionGrant(subId, env.STRIPE_SECRET_KEY);
  if (!r.customerId) return;
  const now = Math.floor(Date.now() / 1000);

  // one licence key per Stripe customer, reused across subscription changes
  const cust = await env.DB.prepare("SELECT id FROM customers WHERE stripe_customer_id = ?")
    .bind(r.customerId).first<{ id: string }>();
  let id: string;
  if (cust) {
    id = cust.id;
    if (r.email) await env.DB.prepare("UPDATE customers SET email = ? WHERE id = ?").bind(r.email, id).run();
  } else {
    id = newLicenceKey();
    await env.DB.prepare("INSERT INTO customers (id, stripe_customer_id, email, created_at) VALUES (?, ?, ?, ?)")
      .bind(id, r.customerId, r.email, now).run();
  }

  await env.DB.prepare(
    `INSERT INTO entitlements (customer_id, status, sport_slots, gambling_slots, all_access, current_period_end, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
     ON CONFLICT(customer_id) DO UPDATE SET
       status=?2, sport_slots=?3, gambling_slots=?4, all_access=?5, current_period_end=?6, updated_at=?7`,
  ).bind(id, r.status, r.grant.sport_slots, r.grant.gambling_slots, r.grant.all_access ? 1 : 0, r.periodEnd, now).run();

  // Fulfilment (Phase 5): on first activation, email the licence + setup exactly once.
  // We atomically *claim* the one-time slot with a conditional UPDATE (emailed_at NULL →
  // now) and only send if this writer won — so two concurrent webhook events (e.g.
  // incomplete→active plus an add-on update) can't both pass the check and double-send.
  // Inert without Resend; the claim is released on send failure so a later event retries.
  if (LIVE_STATUSES.has(r.status) && r.email && env.RESEND_API_KEY) {
    const claim = await env.DB.prepare(
      "UPDATE entitlements SET emailed_at = ? WHERE customer_id = ? AND emailed_at IS NULL",
    ).bind(now, id).run();
    if (claim.meta.changes === 1) {
      const sent = await sendLicenceEmail(env, r.email, id, {
        allAccess: r.grant.all_access,
        sportSlots: r.grant.sport_slots,
        gamblingSlots: r.grant.gambling_slots,
      });
      if (!sent) {
        console.error(`fulfilment email failed for ${id} (${r.email}) — claim released, will retry`);
        await env.DB.prepare("UPDATE entitlements SET emailed_at = NULL WHERE customer_id = ?")
          .bind(id).run();
      }
    }
  }
}

// A charge dispute → set the entitlement to a non-live status so the gate + proxy stop
// serving. Reversible: if the dispute is won, a later subscription.updated re-syncs status.
async function freezeOnDispute(chargeId: string, env: Env): Promise<void> {
  const stripeCustomerId = await chargeCustomerId(chargeId, env.STRIPE_SECRET_KEY);
  if (!stripeCustomerId) return;
  const cust = await env.DB.prepare("SELECT id FROM customers WHERE stripe_customer_id = ?")
    .bind(stripeCustomerId).first<{ id: string }>();
  if (!cust) return;
  await env.DB.prepare("UPDATE entitlements SET status = 'disputed', updated_at = ? WHERE customer_id = ?")
    .bind(Math.floor(Date.now() / 1000), cust.id).run();
  console.error(`froze entitlement ${cust.id} — charge dispute on ${chargeId}`);
}

async function handleWebhook(req: Request, env: Env): Promise<Response> {
  const payload = await req.text();
  const ok = await verifyStripeSignature(
    payload, req.headers.get("stripe-signature"), env.STRIPE_WEBHOOK_SECRET, Math.floor(Date.now() / 1000),
  );
  if (!ok) return json({ error: "bad signature" }, 400);
  const event = JSON.parse(payload);

  // idempotency — ignore a redelivered event
  const seen = await env.DB.prepare("SELECT id FROM stripe_events WHERE id = ?").bind(event.id).first();
  if (seen) return json({ ok: true, duplicate: true });

  // Process FIRST, then record as seen — so a thrown error (Stripe API / D1 / Resend)
  // returns 500, Stripe redelivers, and the redelivery is NOT swallowed as a duplicate.
  // syncSubscription is idempotent (upserts + an atomic email claim), so the rare
  // concurrent-redelivery double-run is harmless.
  if (String(event.type).startsWith("customer.subscription.")) {
    const subId = event.data?.object?.id;
    if (subId) await syncSubscription(subId, env);
  } else if (event.type === "charge.dispute.created") {
    // A dispute = they're clawing the money back → freeze access immediately.
    const chargeId = event.data?.object?.charge;
    if (chargeId) await freezeOnDispute(String(chargeId), env);
  }
  await env.DB.prepare("INSERT INTO stripe_events (id, type, received_at) VALUES (?, ?, ?)")
    .bind(event.id, event.type, Math.floor(Date.now() / 1000)).run();
  return json({ ok: true });
}

async function handleEntitlement(req: Request, env: Env): Promise<Response> {
  const auth = req.headers.get("authorization") || "";
  const key = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!key) return json({ error: "missing bearer licence key" }, 401);

  const row = await env.DB.prepare(
    `SELECT e.status, e.sport_slots, e.gambling_slots, e.all_access, e.groups, e.current_period_end
     FROM entitlements e JOIN customers c ON c.id = e.customer_id WHERE c.id = ?`,
  ).bind(key).first<{
    status: string; sport_slots: number; gambling_slots: number;
    all_access: number; groups: string; current_period_end: number;
  }>();
  if (!row) return json({ error: "unknown licence" }, 404);

  const now = Math.floor(Date.now() / 1000);
  // Bound the offline-honoured window to the paid period (+ a small grace for clock skew
  // / renewal lag), so a cached token can't outlive the subscription. For an auto-renewing
  // sub the period end is far out, so the 7-day TTL governs. The token still carries the
  // real `status`, so the MCP revokes on a non-live status at its next ~15-min re-check.
  const periodEnd = Number(row.current_period_end || 0);
  const expires = periodEnd ? Math.min(now + ENTITLEMENT_TTL, periodEnd + PERIOD_GRACE) : now + ENTITLEMENT_TTL;
  const claims = {
    v: 1,
    key,
    status: row.status,
    sport_slots: row.sport_slots,
    gambling_slots: row.gambling_slots,
    all_access: row.all_access === 1,
    groups: JSON.parse(row.groups || "[]"),
    current_period_end: periodEnd,
    issued_at: now,
    expires,
  };
  const token = await signLicence(claims, env.SIGNING_KEY_PKCS8_B64);
  return json({ licence: token, claims });
}

// GET  /assignment — read the current feed assignment + slot budget
// POST /assignment — set it ({ providers: [...] }); enforced against the slot budget
async function handleAssignment(req: Request, env: Env): Promise<Response> {
  const auth = req.headers.get("authorization") || "";
  const key = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!key) return json({ error: "missing bearer licence key" }, 401);

  const row = await env.DB.prepare(
    `SELECT status, sport_slots, gambling_slots, all_access, groups
     FROM entitlements WHERE customer_id = ?`,
  ).bind(key).first<{
    status: string; sport_slots: number; gambling_slots: number;
    all_access: number; groups: string;
  }>();
  if (!row) return json({ error: "unknown licence" }, 404);

  const budget = {
    sport_slots: row.sport_slots,
    gambling_slots: row.gambling_slots,
    all_access: row.all_access === 1,
  };

  if (req.method === "GET") {
    return json({ providers: JSON.parse(row.groups || "[]"), ...budget });
  }

  // Writes require a live subscription — a lapsed/canceled licence can read its budget
  // but not change its assignment.
  if (!LIVE_STATUSES.has(row.status)) {
    return json({ error: `licence is not active (status: ${row.status})` }, 403);
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }
  const requested = (body as { providers?: unknown })?.providers;
  if (!Array.isArray(requested)) {
    return json({ error: "body.providers must be an array of provider ids" }, 400);
  }

  const check = validateAssignment(requested.map(String), row);
  if (!check.ok) return json({ error: check.error }, 422);

  await env.DB.prepare("UPDATE entitlements SET groups = ?, updated_at = ? WHERE customer_id = ?")
    .bind(JSON.stringify(check.providers), Math.floor(Date.now() / 1000), key).run();
  return json({ ok: true, providers: check.providers, ...budget });
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    // CORS preflight (browser sends OPTIONS before a cross-origin /assignment call).
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    try {
      if (url.pathname === "/healthz") return json({ ok: true });
      if (url.pathname === "/stripe/webhook" && req.method === "POST") return await handleWebhook(req, env);
      if (url.pathname === "/entitlement" && req.method === "GET") return await handleEntitlement(req, env);
      if (url.pathname === "/assignment" && (req.method === "GET" || req.method === "POST")) {
        return await handleAssignment(req, env);
      }
      // /proxy/<provider>/<upstream-path...> — licence-authed credentialed-feed proxy
      if (url.pathname.startsWith("/proxy/")) {
        const rest = url.pathname.slice("/proxy/".length);
        const slash = rest.indexOf("/");
        const provider = slash === -1 ? rest : rest.slice(0, slash);
        const subpath = slash === -1 ? "" : rest.slice(slash + 1);
        return await handleProxy(req, env, provider, subpath, ctx);
      }
      return json({ error: "not found" }, 404);
    } catch (e) {
      // Log on the money/serving path so an outage is visible (observability is on).
      console.error(`worker error on ${req.method} ${url.pathname}:`, e);
      return json({ error: String((e as Error)?.message || e) }, 500);
    }
  },
};
