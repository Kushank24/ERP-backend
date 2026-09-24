-- Originally an idempotency guard for an internal "unpaid sales order,
-- exactly 10 days old" alert (app/jobs/ops_digest.py), mirroring
-- offers.reminder_sent_at (migration 009). That section was later changed to
-- a standing list of all currently-unpaid orders (payment_status IN (1,2)),
-- like the open-work-orders section, so this column is UNUSED — no code
-- reads or writes it. Left in place rather than dropped since dropping a
-- column is the kind of change you don't want to redo casually; safe to
-- remove in a future migration if this stays unused.
ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS payment_reminder_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_sales_orders_payment_reminder_sent_at
    ON sales_orders (payment_reminder_sent_at);
