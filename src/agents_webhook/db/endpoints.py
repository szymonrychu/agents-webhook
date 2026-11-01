"""Webhook endpoints: a unique URL suffix, a type, a preset and an auth mode with credentials.

The api is the only writer of `endpoints`; ingest only reads it through `load_for_auth`.
A basic endpoint stores a salted scrypt hash of its password; the other modes (header,
bearer, hmac) store their secret AES-GCM encrypted with the endpoint id as AAD. `Endpoint`
never carries either, and a secret is returned exactly once, by the call that creates it.
"""

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from agents_webhook import credentials, filters
from agents_webhook.secrets_box import SecretBox

TYPES = ("grafana-alert", "gitlab-issue", "gitlab-mr", "github-issue", "github-pr")
SUFFIX_RE = re.compile(r"[A-Za-z0-9._~-]{1,128}")
USERNAME_MAX = 64
AUTH_MODES = ("basic", "header", "bearer", "hmac")
HMAC_ALGORITHMS = ("sha256",)
HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
PREFIX_MAX = 32
PATCHABLE = (
    "preset_id",
    "prompt_template",
    "suffix",
    "filters",
    "auth_mode",
    "auth_header",
    "hmac_algorithm",
    "hmac_prefix",
    "hmac_timestamp_header",
    "username",
)

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
    auth_mode: str = "basic"
    auth_header: str | None = None
    hmac_algorithm: str | None = None
    hmac_prefix: str | None = None
    hmac_timestamp_header: str | None = None


@dataclass(frozen=True)
class Credentials:
    """Plaintext credentials, returned once by create, rotate, clone and a mode change.

    Basic mode carries `username` and `password`; every other mode carries `secret`.
    """

    username: str | None = None
    password: str | None = field(default=None, repr=False)
    secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Patched:
    endpoint: Endpoint
    credentials: Credentials | None
    """Set only when the patch changed the auth mode, which issues new credentials."""


@dataclass(frozen=True)
class _Auth:
    """A validated auth configuration: the columns that describe how a request proves itself."""

    mode: str
    header: str | None = None
    algorithm: str | None = None
    prefix: str | None = None
    timestamp_header: str | None = None


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


def _check_header_name(name: str | None, field_name: str) -> str:
    if name is None or not HEADER_NAME_RE.fullmatch(name):
        raise ValueError(f"{field_name}: must be a valid HTTP header name")
    return name


def _auth_config(
    mode: str,
    header: str | None,
    algorithm: str | None,
    prefix: str | None,
    timestamp_header: str | None,
) -> _Auth:
    """Validate one mode with its fields; fields that do not belong to the mode are refused."""
    if mode not in AUTH_MODES:
        raise ValueError(f"auth_mode: must be one of {', '.join(AUTH_MODES)}")
    extras = {
        "auth_header": header,
        "hmac_algorithm": algorithm,
        "hmac_prefix": prefix,
        "hmac_timestamp_header": timestamp_header,
    }
    if mode in ("basic", "bearer"):
        stray = [name for name, value in extras.items() if value is not None]
        if stray:
            raise ValueError(f"{stray[0]}: not used in {mode} mode")
        return _Auth(mode)
    name = _check_header_name(header, "auth_header")
    if mode == "header":
        stray = [n for n in ("hmac_algorithm", "hmac_prefix", "hmac_timestamp_header") if extras[n]]
        if stray:
            raise ValueError(f"{stray[0]}: only used in hmac mode")
        return _Auth(mode, name)
    algorithm = algorithm or HMAC_ALGORITHMS[0]
    if algorithm not in HMAC_ALGORITHMS:
        raise ValueError(f"hmac_algorithm: must be one of {', '.join(HMAC_ALGORITHMS)}")
    if prefix is not None and (len(prefix) > PREFIX_MAX or not prefix.isprintable()):
        raise ValueError(f"hmac_prefix: at most {PREFIX_MAX} printable characters")
    if timestamp_header is not None:
        _check_header_name(timestamp_header, "hmac_timestamp_header")
    return _Auth(mode, name, algorithm, prefix or None, timestamp_header or None)


def _encrypt(box: SecretBox, endpoint_id: int, secret: str) -> tuple[bytes, str]:
    return box.encrypt(secret, aad=str(endpoint_id).encode()), box.kid


