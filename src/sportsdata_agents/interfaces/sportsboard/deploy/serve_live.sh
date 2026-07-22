#!/usr/bin/env bash
# Put the sports board LIVE for FREE. Runs ingestion + the board backend locally
# and exposes them through a Cloudflare quick tunnel (no account, no domain).
# Polling happens from YOUR IP — which the books trust — so unlike a cloud host
# nothing gets geo/rate-blocked. Ctrl+C stops everything.
#
#   bash serve_live.sh
#
# Prints a public tunnel URL that:
#   • serves the full live board UI directly, and
#   • can back the public page:  https://sportsdata-ai.com/sports/?api=<tunnel>
#
# Self-contained model: ONE process polls live upstreams in-process (SPORTSBOARD_LIVE)
# into a throwaway SQLite store it also serves from — no separate ingest, no durable
# warehouse. The money-flow window fills over the first few minutes, like the racing
# board on a cold start.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../../.." && pwd)"   # → repo root (src/…/deploy → up 5)
cd "$ROOT"

PORT="${SPORTSBOARD_PORT:-8792}"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
PAGES_URL="https://sportsdata-ai.com/sports"
STORE="$(mktemp -t sportsboard-XXXX.db)"
TUN_LOG="$(mktemp)"

command -v cloudflared >/dev/null || { echo "cloudflared not found → brew install cloudflared"; exit 1; }

cleanup() { echo; echo "stopping…"; kill "${BACK_PID:-}" "${TUN_PID:-}" 2>/dev/null || true; rm -f "$TUN_LOG" "$STORE"; }
trap cleanup EXIT INT TERM

echo "▶ starting self-contained live board on :$PORT (polls from your IP) …"
# ENGINE_BACKEND=local gives the SGM button CORRELATED prices from the local
# engines package (falls back to the independent product if it isn't installed).
PORT="$PORT" SPORTSBOARD_LIVE=1 \
  SPORTSDATA_AGENTS_ENGINE_BACKEND="${SPORTSDATA_AGENTS_ENGINE_BACKEND:-local}" \
  SPORTSDATA_AGENTS_DATABASE_URL="sqlite+aiosqlite:///$STORE" \
  "$PY" -m sportsdata_agents.interfaces.sportsboard > "$ROOT/sportsboard.log" 2>&1 &
BACK_PID=$!

for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
  [ "$i" = 60 ] && { echo "backend didn't come up — see sportsboard.log"; exit 1; }
done
echo "✓ board up & priming live upstreams (money-flow window builds over the next few minutes)"

echo "▶ opening Cloudflare tunnel …"
cloudflared tunnel --url "http://localhost:$PORT" > "$TUN_LOG" 2>&1 &
TUN_PID=$!

for i in $(seq 1 30); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUN_LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -z "${URL:-}" ] && { echo "tunnel URL not found — see $TUN_LOG"; exit 1; }

echo
echo "  LIVE board (direct):  $URL"
echo "  Public page, live:    $PAGES_URL/?api=$URL"
echo
echo "Ctrl+C to stop."
wait
