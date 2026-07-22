#!/usr/bin/env bash
# Render free-tier start command: ONE self-contained process that polls live
# upstreams in-process (SPORTSBOARD_LIVE) into an ephemeral SQLite store it also
# serves from. No separate ingest, no durable warehouse. Used by render.yaml.
set -euo pipefail
export SPORTSBOARD_LIVE=1
export SPORTSDATA_AGENTS_DATABASE_URL="${SPORTSDATA_AGENTS_DATABASE_URL:-sqlite+aiosqlite:////tmp/sportsboard.db}"
exec python -m sportsdata_agents.interfaces.sportsboard   # serves on $PORT, polls in-process