async def _next_id(conn: psycopg.AsyncConnection) -> int:
    """Reserve the id first: it is the AAD of the secret, which is written with the row."""
    cur = await conn.execute("SELECT nextval(pg_get_serial_sequence('endpoints', 'id'))")
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def create(
    conn: psycopg.AsyncConnection,
    data: EndpointIn,
    *,
    min_password_length: int,
    box: SecretBox,
) -> tuple[Endpoint, Credentials]:
    """Insert an endpoint; a missing suffix, username or password is generated.

    Basic mode returns `username` + `password`; header, bearer and hmac return a generated
    `secret` (a caller-chosen one is refused). Raises ValueError naming the invalid field, or
    SuffixTaken.
    """
    if data.type not in TYPES:
        raise ValueError(f"type: must be one of {', '.join(TYPES)}")
    if not data.preset_id:
        raise ValueError("preset_id: required")
    canonical = filters.EMPTY if data.filters is None else filters.validate(data.type, data.filters)
    suffix = credentials.generate_suffix() if data.suffix is None else _check_suffix(data.suffix)
    auth = _auth_config(
        data.auth_mode,
        data.auth_header,
        data.hmac_algorithm,
        data.hmac_prefix,
        data.hmac_timestamp_header,
    )
    username = password_hash = secret = None
    if auth.mode == "basic":
        creds = _credentials(data.username, data.password, min_password_length)
        username, password_hash = creds.username, await _hash(creds.password or "")
    else:
        if data.username is not None or data.password is not None:
            raise ValueError(f"username: not used in {auth.mode} mode, the secret is generated")
        secret = credentials.generate_secret()
        creds = Credentials(secret=secret)
    try:
        async with conn.transaction():
            endpoint_id = await _next_id(conn)
            secret_ct, kid = _encrypt(box, endpoint_id, secret) if secret else (None, None)
            cur = conn.cursor(row_factory=class_row(Endpoint))
            await cur.execute(
                "INSERT INTO endpoints (id, suffix, type, preset_id, prompt_template, username, "
                "password_hash, filters, auth_mode, auth_header, hmac_algorithm, hmac_prefix, "
                "hmac_timestamp_header, secret_ct, secret_kid) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_COLUMNS}",
                (
                    endpoint_id,
                    suffix,
                    data.type,
                    data.preset_id,
                    data.prompt_template,
                    username,
                    password_hash,
                    Jsonb(canonical),
                    auth.mode,
                    auth.header,
                    auth.algorithm,
                    auth.prefix,
                    auth.timestamp_header,
                    secret_ct,
                    kid,
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
    box: SecretBox,
) -> Credentials | None:
    """Replace an endpoint's credentials; None if it does not exist.

    Basic: missing username or password are generated. Other modes: a new secret is always
    generated (`username` and `password` are refused).
    """
    async with conn.transaction():
        cur = await conn.execute("SELECT auth_mode FROM endpoints WHERE id = %s FOR UPDATE", (id,))
        row = await cur.fetchone()
        if row is None:
            return None
        if row[0] == "basic":
            creds = _credentials(username, password, min_password_length)
            await conn.execute(
                "UPDATE endpoints SET username = %s, password_hash = %s, "
                "credentials_rotated_at = now(), updated_at = now() WHERE id = %s",
                (creds.username, await _hash(creds.password or ""), id),
            )
            return creds
        if username is not None or password is not None:
            raise ValueError(f"password: not used in {row[0]} mode, the secret is generated")
        secret = credentials.generate_secret()
        ciphertext, kid = _encrypt(box, id, secret)
        await conn.execute(
            "UPDATE endpoints SET secret_ct = %s, secret_kid = %s, "
            "credentials_rotated_at = now(), updated_at = now() WHERE id = %s",
            (ciphertext, kid, id),
        )
        return Credentials(secret=secret)


async def patch(
    conn: psycopg.AsyncConnection,
    id: int,
    fields: Mapping[str, Any],
    *,
    min_password_length: int,
    box: SecretBox,
) -> Patched | None:
    """Change the given fields of an endpoint in one transaction; None if it does not exist.

    `fields` holds only what the caller sent (None clears a nullable field). `type` and any
    secret are not editable (ValueError). Changing `auth_mode` starts that mode's config from
    the given fields alone and issues new credentials, returned in `Patched.credentials`;
    without a mode change the credentials are untouched. Raises ValueError or SuffixTaken.
    """
    for name in fields:
        if name not in PATCHABLE:
            raise ValueError(f"{name}: not editable")
    if "preset_id" in fields and not fields["preset_id"]:
        raise ValueError("preset_id: required")
    try:
        async with conn.transaction():
            cur = conn.cursor(row_factory=class_row(Endpoint))
            await cur.execute(f"SELECT {_COLUMNS} FROM endpoints WHERE id = %s FOR UPDATE", (id,))
            current = await cur.fetchone()
            if current is None:
                return None
            sets: dict[str, Any] = {}
            creds: Credentials | None = None
            for name in ("preset_id", "prompt_template"):
                if name in fields:
                    sets[name] = fields[name]
            if "suffix" in fields:
                sets["suffix"] = _check_suffix(fields["suffix"])
            if "filters" in fields:
                sets["filters"] = Jsonb(filters.validate(current.type, fields["filters"]))
            mode = fields.get("auth_mode", current.auth_mode)
            changed = mode != current.auth_mode
            base = (
                {}
                if changed
                else {
                    "auth_header": current.auth_header,
                    "hmac_algorithm": current.hmac_algorithm,
                    "hmac_prefix": current.hmac_prefix,
                    "hmac_timestamp_header": current.hmac_timestamp_header,
                }
            )
            merged = base | {k: v for k, v in fields.items() if k in base or changed}
            auth = _auth_config(
                mode,
                merged.get("auth_header"),
                merged.get("hmac_algorithm"),
                merged.get("hmac_prefix"),
                merged.get("hmac_timestamp_header"),
            )
            if "username" in fields and mode != "basic":
                raise ValueError(f"username: not used in {mode} mode")
            sets.update(
                auth_mode=auth.mode,
                auth_header=auth.header,
                hmac_algorithm=auth.algorithm,
                hmac_prefix=auth.prefix,
                hmac_timestamp_header=auth.timestamp_header,
            )
            if changed:
                if mode == "basic":
                    new = _credentials(fields.get("username"), None, min_password_length)
                    sets.update(
                        username=new.username,
                        password_hash=await _hash(new.password or ""),
                        secret_ct=None,
                        secret_kid=None,
                    )
                else:
                    secret = credentials.generate_secret()
                    ciphertext, kid = _encrypt(box, id, secret)
                    new = Credentials(secret=secret)
                    sets.update(
                        username=None, password_hash=None, secret_ct=ciphertext, secret_kid=kid
                    )
                creds = new
                sets["credentials_rotated_at"] = datetime.now(UTC)
            elif "username" in fields:
                sets["username"] = _check_username(fields["username"])
            sets["updated_at"] = datetime.now(UTC)
            assignments = ", ".join(f"{column} = %s" for column in sets)
            await cur.execute(
                f"UPDATE endpoints SET {assignments} WHERE id = %s RETURNING {_COLUMNS}",
                (*sets.values(), id),
            )
            updated = await cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise SuffixTaken(str(fields.get("suffix"))) from exc
    assert updated is not None
    return Patched(updated, creds)


async def clone(
    conn: psycopg.AsyncConnection,
    id: int,
    *,
    suffix: str | None = None,
    preset_id: str | None = None,
    min_password_length: int,
    box: SecretBox,
) -> tuple[Endpoint, Credentials] | None:
    """Copy an endpoint's configuration into a new endpoint; None if the source is missing.

    The copy gets a new id, a new random suffix (or `suffix`) and freshly generated
    credentials; no hash, ciphertext or username is copied.
    """
    async with conn.transaction():
        source = await get(conn, id)
        if source is None:
            return None
        data = EndpointIn(
            type=source.type,
            preset_id=preset_id or source.preset_id,
            suffix=suffix,
            prompt_template=source.prompt_template,
            filters=source.filters,
            auth_mode=source.auth_mode,
            auth_header=source.auth_header,
            hmac_algorithm=source.hmac_algorithm,
            hmac_prefix=source.hmac_prefix,
            hmac_timestamp_header=source.hmac_timestamp_header,
        )
        return await create(conn, data, min_password_length=min_password_length, box=box)


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
            "UPDATE endpoints SET filters = %s, updated_at = now() WHERE id = %s "
            f"RETURNING {_COLUMNS}",
            (Jsonb(canonical), id),
        )
        return await cur.fetchone()


@dataclass(frozen=True)
class AuthRecord:
    """An endpoint with the stored material that proves a request's credentials.

    Read only by ingest, through `load_for_auth`; `Endpoint` never carries these.
    """

    endpoint: Endpoint
    password_hash: str | None = field(repr=False)
    secret_ct: bytes | None = field(repr=False)
    secret_kid: str | None = field(repr=False)


async def load_for_auth(conn: psycopg.AsyncConnection, suffix: str) -> AuthRecord | None:
    """The endpoint and its stored credential material for `suffix`; None when unknown."""
    if not SUFFIX_RE.fullmatch(suffix):
        return None
    cur = await conn.execute(
        f"SELECT {_COLUMNS}, password_hash, secret_ct, secret_kid FROM endpoints WHERE suffix = %s",
        (suffix,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return AuthRecord(Endpoint(*row[:-3]), row[-3], bytes(row[-2]) if row[-2] else None, row[-1])


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
