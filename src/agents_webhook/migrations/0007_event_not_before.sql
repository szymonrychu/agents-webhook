-- A `new` event is claimable only from `not_before` on; NULL means at once. The worker sets it
-- when the orchestrator's usage gate refuses a session until the quota window resets.
ALTER TABLE events ADD COLUMN IF NOT EXISTS not_before timestamptz;
