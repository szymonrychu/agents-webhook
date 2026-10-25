-- Per-endpoint event filters: {"allow": [...], "deny": [...]}, validated by filters.validate.
-- The api stays the only writer of `endpoints` (create, rotate credentials, replace filters,
-- delete); ingest only reads. Existing rows get empty filters, which allow every event.
ALTER TABLE endpoints ADD COLUMN filters jsonb NOT NULL DEFAULT '{"allow": [], "deny": []}'::jsonb;
