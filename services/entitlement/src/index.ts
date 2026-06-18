// sportsdata entitlement service (Phase 1):
//   POST /stripe/webhook  — Stripe subscription events → upsert entitlement, issue licence
//   GET  /entitlement     — licence key (Authorization: Bearer) → signed grants
//   GET  /healthz
// The DataGolf/TAB proxy is the next addition (Phase 1b) — see README.

import { sendLicenceEmail } from "./email";
import { handleProxy } from "./proxy";
import { signLicence } from "./sign";
import { subscriptionGrant, verifyStripeSignature } from "./stripe";

export interface Env {
  DB: D1Database;
  STRIPE_SECRET_KEY: string;
  STRIPE_WEBHOOK_SECRET: string;
  SIGNING_KEY_PKCS8_B64: string;
  // Proxy credentials (Phase 1b) — optional; a provider's proxy is inert without them.
  DATAGOLF_KEY?: string;
  TAB_CLIENT_ID?: string;
  TAB_CLIENT_SECRET?: string;
  // Fulfilment email (Phase 5) — optional; inert without RESEND_API_KEY.
  RESEND_API_KEY?: string;
  LICENCE_FROM_EMAIL?: string;
}

const LIVE_STATUSES = new Set(["active", "trialing", "past_due"]);

const ENTITLEMENT_TTL = 7 * 24 * 3600; // signed licence TTL == the MCP's offline grace

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

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
  // Guarded by entitlements.emailed_at so incomplete→active transitions still send, and
  // later subscription.updated events (add-ons) don't re-email. Inert without Resend.
  if (LIVE_STATUSES.has(r.status) && r.email && env.RESEND_API_KEY) {
    const e = await env.DB.prepare("SELECT emailed_at FROM entitlements WHERE customer_id = ?")
      .bind(id).first<{ emailed_at: number | null }>();
    if (!e?.emailed_at) {
      const sent = await sendLicenceEmail(env, r.email, id, {
        allAccess: r.grant.all_access,
        sportSlots: r.grant.sport_slots,
        gamblingSlots: r.grant.gambling_slots,
      });
      if (sent) {
        await env.DB.prepare("UPDATE entitlements SET emailed_at = ? WHERE customer_id = ?")
          .bind(now, id).run();
      }
    }
  }
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
  await env.DB.prepare("INSERT INTO stripe_events (id, type, received_at) VALUES (?, ?, ?)")
    .bind(event.id, event.type, Math.floor(Date.now() / 1000)).run();

  if (String(event.type).startsWith("customer.subscription.")) {
    const subId = event.data?.object?.id;
    if (subId) await syncSubscription(subId, env);
  }
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
  const claims = {
    v: 1,
    key,
    status: row.status,
    sport_slots: row.sport_slots,
    gambling_slots: row.gambling_slots,
    all_access: row.all_access === 1,
    groups: JSON.parse(row.groups || "[]"),
    issued_at: now,
    expires: now + ENTITLEMENT_TTL,
  };
  const token = await signLicence(claims, env.SIGNING_KEY_PKCS8_B64);
  return json({ licence: token, claims });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    try {
      if (url.pathname === "/healthz") return json({ ok: true });
      if (url.pathname === "/stripe/webhook" && req.method === "POST") return await handleWebhook(req, env);
      if (url.pathname === "/entitlement" && req.method === "GET") return await handleEntitlement(req, env);
      // /proxy/<provider>/<upstream-path...> — licence-authed credentialed-feed proxy
      if (url.pathname.startsWith("/proxy/")) {
        const rest = url.pathname.slice("/proxy/".length);
        const slash = rest.indexOf("/");
        const provider = slash === -1 ? rest : rest.slice(0, slash);
        const subpath = slash === -1 ? "" : rest.slice(slash + 1);
        return await handleProxy(req, env, provider, subpath);
      }
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: String((e as Error)?.message || e) }, 500);
    }
  },
};
