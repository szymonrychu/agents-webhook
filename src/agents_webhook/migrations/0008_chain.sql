-- Chaining: an endpoint lists agents-schedule schedules (ids only, no FK: another service's
-- database) that get one chained run each once the session of a handled event has finished.
-- An event snapshots the list at insert; chain_status tracks the one-shot fan-out and
-- result_text is the final text of the turn, captured at the handled transition.
ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS then_schedule_ids uuid[] NOT NULL DEFAULT '{}';
ALTER TABLE events ADD COLUMN IF NOT EXISTS then_schedule_ids uuid[];
ALTER TABLE events ADD COLUMN IF NOT EXISTS chain_status text
    CHECK (chain_status IN ('pending', 'fired', 'skipped', 'expired'));
ALTER TABLE events ADD COLUMN IF NOT EXISTS result_text text;
CREATE INDEX IF NOT EXISTS events_chain_pending_idx ON events (received_at)
    WHERE chain_status = 'pending';
