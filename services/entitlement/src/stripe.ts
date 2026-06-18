// Stripe: webhook signature verification + resolving a subscription to a feed grant.

import { Grant, grantFromItems } from "./catalogue";

const STRIPE_API = "https://api.stripe.com/v1";

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Verify the `Stripe-Signature` header: v1 = HMAC-SHA256(secret, `${t}.${payload}`),
// with a 5-minute timestamp tolerance.
export async function verifyStripeSignature(
  payload: string, sigHeader: string | null, secret: string, nowSec: number,
): Promise<boolean> {
  if (!sigHeader) return false;
  const fields: Record<string, string> = {};
  for (const part of sigHeader.split(",")) {
    const i = part.indexOf("=");
    if (i > 0) fields[part.slice(0, i)] = part.slice(i + 1);
  }
  const t = fields["t"], v1 = fields["v1"];
  if (!t || !v1) return false;
  if (Math.abs(nowSec - Number(t)) > 300) return false;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${t}.${payload}`));
  return timingSafeEqual(toHex(mac), v1);
}

async function stripeGet(path: string, key: string): Promise<any> {
  const r = await fetch(`${STRIPE_API}${path}`, { headers: { Authorization: `Bearer ${key}` } });
  if (!r.ok) throw new Error(`stripe ${path} -> ${r.status}`);
  return r.json();
}

export interface SubResult {
  grant: Grant;
  status: string;       // active | trialing | past_due | canceled | …
  periodEnd: number;    // unix seconds
  customerId: string;
  email: string | null;
}

// Resolve a subscription id → its grant, status, period end, and customer.
// Expands items → price → product (for the sportsdata_sku) and the customer (email).
export async function subscriptionGrant(subId: string, key: string): Promise<SubResult> {
  const sub = await stripeGet(
    `/subscriptions/${encodeURIComponent(subId)}?expand[]=items.data.price.product&expand[]=customer`,
    key,
  );
  const items = (sub.items?.data || []).map((it: any) => ({
    sku: String(it.price?.product?.metadata?.sportsdata_sku || ""),
    quantity: Number(it.quantity || 1),
  }));
  const customer = sub.customer;
  return {
    grant: grantFromItems(items),
    status: String(sub.status || "inactive"),
    periodEnd: Number(sub.current_period_end || 0),
    customerId: typeof customer === "string" ? customer : String(customer?.id || ""),
    email: typeof customer === "object" ? (customer?.email ?? null) : null,
  };
}
