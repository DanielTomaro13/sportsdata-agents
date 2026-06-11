#!/bin/sh
# Swap the per-job sportsdata cron lines for the ONE conductor line.
# `agents schedule --cron 60` then runs everything: ingest with event-proximity
# pacing, the monitor, nightly resolve+results, weekly steward/eval/site/books/
# health — and hands persistent failures to the incident_triage ops agent.
#
# Run from the repo root:  sh scripts/install-scheduler-cron.sh
# (macOS may ask once for permission to administer cron — approve it.)
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DB='SPORTSDATA_AGENTS_DATABASE_URL="sqlite+aiosqlite:////tmp/agents-warehouse.db"'
LINE="* * * * * cd $REPO && $DB .venv/bin/agents schedule --cron 60 >> /tmp/agents-scheduler.log 2>&1 # sportsdata-agents-cron"

TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "sportsdata-agents-cron" > "$TMP" || true
{
  echo "# sportsdata-agents-cron: ONE conductor line — agents schedule runs everything"
  echo "$LINE"
} >> "$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "installed — the conductor is the only sportsdata cron entry:"
crontab -l | grep "sportsdata-agents-cron"
