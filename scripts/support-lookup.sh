#!/bin/sh
# Operator support lookup against the live D1 entitlement database.
#
#   sh scripts/support-lookup.sh customer@example.com     # by email
#   sh scripts/support-lookup.sh cus_XXXX                 # by Stripe customer id
#   sh scripts/support-lookup.sh --recent                 # last 10 customers
#
# Read-only. Keys are stored only as SHA-256 hashes, so nothing here can leak a
# licence. Needs wrangler authed on the account (same login that deploys).
set -eu

DB="sportsdata-entitlement"
DIR="$(cd "$(dirname "$0")/../services/entitlement" && pwd)"
q() { (cd "$DIR" && npx wrangler d1 execute "$DB" --remote --command "$1"); }

if [ "${1:-}" = "--recent" ]; then
  q "SELECT c.email, c.stripe_customer_id, e.status, e.sport_slots, e.gambling_slots,
            e.all_access, e.groups, datetime(c.created_at,'unixepoch') AS created
     FROM customers c LEFT JOIN entitlements e ON e.customer_id = c.id
     ORDER BY c.created_at DESC LIMIT 10;"
  exit 0
fi

[ -n "${1:-}" ] || { echo "usage: $0 <email | cus_…> | --recent" >&2; exit 2; }
# single-quote-escape the needle so an odd email can't break out of the literal
NEEDLE="$(printf "%s" "$1" | sed "s/'/''/g")"
q "SELECT c.email, c.stripe_customer_id, e.status, e.sport_slots, e.gambling_slots,
          e.all_access, e.groups,
          datetime(e.current_period_end,'unixepoch') AS period_end,
          datetime(e.emailed_at,'unixepoch')        AS fulfilment_email,
          datetime(e.updated_at,'unixepoch')        AS updated
   FROM customers c LEFT JOIN entitlements e ON e.customer_id = c.id
   WHERE c.email = '$NEEDLE' OR c.stripe_customer_id = '$NEEDLE';"
