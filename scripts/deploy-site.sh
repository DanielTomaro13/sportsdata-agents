#!/bin/sh
# Publish site/ to the PUBLIC Pages repo (sportsdata-site). The product repos
# stay private; only these marketing assets go public. Run after editing site/.
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
git clone --depth 1 "https://github.com/DanielTomaro13/sportsdata-site.git" "$WORK"
cp "$REPO_DIR"/site/index.html "$REPO_DIR"/site/demo-fallback.json "$WORK"/
cd "$WORK"
git add -A
git diff --cached --quiet && { echo "site unchanged"; exit 0; }
git commit -m "publish site update"
git push origin main
echo "published — live at https://danieltomaro13.github.io/sportsdata-site/ in ~a minute"
