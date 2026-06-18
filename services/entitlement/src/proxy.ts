// Licence-authed proxy for the credentialed feeds (Phase 1b).
//
// Some providers run on OUR upstream credentials and must never ship inside a
// self-host build: DataGolf (our paid `key`) and — if/when a TAB endpoint needs it —
// TAB's OAuth client. A licensed MCP points those providers at
//   GET /proxy/<provider>/<upstream-path>?<query>
// with the licence key as `Authorization: Bearer`; the Worker verifies the licence
// grants that provider, attaches the upstream credential server-side, and streams the
// response back. The customer's build never sees the credential.
//
// Note: TAB's public endpoints use no auth and TAB geo/bot-manages by IP, so they are
// better run client-side from the customer's own IP. TAB is wired here for the OAuth
// case only, and stays inert unless TAB_CLIENT_ID/SECRET are configured.

import type { Env } from "./index";

interface ProxyProvider {
  base: string; // upstream origin + base path
  attach: "datagolf_key" | "tab_oauth";
  headers: Record<string, string>;
}

const PROXY_PROVIDERS: Record<string, ProxyProvider> = {
  datagolf: {
    base: "https://feeds.datagolf.com",
    attach: "datagolf_key",
    headers: {
      "user-agent": "Mozilla/5.0 (compatible; sportsdata-mcp/0.1)",
      accept: "application/json",
    },
  },
  tab: {
    base: "https://api.beta.tab.com.au/v1",
    attach: "tab_oauth",
    headers: {
      "user-agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
      accept: "application/json, text/plain, */*",
      origin: "https://www.tab.com.au",
      referer: "https://www.tab.com.au/",
    },
  },
};

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const LIVE = new Set(["active", "trialing", "past_due"]);

interface EntRow {
  status: string;
  all_access: number;
  groups: string;
}

// The licence grants a proxied provider iff it's active and either all-access or the
// provider is in its assigned feeds (by provider id or any `<provider>.<group>`).
function providerGranted(row: EntRow, provider: string): boolean {
  if (!LIVE.has(row.status)) return false;
  if (row.all_access === 1) return true;
  let groups: string[];
  try {
    groups = JSON.parse(row.groups || "[]");
  } catch {
    groups = [];
  }
  return groups.includes(provider) || groups.some((g) => g.startsWith(provider + "."));
}

// TAB client-credentials token, cached in-isolate (~3h lifetime). Inert without creds.
let tabToken: { token: string; exp: number } | null = null;

async function mintTabToken(env: Env): Promise<string | null> {
  if (!env.TAB_CLIENT_ID || !env.TAB_CLIENT_SECRET) return null;
  const now = Math.floor(Date.now() / 1000);
  if (tabToken && tabToken.exp - 60 > now) return tabToken.token;
  const body = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: env.TAB_CLIENT_ID,
    client_secret: env.TAB_CLIENT_SECRET,
  });
  const r = await fetch("https://api.beta.tab.com.au/oauth/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!r.ok) return null;
  const j = (await r.json()) as { access_token?: string; expires_in?: number };
  if (!j.access_token) return null;
  tabToken = { token: j.access_token, exp: now + Number(j.expires_in || 10800) };
  return tabToken.token;
}

// GET /proxy/<provider>/<subpath...>
export async function handleProxy(
  req: Request,
  env: Env,
  provider: string,
  subpath: string,
): Promise<Response> {
  if (req.method !== "GET") return json({ error: "method not allowed" }, 405);

  const cfg = PROXY_PROVIDERS[provider];
  if (!cfg) return json({ error: `unknown proxy provider: ${provider}` }, 404);

  const auth = req.headers.get("authorization") || "";
  const key = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!key) return json({ error: "missing bearer licence key" }, 401);

  const row = await env.DB.prepare(
    `SELECT e.status, e.all_access, e.groups
     FROM entitlements e JOIN customers c ON c.id = e.customer_id WHERE c.id = ?`,
  )
    .bind(key)
    .first<EntRow>();
  if (!row) return json({ error: "unknown licence" }, 404);
  if (!providerGranted(row, provider)) {
    return json({ error: `licence does not grant ${provider}` }, 403);
  }

  // Build the upstream URL. The host is pinned to the provider's base — `subpath` only
  // ever extends the path (leading slashes stripped, host re-asserted to block SSRF).
  const clean = subpath.replace(/^\/+/, "");
  const target = new URL(`${cfg.base}/${clean}`);
  if (target.host !== new URL(cfg.base).host) {
    return json({ error: "invalid path" }, 400);
  }
  new URL(req.url).searchParams.forEach((v, k) => target.searchParams.set(k, v));

  const headers: Record<string, string> = { ...cfg.headers };
  if (cfg.attach === "datagolf_key") {
    if (!env.DATAGOLF_KEY) return json({ error: "datagolf proxy not configured" }, 503);
    target.searchParams.set("key", env.DATAGOLF_KEY);
  } else if (cfg.attach === "tab_oauth") {
    const tok = await mintTabToken(env);
    if (!tok) return json({ error: "tab proxy not configured" }, 503);
    headers["authorization"] = `Bearer ${tok}`;
  }

  const upstream = await fetch(target.toString(), { method: "GET", headers });
  const buf = await upstream.arrayBuffer();
  // Pass through status + content-type only; never forward Set-Cookie (Akamai bm_*).
  return new Response(buf, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") || "application/json" },
  });
}
