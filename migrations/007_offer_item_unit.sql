-- Add per-item unit to offer_items (PC / SET / MTR)
ALTER TABLE offer_items
    ADD COLUMN IF NOT EXISTS unit VARCHAR(10) NOT NULL DEFAULT 'PC';
