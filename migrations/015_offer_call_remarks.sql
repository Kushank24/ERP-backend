-- Add call_remarks to store notes from follow-up calls
ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS call_remarks TEXT;
