#!/bin/bash
# sportsdata.app launcher ("native window" model, P4).
#
# This is the .app's main executable. It launches the app in its OWN native
# window (`sportsdata desktop` — gateway + conductor behind an OS web view, no
# browser). Closing the window quits the app; everything runs on the user's
# machine (their compute, their warehouse, their BYO model key).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"                 # …/Contents/MacOS
APP_ROOT="$(cd "$HERE/.." && pwd)"                    # …/Contents
BIN="$APP_ROOT/Resources/sportsdata/sportsdata"       # the bundled onedir binary
LOG_DIR="${HOME}/Library/Logs"
LOG="${LOG_DIR}/sportsdata-app.log"
mkdir -p "$LOG_DIR"

if [ ! -x "$BIN" ]; then
  osascript -e 'display alert "sportsdata" message "The app bundle is incomplete (missing runtime). Re-download and reinstall."' >/dev/null 2>&1
  exit 1
fi

# Has a model key already? Cap the probe at 8s so a stray macOS keychain prompt can
# never HANG the launcher (the key store is an app-private file first, so the normal
# path returns instantly without touching the keychain).
"$BIN" setup --check >/dev/null 2>&1 &
CHECK_PID=$!
( sleep 8 && kill -9 "$CHECK_PID" 2>/dev/null ) &
WATCH_PID=$!
if wait "$CHECK_PID" 2>/dev/null; then HAS_KEY=1; else HAS_KEY=0; fi
kill "$WATCH_PID" 2>/dev/null || true

# First run (or no key) → run the interactive setup wizard in Terminal (it can't run
# inside the app window), then stop. The user pastes their key and re-opens the app.
if [ "$HAS_KEY" != "1" ]; then
  osascript -e "tell application \"Terminal\" to do script \"'$BIN' setup\"" >/dev/null 2>&1 || true
  osascript -e 'display dialog "Finish setup in the Terminal window (paste your model API key), then re-open sportsdata." buttons {"OK"} with title "sportsdata — first run" default button "OK"' >/dev/null 2>&1 || true
  exit 0
fi

# Launch the native window; it owns the app lifecycle (blocks until closed).
exec "$BIN" desktop >>"$LOG" 2>&1
