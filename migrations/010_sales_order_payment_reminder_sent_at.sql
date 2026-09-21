-- Idempotency guard for the internal "unpaid sales order, exactly 10 days
-- old" alert (app/jobs/ops_digest.py), mirroring offers.reminder_sent_at
-- (migration 009). Same reasoning: the alert matches on an EXACT day
-- (sales_date = today - 10 days), which by itself makes each order eligible
-- on only one calendar day — this column is a same-day double-fire guard
-- and audit trail on top of that, not the primary dedup mechanism.
--
-- Not used by the other two ops_digest sections (open work orders, upcoming
-- deliveries) — those are deliberately recurring daily digests, not
-- one-time alerts, so they have no "already notified" state to track.
ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS payment_reminder_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_sales_orders_payment_reminder_sent_at
    ON sales_orders (payment_reminder_sent_at);
