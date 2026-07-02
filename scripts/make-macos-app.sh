#!/bin/sh
# Assemble sportsdata.app around the PyInstaller onedir bundle (P4 M4.3).
#
#   sh scripts/build-desktop.sh        # produces dist/sportsdata/ (the onedir)
#   sh scripts/make-macos-app.sh       # wraps it as dist/sportsdata.app
#
# The .app is the "daemon + browser UI" model (plan §3 option D): its launcher
# starts `sportsdata app` and opens the chat UI. Sign + notarize it afterwards
# with scripts/sign-and-notarize.sh once you have an Apple Developer ID.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="sportsdata"
DIST="$REPO/dist"
ONEDIR="$DIST/$APP_NAME"                  # from build-desktop.sh
APP="$DIST/$APP_NAME.app"
PKG="$REPO/packaging/macos"
VPY="$REPO/.venv/bin/python"; [ -x "$VPY" ] || VPY="python"
VERSION="$("$VPY" -c 'import sportsdata_agents; print(sportsdata_agents.__version__)' 2>/dev/null || echo "0.0.0")"

if [ ! -d "$ONEDIR" ]; then
  echo "error: $ONEDIR not found — run 'sh scripts/build-desktop.sh' first" >&2
  exit 1
fi

echo "assembling $APP_NAME.app $VERSION"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 1. the bundled runtime + binary go under Resources/sportsdata/
cp -R "$ONEDIR" "$APP/Contents/Resources/$APP_NAME"

# 2. the launcher is the bundle's main executable
cp "$PKG/launcher.sh" "$APP/Contents/MacOS/$APP_NAME-launcher"
chmod +x "$APP/Contents/MacOS/$APP_NAME-launcher"

# 3. Info.plist with the version substituted in
sed "s/__VERSION__/$VERSION/g" "$PKG/Info.plist.template" > "$APP/Contents/Info.plist"

# 4. icon, if present (optional — Finder shows a generic icon otherwise)
if [ -f "$PKG/$APP_NAME.icns" ]; then
  cp "$PKG/$APP_NAME.icns" "$APP/Contents/Resources/$APP_NAME.icns"
else
  echo "  (no $APP_NAME.icns — shipping with the generic app icon)"
fi

echo "done → $APP"
echo "next: sh scripts/sign-and-notarize.sh   (needs an Apple Developer ID)"
