"""Per-mode verification of an incoming webhook request (no database access).

Modes: `basic` (username + scrypt), `header` (a configured header holds the secret),
`bearer` (`Authorization: Bearer <secret>`), `hmac` (HMAC-SHA256 of the raw body, optionally
of `<timestamp>:<body>` with a replay window). Every secret compare is constant time (both
sides are hashed to fixed-length digests first). Every failure takes the same shape: the
caller answers one identical 401, and every failed path ends in one scrypt computation so
timing does not tell an unknown suffix from a wrong credential. Nothing here logs values.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from agents_webhook import credentials
from agents_webhook.db.endpoints import AuthRecord, Endpoint
from agents_webhook.secrets_box import SecretBox, SecretBoxError

log = logging.getLogger(__name__)

HMAC_DIGESTS = {"sha256": hashlib.sha256}


@dataclass(frozen=True)
class AuthRequest:
    """What verification needs from the request: headers and, for hmac, the raw body.

    `body` is None when it was not read (not an hmac endpoint) or exceeded the cap; an hmac
    endpoint then fails verification, since a body that cannot be checked is not trusted.
    """

    headers: Mapping[str, str]
    body: bytes | None = None

    def header(self, name: str | None) -> str:
        if not name:
            return ""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return ""


def _digest(value: str) -> bytes:
    # Fixed-length digests, so the constant-time compare does not leak the value's length.
    return hashlib.sha256(value.encode()).digest()


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(_digest(a), _digest(b))


def _bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    return token if scheme.lower() == "bearer" else ""


def _hmac_ok(
    record: AuthRecord, secret: str, request: AuthRequest, now: float, replay: int
) -> bool:
    endpoint = record.endpoint
    digestmod = HMAC_DIGESTS.get(endpoint.hmac_algorithm or "")
    if digestmod is None or request.body is None:
        return False
    message = request.body
    timestamp_ok = True
    if endpoint.hmac_timestamp_header:
        stamp = request.header(endpoint.hmac_timestamp_header)
        try:
            timestamp_ok = abs(now - int(stamp)) <= replay
        except ValueError:
            timestamp_ok = False
        message = stamp.encode() + b":" + message
    mac = hmac.new(secret.encode(), message, digestmod).hexdigest()
    expected = (endpoint.hmac_prefix or "") + mac
    signature_ok = _equal(request.header(endpoint.auth_header), expected)
    return signature_ok and timestamp_ok


async def _dummy_scrypt() -> None:
    await asyncio.to_thread(credentials.verify_password, "", credentials.DUMMY_HASH)


async def _basic(record: AuthRecord | None, request: AuthRequest) -> bool:
    """One username compare and one scrypt verify, against DUMMY_HASH when there is no record."""
    parsed = credentials.parse_basic(request.header("Authorization"))
    username, password = parsed if parsed is not None else ("", "")
    usable = record is not None and parsed is not None and record.password_hash is not None
    stored = record.password_hash if usable and record is not None else None
    expected = (record.endpoint.username or "") if record is not None else ""
    username_ok = _equal(username, expected)
    password_ok = await asyncio.to_thread(
        credentials.verify_password, password, stored or credentials.DUMMY_HASH
    )
    return usable and username_ok and password_ok


def _secret_ok(
    record: AuthRecord, box: SecretBox, request: AuthRequest, now: float, replay: int
) -> bool:
    endpoint = record.endpoint
    if record.secret_ct is None:
        return False
    try:
        secret = box.decrypt(record.secret_ct, aad=str(endpoint.id).encode())
    except SecretBoxError:
        log.error(
            "ingest: secret does not decrypt endpoint=%s mode=%s", endpoint.id, endpoint.auth_mode
        )
        return False
    if endpoint.auth_mode == "header":
        return _equal(request.header(endpoint.auth_header), secret)
    if endpoint.auth_mode == "bearer":
        return _equal(_bearer_token(request.header("Authorization")), secret)
    if endpoint.auth_mode == "hmac":
        return _hmac_ok(record, secret, request, now, replay)
    return False


async def authenticate(
    record: AuthRecord | None,
    request: AuthRequest,
    *,
    box: SecretBox,
    replay_seconds: int,
    now: float | None = None,
) -> Endpoint | None:
    """The endpoint when the request proves its credentials, else None (one outcome for all)."""
    when = time.time() if now is None else now
    if record is None:
        await _basic(None, request)  # the failure cost every other path ends in: one scrypt
        return None
    if record.endpoint.auth_mode == "basic":
        return record.endpoint if await _basic(record, request) else None
    if _secret_ok(record, box, request, when, replay_seconds):
        return record.endpoint
    await _dummy_scrypt()
    return None
