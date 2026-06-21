// Licence-key hashing for D1 at-rest. The customer-facing key stays `sd_live_…` (emailed,
// presented as the bearer); D1 stores only its SHA-256, so a DB read never yields a usable
// key. Lookups accept hash OR raw during/after the migration (a raw row just stops matching
// once it's been hashed), so there's no flag-day.

export async function hashKey(key: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// A licence key is already hashed iff it's 64 lowercase hex chars (sd_live_… never is).
export function looksHashed(id: string): boolean {
  return /^[0-9a-f]{64}$/.test(id);
}

// ── Download token ──────────────────────────────────────────────────────────
// The fulfilment email's one-click download link used to carry the raw licence key in the
// URL (`/download?key=sd_live_…`). That key is the full bearer credential — in a URL it can
// leak via referrer, history, proxy logs, or a forwarded email. Instead we mint a
// download-ONLY, time-limited token: it identifies the customer by their *hashed* id and is
// accepted at /download only — never at /entitlement, /assignment or /proxy. Even fully
// leaked it grants nothing but the (still licence-gated, still expiring) binary. HMAC-SHA256
// keeps it unforgeable; inert without DOWNLOAD_TOKEN_SECRET (the email falls back to ?key=).

export const DOWNLOAD_TOKEN_TTL = 30 * 24 * 3600; // email links stay good for 30 days

async function hmacHex(secret: string, msg: string): Promise<string> {
  const k = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Constant-time equality for equal-length hex strings (a mismatch in length is not secret).
function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// Token = "<customerHash>.<exp>.<hmac>" — all URL-safe (hex + digits + dots), no escaping.
export async function signDownloadToken(
  customerHash: string,
  exp: number,
  secret: string,
): Promise<string> {
  const msg = `${customerHash}.${exp}`;
  return `${msg}.${await hmacHex(secret, msg)}`;
}

// Returns the customer's hashed id iff the token is well-formed, unexpired and signature
// matches; else null. The hash it returns is the customers/entitlements PK to look up.
export async function verifyDownloadToken(
  token: string,
  secret: string,
  now: number,
): Promise<string | null> {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [customerHash, expStr, sig] = parts;
  if (!/^[0-9a-f]{64}$/.test(customerHash) || !/^[0-9]+$/.test(expStr)) return null;
  if (Number(expStr) < now) return null;
  const expected = await hmacHex(secret, `${customerHash}.${expStr}`);
  return timingSafeEqualHex(sig, expected) ? customerHash : null;
}
