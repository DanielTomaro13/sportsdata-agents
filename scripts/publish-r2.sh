#!/bin/sh
# Publish the latest sportsdata-mcp release binaries to the R2 download origin.
#
# /download serves R2 first (OS-aware) and falls back to the GitHub release, so this is
# what makes the R2 path live for a new release. Run once per release, after the tag's
# workflow has published its assets:
#
#   sh scripts/publish-r2.sh              # latest release of the product repo
#   sh scripts/publish-r2.sh v0.17.2      # a specific tag
#
# Needs: gh (authed on the private repo) + wrangler with the r2 scope
# (`wrangler login` again if `r2 object put` says unauthorised).
set -eu

REPO="${SPORTSDATA_RELEASE_REPO:-DanielTomaro13/sportsdata-mcp}"
BUCKET="${SPORTSDATA_R2_BUCKET:-sportsdata-downloads}"
TAG="${1:-$(gh release view --repo "$REPO" --json tagName --jq .tagName)}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "publishing $REPO $TAG → r2://$BUCKET/latest/"
gh release download "$TAG" --repo "$REPO" --dir "$WORKDIR" \
  --pattern "*-macos-unsigned.zip" --pattern "*.dmg" --pattern "*-windows.zip"

put() { # put <local-file> <object-key>
  echo "  $2  ($(du -h "$1" | cut -f1 | tr -d ' '))"
  (cd "$(dirname "$0")/../services/entitlement" && \
    npx wrangler r2 object put "$BUCKET/$2" --file "$1" --content-type application/zip)
}

MAC="$(ls "$WORKDIR"/*.dmg 2>/dev/null | head -1 || ls "$WORKDIR"/*-macos-unsigned.zip 2>/dev/null | head -1 || true)"
WIN="$(ls "$WORKDIR"/*-windows.zip 2>/dev/null | head -1 || true)"
[ -n "$MAC" ] && put "$MAC" "latest/sportsdata-mcp-macos.zip" || echo "  (no macOS asset on $TAG)"
[ -n "$WIN" ] && put "$WIN" "latest/sportsdata-mcp-windows.zip" || echo "  (no Windows asset on $TAG)"
echo "done — /download now serves these before falling back to the GitHub release"
