-- Per-endpoint auth mode. Existing rows become `basic` and stay valid unchanged (username +
-- scrypt hash). The other modes keep their secret AES-GCM encrypted (secret_ct = nonce ||
-- ciphertext || tag, secret_kid = key fingerprint) and have neither username nor hash.
-- Writer: the api only, as before; ingest only reads.
ALTER TABLE endpoints
    ADD COLUMN auth_mode text NOT NULL DEFAULT 'basic'
        CHECK (auth_mode IN ('basic', 'header', 'bearer', 'hmac')),
    ADD COLUMN auth_header text NULL,
    ADD COLUMN hmac_algorithm text NULL CHECK (hmac_algorithm = 'sha256'),
    ADD COLUMN hmac_prefix text NULL,
    ADD COLUMN hmac_timestamp_header text NULL,
    ADD COLUMN secret_ct bytea NULL,
    ADD COLUMN secret_kid text NULL,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE endpoints
    ALTER COLUMN username DROP NOT NULL,
    ALTER COLUMN password_hash DROP NOT NULL;

ALTER TABLE endpoints ADD CONSTRAINT endpoints_auth_columns CHECK (
    CASE auth_mode
        WHEN 'basic' THEN
            username IS NOT NULL AND password_hash IS NOT NULL
            AND secret_ct IS NULL AND secret_kid IS NULL
            AND auth_header IS NULL AND hmac_algorithm IS NULL
            AND hmac_prefix IS NULL AND hmac_timestamp_header IS NULL
        WHEN 'header' THEN
            username IS NULL AND password_hash IS NULL
            AND secret_ct IS NOT NULL AND secret_kid IS NOT NULL
            AND auth_header IS NOT NULL AND hmac_algorithm IS NULL
            AND hmac_prefix IS NULL AND hmac_timestamp_header IS NULL
        WHEN 'bearer' THEN
            username IS NULL AND password_hash IS NULL
            AND secret_ct IS NOT NULL AND secret_kid IS NOT NULL
            AND auth_header IS NULL AND hmac_algorithm IS NULL
            AND hmac_prefix IS NULL AND hmac_timestamp_header IS NULL
        WHEN 'hmac' THEN
            username IS NULL AND password_hash IS NULL
            AND secret_ct IS NOT NULL AND secret_kid IS NOT NULL
            AND auth_header IS NOT NULL AND hmac_algorithm IS NOT NULL
    END
);
