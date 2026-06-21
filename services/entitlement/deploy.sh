#!/bin/sh
# Guided one-command deploy of the entitlement Worker. Runs every non-secret step and
# pauses for YOU to paste each secret value (they go straight into your Cloudflare
# account — never to a file, never to git). Run it from this directory:
#
#   sh deploy.sh
#
# Prereqs: Node + npm, a Cloudflare account. Stripe products already created
# (scripts/setup-stripe.py). You stay in control of every secret.
set -eu
cd "$(dirname "$0")"

say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ask() { printf '%s [y/N] ' "$1"; read -r a; [ "$a" = y ] || [ "$a" = Y ]; }

command -v node >/dev/null 2>&1 || { echo "Install Node first (https://nodejs.org)"; exit 1; }

say "1/7  npm install"
npm install

say "2/7  Cloudflare login check"
if ! npx wrangler whoami >/dev/null 2>&1; then
  echo "You're not logged in. Run:  npx wrangler login   then re-run this script."
  exit 1
fi

say "3/7  D1 database"
if grep -q REPLACE_WITH_D1_DATABASE_ID wrangler.jsonc; then
  npx wrangler d1 create sportsdata-entitlement >/dev/null 2>&1 || echo "  (database may already exist — continuing)"
  ID=$(npx wrangler d1 list --json 2>/dev/null \
    | python3 -c "import sys,json;print(next((d['uuid'] for d in json.load(sys.stdin) if d.get('name')=='sportsdata-entitlement'),''))" 2>/dev/null || true)
  if [ -z "${ID:-}" ]; then
    echo "  Couldn't read the database_id automatically. Run 'npx wrangler d1 create sportsdata-entitlement',"
    echo "  paste the printed database_id into wrangler.jsonc, then re-run this script."
    exit 1
  fi
  sed -i.bak "s/REPLACE_WITH_D1_DATABASE_ID/$ID/" wrangler.jsonc && rm -f wrangler.jsonc.bak
  echo "  database_id = $ID  (written to wrangler.jsonc)"
else
  echo "  already configured in wrangler.jsonc — skipping create"
fi

say "4/7  Apply the schema (remote D1)"
npx wrangler d1 execute sportsdata-entitlement --file=schema.sql --remote

say "5/7  Signing keypair"
# Any python3 with `cryptography` works; override with PY=… if you keep it elsewhere.
PY="${PY:-python3}"
if ! "$PY" -c "import cryptography" 2>/dev/null; then
  echo "  error: \$PY ($PY) needs the 'cryptography' package — set PY=/path/to/python or pip install cryptography" >&2
  exit 1
fi
echo "  Generating an Ed25519 keypair (printed to THIS terminal only):"
echo "  ----------------------------------------------------------------"
"$PY" gen-keypair.py
echo "  ----------------------------------------------------------------"
echo "  → Copy the PUBLIC line and keep it (send it to Claude with the URL)."
echo "  → At the next prompt, paste the PRIVATE line."
npx wrangler secret put SIGNING_KEY_PKCS8_B64

say "6/7  Other secrets"
echo "  Stripe secret key (sk_live_… or sk_test_…):"
npx wrangler secret put STRIPE_SECRET_KEY
ask "  Set RESEND_API_KEY now (turns on the auto fulfilment email)?" && npx wrangler secret put RESEND_API_KEY || true
ask "  Selling DataGolf (set DATAGOLF_KEY for the proxy)?"            && npx wrangler secret put DATAGOLF_KEY    || true

say "7/7  Deploy"
npx wrangler deploy

URL=$(npx wrangler deployments list 2>/dev/null | sed -n 's#.*\(https://[a-z0-9.-]*workers\.dev\).*#\1#p' | head -1 || true)
say "Deployed. ${URL:+URL: $URL}"
cat <<EOF

Still to do (these need your dashboards):
  • Stripe → Developers → Webhooks → Add endpoint:
      URL    = <your-worker-url>/stripe/webhook
      events = customer.subscription.created / .updated / .deleted
    then:  npx wrangler secret put STRIPE_WEBHOOK_SECRET   (paste the whsec_… signing secret)

  • Send Claude:  the PUBLIC keypair line  +  your worker URL
    → Claude bakes them into the MCP and wires site/entitlement.json.

Quick check:  curl <your-worker-url>/healthz   → {"ok":true}
EOF
