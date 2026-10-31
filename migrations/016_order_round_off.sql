-- Round-off on the grand total of sales orders and purchase orders.
--
-- Two columns per table rather than one:
--   round_off_mode  the user's intent ('none' | 'down' | 'up'), which survives
--                   any later edit to the lines, extra costs or GST rate
--   round_off       the signed delta currently applied to the grand total,
--                   always in (-1, 1) and re-derived from the mode whenever
--                   the base total changes
-- Keeping only the delta would lose the intent: once a base happens to land on
-- a whole rupee the delta is 0 for both directions, and a later edit could not
-- tell which way the user had rounded.
--
-- total_amount already stores the grand total, so it now includes round_off.

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS round_off DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS round_off_mode TEXT NOT NULL DEFAULT 'none';

ALTER TABLE purchase_orders
    ADD COLUMN IF NOT EXISTS round_off DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS round_off_mode TEXT NOT NULL DEFAULT 'none';

ALTER TABLE sales_orders
    DROP CONSTRAINT IF EXISTS sales_orders_round_off_mode_check;
ALTER TABLE sales_orders
    ADD CONSTRAINT sales_orders_round_off_mode_check
    CHECK (round_off_mode IN ('none', 'down', 'up'));

ALTER TABLE purchase_orders
    DROP CONSTRAINT IF EXISTS purchase_orders_round_off_mode_check;
ALTER TABLE purchase_orders
    ADD CONSTRAINT purchase_orders_round_off_mode_check
    CHECK (round_off_mode IN ('none', 'down', 'up'));
