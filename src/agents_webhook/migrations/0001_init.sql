-- agents-webhook schema.
-- Writers:
--   endpoints: the api only (create, rotate credentials, change preset/template, delete).
--   events:    a queue table. ingest inserts rows and replaces payloads of `new` rows and
--              bumps deliveries; the worker moves status, only through status-guarded UPDATEs.
--              The api only reads it. The worker is also the single maintenance writer: it
--              deletes final events past the retention window (nulling update_of/duplicate_of
--              references to them).

CREATE TABLE endpoints (
    id                     bigserial PRIMARY KEY,
    suffix                 text NOT NULL UNIQUE CHECK (suffix ~ '^[A-Za-z0-9._~-]{1,128}$'),
    type                   text NOT NULL CHECK (type IN
                               ('grafana-alert', 'gitlab-issue', 'gitlab-mr', 'github-issue',
                                'github-pr')),
    preset_id              text NOT NULL,
    prompt_template        text NULL,
    username               text NOT NULL,
    password_hash          text NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    credentials_rotated_at timestamptz NULL
);

CREATE TABLE events (
    id              bigserial PRIMARY KEY,
    seq             bigserial,
    priority        smallint NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    endpoint_id     bigint NULL REFERENCES endpoints (id) ON DELETE SET NULL,
    endpoint_suffix text NOT NULL,
    type            text NOT NULL,
    source          text NOT NULL CHECK (source IN ('github', 'gitlab', 'grafana')),
    kind            text NOT NULL CHECK (kind IN ('mr', 'issue', 'comment', 'alert')),
    action          text NULL,
    repo            text NULL,
    number          int NULL,
    title           text NULL,
    url             text NULL,
    author          text NULL,
    actor           text NULL,
    labels          jsonb NULL,
    payload         jsonb NOT NULL,
    delivery_id     text NULL,
    dedup_key       text NOT NULL,
    status          text NOT NULL CHECK (status IN
                        ('new', 'in-progress', 'handled', 'ignored', 'failed')),
    reason          text NULL,
    preset_id       text NULL,
    prompt_template text NULL,
    update_of       bigint NULL REFERENCES events (id),
    duplicate_of    bigint NULL REFERENCES events (id),
    deliveries      int NOT NULL DEFAULT 1,
    cluster_id      text NULL,
    cluster_slug    text NULL,
    message_id      text NULL,
    started_at      timestamptz NULL,
    finished_at     timestamptz NULL
);

CREATE UNIQUE INDEX events_new_key ON events (endpoint_id, dedup_key) WHERE status = 'new';
CREATE INDEX events_delivery ON events (endpoint_id, delivery_id);
CREATE INDEX events_latest_per_key ON events (endpoint_id, dedup_key, id DESC);
CREATE INDEX events_queue ON events (status, priority, seq);
CREATE INDEX events_received ON events (received_at DESC);
