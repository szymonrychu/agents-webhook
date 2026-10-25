"""Endpoint credentials: generation, scrypt hashing and HTTP basic-auth parsing (no I/O).

Only `hash_password` output is ever stored; a password exists in memory between generation
(or the user's request) and the create or rotate response, and is never logged.
"""

import base64
import binascii
import hashlib
import hmac
import secrets

SCHEME = "scrypt"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024


def generate_suffix() -> str:
    """A 32-char URL-safe random suffix."""
    return secrets.token_urlsafe(24)


def generate_username() -> str:
    """`wh-` followed by 8 lowercase hex chars."""
    return "wh-" + secrets.token_hex(4)


def generate_password() -> str:
    """A 43-char URL-safe random password."""
    return secrets.token_urlsafe(32)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_DKLEN)


def hash_password(password: str) -> str:
    """`scrypt$16384$8$1$<salt b64>$<hash b64>` with a fresh 16-byte salt."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (
            SCHEME,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def verify_password(password: str, stored: str) -> bool:
    """True when `password` hashes to `stored`; the digests are compared in constant time.

    A malformed `stored` value is False; the parameters are accepted only at the cost this
    module writes, so a tampered row cannot make the check cheaper or exhaust memory.
    """
    parts = stored.split("$")
    if len(parts) != 6 or parts[:4] != [SCHEME, str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)]:
        return False
    try:
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except (binascii.Error, ValueError):
        return False
    if not salt or not expected:
        return False
    actual = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return hmac.compare_digest(actual, expected)


DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
"""Hash of a random password nobody knows, computed once at import: an unknown suffix or a
malformed header is verified against it so every failed authentication costs one scrypt."""


def parse_basic(header: str | None) -> tuple[str, str] | None:
    """`(username, password)` from an `Authorization: Basic ...` header; None when malformed."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token or " " in token:
        return None
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password
