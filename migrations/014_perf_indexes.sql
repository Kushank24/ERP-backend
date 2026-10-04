-- Performance indexes for list pages and dashboard aggregates

-- enquiries: filtered by status on dashboard + list; sorted/joined by date and company
CREATE INDEX IF NOT EXISTS idx_enquiries_status      ON enquiries (status);
CREATE INDEX IF NOT EXISTS idx_enquiries_company_id  ON enquiries (company_id);
CREATE INDEX IF NOT EXISTS idx_enquiries_date_id     ON enquiries (enquiry_date DESC, id DESC);

-- offers: same pattern
CREATE INDEX IF NOT EXISTS idx_offers_status         ON offers (status);
CREATE INDEX IF NOT EXISTS idx_offers_company_id     ON offers (company_id);
CREATE INDEX IF NOT EXISTS idx_offers_date_id        ON offers (offer_date DESC, id DESC);

-- sales_order_items: FK lookup on every sales-order detail/edit load
CREATE INDEX IF NOT EXISTS idx_so_items_order_id     ON sales_order_items (sales_order_id);

-- email_campaigns: list page sorts by id DESC; status filter for active check
CREATE INDEX IF NOT EXISTS idx_email_campaigns_status ON email_campaigns (status);
