-- The template context the worker renders: `draft` and `changed` of the normalised event, so
-- the prompt matches the UI preview. Defaults keep rows stored before this valid.
ALTER TABLE events ADD COLUMN draft boolean NOT NULL DEFAULT false;
ALTER TABLE events ADD COLUMN changed jsonb NOT NULL DEFAULT '[]'::jsonb;
