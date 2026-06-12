#!/bin/bash
# sportsdata.app launcher (P4 M4.3, "daemon + browser UI" model).
#
# This is the .app's main executable. It starts the local daemon (`sportsdata
# app` — gateway + conductor in one process) and opens the chat UI in the
# default browser. The daemon is a CHILD of this script, so quitting the app
# (or the script exiting) tears the daemon down with it.
#
# Everything runs on the user's machine: their compute, their warehouse, their
# BYO model key. No network egress beyond model-API calls and the user's own
# Slack/Discord.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"                 # …/Contents/MacOS
APP_ROOT="$(cd "$HERE/.." && pwd)"                    # …/Contents
BIN="$APP_ROOT/Resources/sportsdata/sportsdata"       # the bundled onedir binary
PORT="${SPORTSDATA_PORT:-8765}"
URL="http://127.0.0.1:${PORT}/"
LOG_DIR="${HOME}/Library/Logs"
LOG="${LOG_DIR}/sportsdata-app.log"
mkdir -p "$LOG_DIR"

if [ ! -x "$BIN" ]; then
  osascript -e 'display alert "sportsdata" message "The app bundle is incomplete (missing runtime). Re-download and reinstall."' >/dev/null 2>&1
  exit 1
fi

# First run with no model key configured → open Terminal on the setup wizard,
# which is interactive and can't run inside the .app. The daemon still starts;
# the chat UI shows a configure-a-key hint until a key is stored.
if ! "$BIN" setup --check >/dev/null 2>&1; then
  osascript -e "tell application \"Terminal\" to do script \"'$BIN' setup\"" >/dev/null 2>&1 || true
fi

# Start the daemon; tear it down when this launcher exits (app quit / logout).
"$BIN" app --port "$PORT" >>"$LOG" 2>&1 &
DAEMON=$!
trap 'kill "$DAEMON" 2>/dev/null' EXIT INT TERM

# Wait (≤30s) for the daemon to answer health, then open the UI.
i=0
while [ "$i" -lt 60 ]; do
  if curl -fs "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then break; fi
  i=$((i + 1))
  sleep 0.5
done
open "$URL" >/dev/null 2>&1 || true

wait "$DAEMON"
