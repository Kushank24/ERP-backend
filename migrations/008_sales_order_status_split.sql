-- Split the overloaded sales_orders.status column.
--
-- PATCH /sales-orders/{id}/payment wrote payment state (1=not received,
-- 2=partial, 3=received) into `status`, while POST /sales-orders/{id}/dispatch
-- wrote dispatch state (3=partial, 4=full) into the SAME column. Recording a
-- dispatch destroyed the payment state and vice-versa.
--
-- After this migration:
--   payment_status  1=not received | 2=partial | 3=received   (NULL = unknown)
--   dispatch_status 1=not dispatched | 3=partial | 4=full
--   status          LEGACY. Kept and still mirrored from payment_status so any
--                   reader we have not migrated keeps working. Only the payment
--                   endpoint writes it now, so the collision is gone. Do not
--                   add new readers.

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS payment_status  INTEGER,
    ADD COLUMN IF NOT EXISTS dispatch_status INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------------
-- Backfill payment_status
-- ---------------------------------------------------------------------------
-- Only 1/2/3 were ever written by the payment endpoint, and the frontend has
-- always rendered `status` as payment state, so those map across directly.
--
-- status = 6 is a legacy value that no current code path produces (713 rows,
-- all created 2026-08-27 by a bulk import). It carries no payment meaning, so
-- it is left NULL rather than asserting "not received" for 713 real orders.
-- NULL renders as "no selection" in the UI, which is exactly how 6 behaves
-- today — so this is not a UX change.
UPDATE sales_orders
   SET payment_status = status
 WHERE status IN (1, 2, 3)
   AND payment_status IS NULL;

-- ---------------------------------------------------------------------------
-- Backfill dispatch_status from the line items
-- ---------------------------------------------------------------------------
-- sales_order_items.dispatched_qty is the real record of what shipped, so
-- derive from it rather than guessing from the conflated status value.
WITH agg AS (
    SELECT sales_order_id,
           COUNT(*)                                               AS line_count,
           COUNT(*) FILTER (WHERE dispatched_qty >= quantity_sold) AS full_lines,
           COUNT(*) FILTER (WHERE dispatched_qty > 0)              AS started_lines
      FROM sales_order_items
     GROUP BY sales_order_id
)
UPDATE sales_orders so
   SET dispatch_status = CASE
           WHEN a.line_count > 0 AND a.full_lines = a.line_count THEN 4
           WHEN a.started_lines > 0                              THEN 3
           ELSE 1
       END
  FROM agg a
 WHERE a.sales_order_id = so.id;

-- ---------------------------------------------------------------------------
-- payment_received existed since the initial schema but was never written by
-- any endpoint, so it is FALSE on all 838 rows. Make it consistent.
-- ---------------------------------------------------------------------------
UPDATE sales_orders
   SET payment_received = (payment_status = 3)
 WHERE payment_status IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Constrain both columns so a future bug cannot write an out-of-range code
-- the way `status` accumulated a 6.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_payment_status_chk'
    ) THEN
        ALTER TABLE sales_orders
            ADD CONSTRAINT sales_orders_payment_status_chk
            CHECK (payment_status IS NULL OR payment_status IN (1, 2, 3));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_dispatch_status_chk'
    ) THEN
        ALTER TABLE sales_orders
            ADD CONSTRAINT sales_orders_dispatch_status_chk
            CHECK (dispatch_status IN (1, 3, 4));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_sales_orders_payment_status  ON sales_orders (payment_status);
CREATE INDEX IF NOT EXISTS idx_sales_orders_dispatch_status ON sales_orders (dispatch_status);
