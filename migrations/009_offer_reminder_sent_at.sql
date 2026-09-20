-- Tracks whether the automated 15-day follow-up reminder has been sent for
-- an offer, so:
--   1. a cron re-run on the same day (manual retrigger, platform double-fire)
--      cannot send the same offer twice, and
--   2. there is an auditable answer to "did we email this company about
--      this offer, and when" without grepping logs.
--
-- The reminder job (app/jobs/offer_reminders.py) matches offers whose
-- offer_date is EXACTLY 15 days ago — not "15 or more" — by explicit design
-- decision, to avoid ever mass-emailing the large backlog of older open
-- offers. This column does not change that; it only guards against sending
-- the same day's cohort more than once.
ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_offers_reminder_sent_at ON offers (reminder_sent_at);
