"""Webhook endpoints: a unique URL suffix, a type, a preset and basic-auth credentials.

The api is the only writer of `endpoints`; ingest only reads it through `authenticate`.
Only a salted scrypt hash of a password is stored, and `Endpoint` never carries it.
"""

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from agents_webhook import credentials, filters

TYPES = ("grafana-alert", "gitlab-issue", "gitlab-mr", "github-issue", "github-pr")
SUFFIX_RE = re.compile(r"[A-Za-z0-9._~-]{1,128}")
USERNAME_MAX = 64

_COLUMNS = (
    "id, suffix, type, preset_id, prompt_template, username, created_at, "
    "credentials_rotated_at, filters, auth_mode, auth_header, hmac_algorithm, hmac_prefix, "
    "hmac_timestamp_header, updated_at, secret_ct IS NOT NULL AS has_secret"
)


@dataclass(frozen=True)
class Endpoint:
    id: int
    suffix: str
    type: str
    preset_id: str
    prompt_template: str | None
    username: str | None
    created_at: datetime
    credentials_rotated_at: datetime | None
    filters: dict
    auth_mode: str
    auth_header: str | None
    hmac_algorithm: str | None
    hmac_prefix: str | None
    hmac_timestamp_header: str | None
    updated_at: datetime
    has_secret: bool


@dataclass(frozen=True)
class EndpointIn:
    type: str
    preset_id: str
    suffix: str | None = None
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    prompt_template: str | None = None
    filters: Any = None


@dataclass(frozen=True)
class Credentials:
    """Plaintext credentials, returned once by create and rotate; never stored."""

    username: str
    password: str = field(repr=False)


class SuffixTaken(Exception):
    """Another endpoint already has this suffix."""

    def __init__(self, suffix: str) -> None:
        super().__init__(f"suffix: already taken: {suffix!r}")
        self.suffix = suffix


def _check_suffix(suffix: str) -> str:
    if not SUFFIX_RE.fullmatch(suffix):
        raise ValueError("suffix: must match ^[A-Za-z0-9._~-]{1,128}$")
    return suffix


def _check_username(username: str) -> str:
    if not 1 <= len(username) <= USERNAME_MAX:
        raise ValueError(f"username: must be 1-{USERNAME_MAX} characters")
    if ":" in username or not username.isprintable():
        raise ValueError("username: must be printable characters without ':'")
    return username


def _check_password(password: str, min_length: int) -> str:
    if len(password) < min_length:
        raise ValueError(f"password: must be at least {min_length} characters")
    return password


def _credentials(
    username: str | None, password: str | None, min_password_length: int
) -> Credentials:
    return Credentials(
        username=credentials.generate_username() if username is None else _check_username(username),
        password=credentials.generate_password()
        if password is None
        else _check_password(password, min_password_length),
    )


async def _hash(password: str) -> str:
    # scrypt costs tens of milliseconds; keep it off the event loop.
    return await asyncio.to_thread(credentials.hash_password, password)


async def create(
    conn: psycopg.AsyncConnection, data: EndpointIn, *, min_password_length: int
) -> tuple[Endpoint, Credentials]:
    """Insert an endpoint; a missing suffix, username or password is generated.

    Raises ValueError naming the invalid field, or SuffixTaken.
    """
    if data.type not in TYPES:
        raise ValueError(f"type: must be one of {', '.join(TYPES)}")
    if not data.preset_id:
        raise ValueError("preset_id: required")
    canonical = filters.EMPTY if data.filters is None else filters.validate(data.type, data.filters)
    suffix = credentials.generate_suffix() if data.suffix is None else _check_suffix(data.suffix)
    creds = _credentials(data.username, data.password, min_password_length)
    password_hash = await _hash(creds.password)
    try:
        async with conn.transaction():
            cur = conn.cursor(row_factory=class_row(Endpoint))
            await cur.execute(
                "INSERT INTO endpoints (suffix, type, preset_id, prompt_template, username, "
                f"password_hash, filters) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                (
                    suffix,
                    data.type,
                    data.preset_id,
                    data.prompt_template,
                    creds.username,
                    password_hash,
                    Jsonb(canonical),
                ),
            )
            endpoint = await cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise SuffixTaken(suffix) from exc
    assert endpoint is not None
    return endpoint, creds


