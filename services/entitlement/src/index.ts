// sportsdata API worker — slimmed to a download redirect + health endpoint.
//
// This service was the commerce/entitlement backend (Stripe webhook → D1 →
// signed feed entitlements → gated /download → credentialed-feed proxy) until
// the whole product went FREE and open source (2026-07). Everything it sold is
// now public; what remains is:
//   GET /healthz   — uptime signal for the scheduled monitor
//   GET /download  — 302 to the latest public GitHub release (keeps every old
//                    email/site link working)
//   anything else  — 410 Gone with a pointer to the repo
// The old implementation is in git history if a hosted/premium offering ever
// brings parts of it back.

export interface Env {
  GITHUB_RELEASE_REPO?: string; // owner/repo override
}

const REPO_DEFAULT = "DanielTomaro13/sportsdata-mcp";

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
  });

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/healthz") {
      return json({ ok: true, mode: "free-open-source" });
    }

    if (url.pathname === "/download") {
      const repo = env.GITHUB_RELEASE_REPO || REPO_DEFAULT;
      return Response.redirect(`https://github.com/${repo}/releases/latest`, 302);
    }

    return json(
      {
        error: "gone — sportsdata is free and open source now",
        repo: `https://github.com/${REPO_DEFAULT}`,
        site: "https://sportsdata-ai.com",
      },
      410,
    );
  },
};
