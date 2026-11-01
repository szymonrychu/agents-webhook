"""Test helpers: build auth records and authenticate through the production path."""

import secrets

from agents_webhook.db import endpoints
from agents_webhook.ingest import auth
from agents_webhook.secrets_box import KEY_BYTES, SecretBox

BOX = SecretBox(secrets.token_bytes(KEY_BYTES))


async def authenticate(conn, suffix, authorization, *, box=BOX, headers=None, body=None):
    """endpoints.load_for_auth plus ingest.auth.authenticate, as ingest runs them."""
    merged = dict(headers or {})
    if authorization is not None:
        merged["Authorization"] = authorization
    record = await endpoints.load_for_auth(conn, suffix)
    request = auth.AuthRequest(merged, body)
    return await auth.authenticate(record, request, box=box, replay_seconds=300)


async def make_secret_endpoint(
    conn,
    *,
    mode,
    auth_header=None,
    hmac_prefix=None,
    timestamp_header=None,
    type_="github-pr",
    suffix=None,
    box=BOX,
):
    """Insert a non-basic endpoint directly; returns (Endpoint, plaintext secret).

    The id is the AAD, so the row is inserted first and its secret set right after.
    """
    from agents_webhook import credentials

    secret = credentials.generate_secret()
    suffix = suffix or credentials.generate_suffix()
    algo = "sha256" if mode == "hmac" else None
    cur = await conn.execute(
        "INSERT INTO endpoints (suffix, type, preset_id, auth_mode, auth_header, hmac_algorithm, "
        "hmac_prefix, hmac_timestamp_header, secret_ct, secret_kid) "
        "VALUES (%s, %s, 'p1', %s, %s, %s, %s, %s, 'x', 'x') RETURNING id",
        (suffix, type_, mode, auth_header, algo, hmac_prefix, timestamp_header),
    )
    endpoint_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE endpoints SET secret_ct = %s, secret_kid = %s WHERE id = %s",
        (box.encrypt(secret, aad=str(endpoint_id).encode()), box.kid, endpoint_id),
    )
    return await endpoints.get(conn, endpoint_id), secret
