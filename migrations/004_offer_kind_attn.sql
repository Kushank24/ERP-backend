-- Add per-offer "Kind Attn" field (overrides company contact_person in PDF)
ALTER TABLE offers ADD COLUMN IF NOT EXISTS kind_attn VARCHAR(200);
