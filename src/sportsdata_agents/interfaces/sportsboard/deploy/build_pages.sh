#!/usr/bin/env bash
# Build the STATIC sports board into a destination dir. GitHub Pages can't run
# the warehouse, so the built page ships a captured replay and forces replay
# mode. Make a static build LIVE by opening it with ?api=https://your-host
# (or by setting apiBase in the config.js written below).
#
#   bash build_pages.sh [DEST]                     # DEST defaults to ./dist
#   bash build_pages.sh ~/…/sportsdata-site/sports # deploy into the public site
#
# Capture a fresh replay first (real data must be in the warehouse):
#   python -m sportsdata_agents.interfaces.sportsboard.capture_replay 18 90
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../static"
DEST="${1:-$HERE/dist}"

mkdir -p "$DEST/data"
cp "$SRC/index.html"       "$DEST/index.html"
cp "$SRC/styles.css"       "$DEST/styles.css"
cp "$SRC/app.js"           "$DEST/app.js"
cp "$SRC/data/replay.json" "$DEST/data/replay.json"

# Static config: replay by default; set apiBase (or open with ?api=) to go live.
cat > "$DEST/config.js" <<'EOF'
// STATIC sports board — animates a captured sequence of real market data.
// Make it LIVE by deploying the warehouse-backed board (see deploy/serve_live.sh
// for the free path, or deploy/render.yaml) and opening the page with
//   …/sports/?api=https://your-host
window.SB_CONFIG = { forceReplay: true, replayUrl: "data/replay.json", apiBase: null };
EOF

touch "$DEST/.nojekyll"   # serve files verbatim (no Jekyll)
echo "built sports board -> $DEST ($(du -sh "$DEST" | cut -f1))"
ls -1 "$DEST"