async def rotate(
    conn: psycopg.AsyncConnection,
    id: int,
    *,
    username: str | None = None,
    password: str | None = None,
    min_password_length: int,
) -> Credentials | None:
    """Replace an endpoint's credentials (missing ones generated); None if it does not exist."""
    creds = _credentials(username, password, min_password_length)
    password_hash = await _hash(creds.password)
    cur = await conn.execute(
        "UPDATE endpoints SET username = %s, password_hash = %s, credentials_rotated_at = now() "
        "WHERE id = %s RETURNING id",
        (creds.username, password_hash, id),
    )
    return creds if await cur.fetchone() else None


async def set_filters(conn: psycopg.AsyncConnection, id: int, data: Any) -> Endpoint | None:
    """Replace an endpoint's filters; None if it does not exist.

    `data` is validated against the endpoint's own type; a ValueError writes nothing.
    """
    async with conn.transaction():
        cur = await conn.execute("SELECT type FROM endpoints WHERE id = %s FOR UPDATE", (id,))
        row = await cur.fetchone()
        if row is None:
            return None
        canonical = filters.validate(row[0], data)
        cur = conn.cursor(row_factory=class_row(Endpoint))
        await cur.execute(
            f"UPDATE endpoints SET filters = %s WHERE id = %s RETURNING {_COLUMNS}",
            (Jsonb(canonical), id),
        )
        return await cur.fetchone()


def _digest(value: str) -> bytes:
    # Fixed-length digests, so the constant-time compare does not leak the username length.
    return hashlib.sha256(value.encode()).digest()


async def authenticate(
    conn: psycopg.AsyncConnection, suffix: str, authorization: str | None
) -> Endpoint | None:
    """The endpoint when `authorization` carries its basic-auth credentials, else None.

    Every failure (unknown suffix, malformed or missing header, wrong username or password)
    takes the same path: one username compare and one scrypt verify, against `DUMMY_HASH`
    when there is no endpoint or no parsable header, so failures cannot be told apart.
    """
    parsed = credentials.parse_basic(authorization)
    username, password = parsed if parsed is not None else ("", "")
    row = None
    if SUFFIX_RE.fullmatch(suffix):
        cur = await conn.execute(
            f"SELECT {_COLUMNS}, password_hash FROM endpoints WHERE suffix = %s", (suffix,)
        )
        row = await cur.fetchone()
    stored = row[-1] if row is not None and parsed is not None else credentials.DUMMY_HASH
    expected_username = (row[5] or "") if row is not None else ""
    username_ok = hmac.compare_digest(_digest(username), _digest(expected_username))
    password_ok = await asyncio.to_thread(credentials.verify_password, password, stored)
    if row is None or parsed is None or not (username_ok and password_ok):
        return None
    return Endpoint(*row[:-1])


async def get(conn: psycopg.AsyncConnection, id: int) -> Endpoint | None:
    cur = conn.cursor(row_factory=class_row(Endpoint))
    await cur.execute(f"SELECT {_COLUMNS} FROM endpoints WHERE id = %s", (id,))
    return await cur.fetchone()


async def get_by_suffix(conn: psycopg.AsyncConnection, suffix: str) -> Endpoint | None:
    if not SUFFIX_RE.fullmatch(suffix):
        return None
    cur = conn.cursor(row_factory=class_row(Endpoint))
    await cur.execute(f"SELECT {_COLUMNS} FROM endpoints WHERE suffix = %s", (suffix,))
    return await cur.fetchone()


async def list_all(conn: psycopg.AsyncConnection) -> list[Endpoint]:
    cur = conn.cursor(row_factory=class_row(Endpoint))
    await cur.execute(f"SELECT {_COLUMNS} FROM endpoints ORDER BY id")
    return await cur.fetchall()


async def delete(conn: psycopg.AsyncConnection, id: int) -> bool:
    """Delete an endpoint; its events keep `endpoint_suffix` and their `endpoint_id` goes null."""
    cur = await conn.execute("DELETE FROM endpoints WHERE id = %s RETURNING id", (id,))
    return await cur.fetchone() is not None
