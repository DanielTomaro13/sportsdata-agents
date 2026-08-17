#!/bin/sh
# Publish site/ to the PUBLIC Pages repo (sportsdata-site). The product repos
# stay private; only these marketing assets go public. Run after editing site/.
#
# CAUTION — this OVERWRITES the five files below in the public repo. That repo is
# now also edited directly (it holds agents.html, engines.html, search.html, the
# boards and the whole SEO layer, none of which live here), so site/ here must be
# re-synced FROM it before this runs or those direct edits are silently reverted.
# That has already happened once: an old copy here still carried retired
# Base/Plus/Pro packaging copy long after the public site had dropped it.
#
#   cp ../sportsdata-site/{index,terms,privacy}.html site/    # before deploying
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
git clone --depth 1 "https://github.com/DanielTomaro13/sportsdata-site.git" "$WORK"
cp "$REPO_DIR"/site/index.html "$REPO_DIR"/site/demo-fallback.json "$REPO_DIR"/site/catalogue.json "$REPO_DIR"/site/terms.html "$REPO_DIR"/site/privacy.html "$WORK"/
# retired pages must vanish from the PUBLIC repo too (it mirrors this dir)
rm -f "$WORK"/feeds.html "$WORK"/refunds.html "$WORK"/stripe.json "$WORK"/entitlement.json
# CNAME binds the Pages site to a custom domain (sportsdata-ai.com). Only create site/CNAME
# AS PART OF the domain cutover (see DOMAIN-CUTOVER.md) — once published, GitHub Pages serves
# at the custom domain, so don't add it until the DNS records are live.
[ -f "$REPO_DIR"/site/CNAME ] && cp "$REPO_DIR"/site/CNAME "$WORK"/
cd "$WORK"
git add -A
git diff --cached --quiet && { echo "site unchanged"; exit 0; }
# noreply author: never leak a personal email into the public repo's history
git -c user.name="DanielTomaro13" -c user.email="DanielTomaro13@users.noreply.github.com" \
    commit -m "publish site update"
git push origin main
echo "published — live at https://danieltomaro13.github.io/sportsdata-site/ in ~a minute"
