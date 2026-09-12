-- Track which items in a partially-accepted offer were accepted
-- Defaults to TRUE so existing fully-accepted offers are unaffected
ALTER TABLE offer_items
    ADD COLUMN IF NOT EXISTS accepted BOOLEAN NOT NULL DEFAULT TRUE;

-- Add 'partial' to the offer_status enum
-- (ALTER TYPE … ADD VALUE cannot run inside a transaction block)
ALTER TYPE offer_status ADD VALUE IF NOT EXISTS 'partial' AFTER 'accepted';
