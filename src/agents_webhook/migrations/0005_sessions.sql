-- The orchestrator's unit is a session (it was a cluster): the run columns follow. Renames
-- only, so every row keeps its values; no index or constraint names either column.
ALTER TABLE events RENAME COLUMN cluster_id TO session_id;
ALTER TABLE events RENAME COLUMN cluster_slug TO session_slug;
