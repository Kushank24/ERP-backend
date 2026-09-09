-- Add call-status tracking to offers
-- call_status: whether the follow-up call was made
-- called_at: timestamp of when the checkbox was checked
ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS call_status  BOOLEAN   NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS called_at    TIMESTAMPTZ;
