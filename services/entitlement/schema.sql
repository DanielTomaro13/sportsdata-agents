-- sportsdata entitlement service — D1 schema (Phase 1).
-- One customer ↔ one Stripe customer ↔ one licence key ↔ one entitlement row.

CREATE TABLE IF NOT EXISTS customers (
  id                  TEXT PRIMARY KEY,        -- our id (== licence key, "sd_live_…")
  stripe_customer_id  TEXT UNIQUE,
  email               TEXT,
  created_at          INTEGER NOT NULL         -- unix seconds
);
CREATE INDEX IF NOT EXISTS idx_customers_stripe ON customers(stripe_customer_id);

CREATE TABLE IF NOT EXISTS entitlements (
  customer_id      TEXT PRIMARY KEY REFERENCES customers(id),
  status           TEXT NOT NULL DEFAULT 'inactive',  -- active | past_due | canceled | inactive
  sport_slots      INTEGER NOT NULL DEFAULT 0,        -- 5 included in Base + extras
  gambling_slots   INTEGER NOT NULL DEFAULT 0,
  all_access       INTEGER NOT NULL DEFAULT 0,        -- 0/1
  groups           TEXT NOT NULL DEFAULT '[]',        -- JSON array: the assigned feed groups
  current_period_end INTEGER,                          -- unix seconds (entitlement expiry anchor)
  emailed_at       INTEGER,                            -- when the fulfilment email was sent (idempotency)
  last_event_at    INTEGER,                            -- Stripe event.created of the last applied event (out-of-order guard)
  updated_at       INTEGER NOT NULL
);
-- Existing DBs: run once to add the column —
--   ALTER TABLE entitlements ADD COLUMN last_event_at INTEGER;

-- Audit of Stripe events we've processed (idempotency + debugging).
CREATE TABLE IF NOT EXISTS stripe_events (
  id          TEXT PRIMARY KEY,   -- Stripe event id (evt_…) — dedupes redelivery
  type        TEXT,
  received_at INTEGER NOT NULL
);
