#!/bin/sh
# Build the downloadable desktop bundle (NOT App Store — direct download).
#
#   sh scripts/build-desktop.sh
#
# Produces dist/sportsdata/ (a self-contained onedir: the `sportsdata` binary
# plus its bundled Python runtime and the sportsdata-mcp data plane) and a
# zip you can put on the download page. On macOS, sign + notarize afterwards
# with a Developer ID cert (see PRICING.md / POST_DEV.md) so Gatekeeper is
# happy on a direct download.
#
# Prereqs: pip install -e ".[build]"  and the sibling sportsdata-mcp checkout.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MCP_DIR="${SPORTSDATA_MCP_DIR:-$REPO/../sportsdata-mcp}"
APP_NAME="sportsdata"
VERSION="$("$REPO/.venv/bin/python" -c 'import sportsdata_agents; print(sportsdata_agents.__version__)')"

echo "building $APP_NAME $VERSION (onedir, direct-download)"
cd "$REPO"

# bake the license public key into the build if one is set (else free-tier-only)
PUBKEY="${SPORTSDATA_LICENSE_PUBKEY:-}"
[ -n "$PUBKEY" ] && echo "  embedding license public key" || echo "  no SPORTSDATA_LICENSE_PUBKEY — build verifies no licenses (free tier only)"

# the MCP data plane ships alongside so the app is self-contained
MCP_BIN="$MCP_DIR/.venv/bin/sportsdata-mcp"
ADD_MCP=""
if [ -x "$MCP_BIN" ]; then
  ADD_MCP="--add-binary $MCP_BIN:."
  echo "  bundling data plane: $MCP_BIN"
else
  echo "  WARNING: $MCP_BIN not found — the bundle will need an external sportsdata-mcp"
fi

.venv/bin/pyinstaller \
  --name "$APP_NAME" \
  --onedir \
  --console \
  --collect-all sportsdata_agents \
  --collect-all litellm \
  --collect-all tiktoken \
  --hidden-import tiktoken_ext.openai_public \
  --copy-metadata sportsdata-agents \
  $ADD_MCP \
  --noconfirm \
  "$REPO/src/sportsdata_agents/__main__.py" 2>/dev/null || {
    echo "pyinstaller not installed — run: pip install -e '.[build]'"; exit 1; }

# embed the pubkey as a sibling .env the launcher sources (kept out of the binary
# so a re-key doesn't need a rebuild)
if [ -n "$PUBKEY" ]; then
  echo "SPORTSDATA_LICENSE_PUBKEY=$PUBKEY" > "dist/$APP_NAME/.env"
fi

( cd dist && zip -qr "$APP_NAME-$VERSION-macos.zip" "$APP_NAME" )
echo "done → dist/$APP_NAME-$VERSION-macos.zip"
echo "next: codesign + notarize for Gatekeeper (Developer ID), then put it on the download page"
