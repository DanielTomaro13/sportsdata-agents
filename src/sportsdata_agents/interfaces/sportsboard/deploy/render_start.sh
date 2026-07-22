#!/usr/bin/env bash
# Render free-tier start command: ingestion loop + board backend in one service,
# reading/writing an ephemeral local SQLite warehouse. Used by render.yaml.
set -euo pipefail
export SPORTSDATA_AGENTS_DATABASE_URL="${SPORTSDATA_AGENTS_DATABASE_URL:-sqlite+aiosqlite:////tmp/sportsboard.db}"

agents ingest --loop > /tmp/ingest.log 2>&1 &   # fill the warehouse in the background
exec python -m sportsdata_agents.interfaces.sportsboard   # serve on $PORT
