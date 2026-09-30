-- Track when a sales order email was last sent so the UI can show a
-- persistent "sent" indicator across page reloads.
ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS email_last_sent_at TIMESTAMPTZ;
