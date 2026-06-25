# Domain cutover — `sportsdata-ai.com`

Point the public marketing site at `sportsdata-ai.com`. **Site-first**: the entitlement
Worker stays on `…workers.dev` (it's a backend API customers never see; moving it to
`api.sportsdata-ai.com` is an optional later phase, below).

The domain is registered through **Cloudflare Registrar**, so the zone is already in the
Cloudflare account — no nameserver change. The marketing site is a GitHub Pages project page
in the **public `DanielTomaro13/sportsdata-site`** repo, published by `scripts/deploy-site.sh`.

> **Why the order matters:** if the site's URLs flip to `sportsdata-ai.com` before DNS
> resolves, fulfilment emails would link to a dead host. Do the DNS + Pages steps FIRST,
> confirm the domain serves, THEN flip the URLs.

---

## Phase 0 — already pre-staged (shipped, inert)
- Worker `ALLOWED_ORIGINS` already allows `https://sportsdata-ai.com` + `https://www.…`
  (an unused allow-list entry does nothing until a request actually comes from that origin).
- `deploy-site.sh` publishes `site/CNAME` **if it exists** (it doesn't yet — created in Phase 3).

Nothing about the live system changed.

## Phase 1 — DNS (you, Cloudflare dashboard → `sportsdata-ai.com` → DNS → Records)
Add these, all **DNS only (grey cloud)** so GitHub manages the TLS cert cleanly:

| Type | Name | Value | Proxy |
|------|------|-------|-------|
| A | `@` | `185.199.108.153` | DNS only |
| A | `@` | `185.199.109.153` | DNS only |
| A | `@` | `185.199.110.153` | DNS only |
| A | `@` | `185.199.111.153` | DNS only |
| CNAME | `www` | `danieltomaro13.github.io` | DNS only |

(The four A records are GitHub Pages' apex IPs. `www` → your Pages host redirects to the apex.)

## Phase 2 — GitHub Pages (you, in the `sportsdata-site` repo)
1. **Settings → Pages → Custom domain** → enter `sportsdata-ai.com` → Save.
2. Wait for the DNS check to go green (minutes to ~an hour), then tick **Enforce HTTPS**
   (it appears once GitHub provisions the cert — can take up to 24 h, usually much less).

Confirm `https://sportsdata-ai.com` serves the site. **Then tell me "DNS is live."**

## Phase 3 — flip the URLs (me, on your signal — one coordinated step)
1. Create `site/CNAME` containing `sportsdata-ai.com` (so re-publishes keep the custom domain).
2. Point fulfilment emails' feeds link at the new domain — add to the Worker config:
   ```jsonc
   // wrangler.jsonc → top level
   "vars": { "LICENCE_FEEDS_URL": "https://sportsdata-ai.com/feeds.html" }
   ```
   then `npx wrangler deploy`. (`LICENCE_FEEDS_URL` overrides `DEFAULT_FEEDS_URL`; the
   download link stays on the Worker, which is fine.)
3. `site/entitlement.json` stays pointed at the Worker (`…workers.dev`) — feeds.html keeps
   calling the same API; the new site origin is already CORS-allowed (Phase 0).
4. `./scripts/deploy-site.sh` to republish (now ships the CNAME).
5. Verify: `https://sportsdata-ai.com/feeds.html` loads, can paste a key + save (CORS OK),
   and a fresh fulfilment email links to `sportsdata-ai.com/feeds.html`.

## Phase 4 — move the Worker to `api.sportsdata-ai.com` (OPTIONAL, later)
1. You: **Workers & Pages → sportsdata-entitlement → Settings → Domains & Routes → Add
   Custom Domain → `api.sportsdata-ai.com`** (Cloudflare auto-creates the proxied record).
2. Me: set `"ENTITLEMENT_PUBLIC_URL": "https://api.sportsdata-ai.com"` in `wrangler.jsonc`
   vars, update `site/entitlement.json` → `{"url":"https://api.sportsdata-ai.com"}`,
   republish, redeploy. Then the email download link + feeds.html API both use the branded host.
3. Keep the `…workers.dev` host working (Cloudflare serves both) so any already-sent email
   links don't break.

## Rollback
- Site: clear the GitHub Pages custom domain + delete `site/CNAME`, republish → back to
  `danieltomaro13.github.io/sportsdata-site/`.
- Worker URL: remove the `vars` entry + redeploy → falls back to the baked defaults.
Each phase is independently reversible; nothing is destructive.
