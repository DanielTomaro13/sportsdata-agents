// Licence-gated app download (commerce Phase 6).
//
// The product repo is PRIVATE, so the built .app lives on a private GitHub release that
// the public can't reach. Rather than expose a public binaries repo (anyone could grab +
// reverse-engineer the build), we serve the binary through here:
//   GET /download            with  Authorization: Bearer <licence>   (feeds.html, header)
//   GET /download?key=<lic>  one-click link (the fulfilment email)
// The Worker verifies the licence is live, then fetches the latest release asset from the
// private repo with a server-side read-only token and streams it back. Only paying
// customers ever touch the binary; the token + the source stay server-side.
//
// Inert (503) until GITHUB_DOWNLOAD_TOKEN is set — mirrors the proxy's degrade-gracefully
// stance so an un-configured deploy fails loud-but-safe instead of leaking anything.

import type { Env } from "./index";

const CORS: Record<string, string> = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, OPTIONS",
  "access-control-allow-headers": "authorization, content-type",
};

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });

const LIVE = new Set(["active", "trialing", "past_due"]);

// Default to the product repo; overridable so a fork/rename doesn't need a code change.
const DEFAULT_REPO = "DanielTomaro13/sportsdata-mcp";
const UA = "sportsdata-entitlement/1.0";

interface GhAsset {
  name: string;
  url: string; // the API asset url (…/releases/assets/<id>), NOT the browser url
  content_type: string;
  size: number;
}

// Pick the asset a customer should get: a signed/notarized DMG if present, else the
// ad-hoc-signed unsigned zip. Ignore anything else attached to the release.
function pickAsset(assets: GhAsset[]): GhAsset | null {
  return (
    assets.find((a) => a.name.endsWith(".dmg")) ||
    assets.find((a) => a.name.endsWith("-unsigned.zip")) ||
    assets.find((a) => a.name.endsWith(".zip")) ||
    null
  );
}

export async function handleDownload(req: Request, env: Env): Promise<Response> {
  if (req.method !== "GET") return json({ error: "method not allowed" }, 405);

  const url = new URL(req.url);
  const auth = req.headers.get("authorization") || "";
  // Bearer header (feeds.html keeps the key out of the URL) OR ?key= (email one-click,
  // where the key is already in the email body, so the query adds no exposure).
  const key = (auth.startsWith("Bearer ") ? auth.slice(7) : url.searchParams.get("key") || "").trim();
  if (!key) return json({ error: "missing licence key" }, 401);

  const row = await env.DB.prepare(
    "SELECT status FROM entitlements WHERE customer_id = ?",
  ).bind(key).first<{ status: string }>();
  if (!row) return json({ error: "unknown licence" }, 404);
  if (!LIVE.has(row.status)) {
    return json({ error: `licence is not active (status: ${row.status})` }, 403);
  }

  const token = env.GITHUB_DOWNLOAD_TOKEN;
  if (!token) return json({ error: "download not configured" }, 503);
  const repo = env.GITHUB_RELEASE_REPO || DEFAULT_REPO;
  const ghHeaders = {
    authorization: `Bearer ${token}`,
    accept: "application/vnd.github+json",
    "user-agent": UA,
    "x-github-api-version": "2022-11-28",
  };

  // 1) Resolve the latest release's downloadable asset.
  const rel = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, { headers: ghHeaders });
  if (!rel.ok) {
    console.error(`download: release lookup failed (${rel.status}) for ${repo}`);
    return json({ error: "no release available" }, 502);
  }
  const release = (await rel.json()) as { tag_name?: string; assets?: GhAsset[] };
  const asset = pickAsset(release.assets || []);
  if (!asset) {
    console.error(`download: no asset on latest release of ${repo}`);
    return json({ error: "no downloadable asset on the latest release" }, 502);
  }

  // 2) Fetch the asset bytes. The asset API returns a 302 to a pre-signed CDN URL; follow
  // it MANUALLY and drop the GitHub token on the second hop (the CDN URL is already signed,
  // and forwarding our token to a third party would leak it).
  const hop = await fetch(asset.url, {
    headers: { authorization: `Bearer ${token}`, accept: "application/octet-stream", "user-agent": UA },
    redirect: "manual",
  });
  let bytes: Response;
  if (hop.status >= 300 && hop.status < 400) {
    const loc = hop.headers.get("location");
    if (!loc) {
      console.error(`download: asset redirect had no location (${repo})`);
      return json({ error: "asset fetch failed" }, 502);
    }
    bytes = await fetch(loc, { headers: { "user-agent": UA } });
  } else {
    bytes = hop; // some responses stream the octet-stream directly
  }
  if (!bytes.ok || !bytes.body) {
    console.error(`download: asset body fetch failed (${bytes.status}) for ${repo}`);
    return json({ error: "asset fetch failed" }, 502);
  }

  // 3) Stream it back as an attachment (no token, no Set-Cookie passed through).
  return new Response(bytes.body, {
    status: 200,
    headers: {
      "content-type": asset.content_type || "application/octet-stream",
      "content-disposition": `attachment; filename="${asset.name}"`,
      "cache-control": "no-store",
      ...CORS,
    },
  });
}
