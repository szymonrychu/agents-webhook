-- The event text the prompt needs, so the worker rebuilds the normalised event from the row
-- (the provider headers normalize() reads are not stored). Latest wins replaces both.
ALTER TABLE events ADD COLUMN body text NULL;
ALTER TABLE events ADD COLUMN comment_body text NULL;
