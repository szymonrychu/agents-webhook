"""Per-mode verification: header, bearer, hmac (with and without timestamp), basic."""

import hashlib
import hmac as hmac_lib
import secrets
from datetime import UTC, datetime

import pytest

from agents_webhook import credentials
from agents_webhook.db.endpoints import AuthRecord, Endpoint
from agents_webhook.ingest import auth
from agents_webhook.ingest.auth import AuthRequest
from agents_webhook.secrets_box import KEY_BYTES, SecretBox

NOW = 1_800_000_000.0
BOX = SecretBox(secrets.token_bytes(KEY_BYTES))


def record(mode, secret=None, *, header=None, prefix=None, ts_header=None, box=BOX, **over):
    ep = Endpoint(
        id=7,
        suffix="s",
        type="github-pr",
        preset_id="p",
        prompt_template=None,
        username=over.get("username"),
        created_at=datetime.now(UTC),
        credentials_rotated_at=None,
        filters={},
        auth_mode=mode,
        auth_header=header,
        hmac_algorithm="sha256" if mode == "hmac" else None,
        hmac_prefix=prefix,
        hmac_timestamp_header=ts_header,
        updated_at=datetime.now(UTC),
        has_secret=mode != "basic",
    )
    ct = box.encrypt(secret, aad=b"7") if secret is not None else None
    return AuthRecord(ep, over.get("password_hash"), ct, box.kid if ct else None)


async def run(rec, headers, body=None, *, now=NOW, replay=300, box=BOX):
    req = AuthRequest(headers, body)
    return await auth.authenticate(rec, req, box=box, replay_seconds=replay, now=now)


def sign(secret, body, ts=None):
    message = (ts.encode() + b":" if ts is not None else b"") + body
    return hmac_lib.new(secret.encode(), message, hashlib.sha256).hexdigest()


# --- header -----------------------------------------------------------------------------------


async def test_header_mode_accepts_configured_header_only():
    secret = credentials.generate_secret()
    rec = record("header", secret, header="X-Gitlab-Token")
    assert await run(rec, {"x-gitlab-token": secret}) is rec.endpoint
    assert await run(rec, {"X-GITLAB-TOKEN": secret}) is rec.endpoint  # header name case
    assert await run(rec, {"x-gitlab-token": secret + "x"}) is None
    assert await run(rec, {"x-gitlab-token": ""}) is None
    assert await run(rec, {}) is None
    assert await run(rec, {"x-other": secret}) is None
    assert await run(rec, {"authorization": f"Bearer {secret}"}) is None


# --- bearer -----------------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_bearer_scheme_case_insensitive(scheme):
    secret = credentials.generate_secret()
    rec = record("bearer", secret)
    assert await run(rec, {"authorization": f"{scheme} {secret}"}) is rec.endpoint


async def test_bearer_rejects_wrong_scheme_token_and_missing():
    secret = credentials.generate_secret()
    rec = record("bearer", secret)
    assert await run(rec, {"authorization": f"Basic {secret}"}) is None
    assert await run(rec, {"authorization": f"Bearer {secret}x"}) is None
    assert await run(rec, {"authorization": secret}) is None
    assert await run(rec, {"authorization": "Bearer"}) is None
    assert await run(rec, {}) is None


# --- hmac -------------------------------------------------------------------------------------


async def test_hmac_github_style_prefix_over_raw_body():
    secret = credentials.generate_secret()
    body = b'{"a": 1,  "b":2}\n'  # unusual spacing: the raw bytes must be signed, not re-encoded
    rec = record("hmac", secret, header="X-Hub-Signature-256", prefix="sha256=")
    good = {"x-hub-signature-256": "sha256=" + sign(secret, body)}
    assert await run(rec, good, body) is rec.endpoint
    assert await run(rec, good, body + b" ") is None
    assert await run(rec, {"x-hub-signature-256": sign(secret, body)}, body) is None  # no prefix
    assert await run(rec, {"x-hub-signature-256": "sha1=" + sign(secret, body)}, body) is None
    assert await run(rec, {}, body) is None


async def test_hmac_wrong_secret_and_no_body_fail():
    secret = credentials.generate_secret()
    rec = record("hmac", secret, header="X-Sig", prefix="")
    body = b"payload"
    assert await run(rec, {"x-sig": sign(secret, body)}, body) is rec.endpoint
    assert await run(rec, {"x-sig": sign(credentials.generate_secret(), body)}, body) is None
    assert await run(rec, {"x-sig": sign(secret, body)}, None) is None  # body over the cap


async def test_hmac_grafana_without_timestamp_header_signs_body_only():
    secret = credentials.generate_secret()
    body = b'{"alerts": []}'
    rec = record("hmac", secret, header="X-Grafana-Alerting-Signature")
    assert await run(rec, {"x-grafana-alerting-signature": sign(secret, body)}, body)


async def test_hmac_grafana_timestamp_signs_timestamp_colon_body():
    secret = credentials.generate_secret()
    body = b'{"alerts": []}'
    ts = str(int(NOW))
    rec = record("hmac", secret, header="X-Sig", ts_header="X-Ts")
    ok = {"x-sig": sign(secret, body, ts), "x-ts": ts}
    assert await run(rec, ok, body) is rec.endpoint
    # a body-only signature does not satisfy a timestamped endpoint
    assert await run(rec, {"x-sig": sign(secret, body), "x-ts": ts}, body) is None
    # the timestamp is required when configured
    assert await run(rec, {"x-sig": sign(secret, body, ts)}, body) is None
    assert await run(rec, {**ok, "x-ts": ""}, body) is None
    assert await run(rec, {**ok, "x-ts": "abc"}, body) is None


async def test_hmac_replay_window_edges():
    secret = credentials.generate_secret()
    body = b"{}"
    rec = record("hmac", secret, header="X-Sig", ts_header="X-Ts")

    def headers(ts):
        return {"x-sig": sign(secret, body, str(ts)), "x-ts": str(ts)}

    now = int(NOW)
    assert await run(rec, headers(now - 300), body) is rec.endpoint
    assert await run(rec, headers(now + 300), body) is rec.endpoint
    assert await run(rec, headers(now - 301), body) is None
    assert await run(rec, headers(now + 301), body) is None
    assert await run(rec, headers(now - 301), body, replay=400) is rec.endpoint


# --- basic ------------------------------------------------------------------------------------


async def test_basic_mode_unchanged():
    import base64

    password = credentials.generate_password()
    rec = record("basic", None, username="alice", password_hash=credentials.hash_password(password))

    def hdr(u, pw):
        return {"authorization": "Basic " + base64.b64encode(f"{u}:{pw}".encode()).decode()}

    assert await run(rec, hdr("alice", password)) is rec.endpoint
    assert await run(rec, hdr("bob", password)) is None
    assert await run(rec, hdr("alice", password + "x")) is None
    assert await run(rec, {}) is None


# --- cost, constant time, failure paths -------------------------------------------------------


@pytest.fixture
def scrypt_spy(monkeypatch):
    calls = []
    real = credentials.verify_password

    def spy(password, stored):
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(credentials, "verify_password", spy)
    return calls


async def test_every_failure_path_costs_one_scrypt(scrypt_spy):
    secret = credentials.generate_secret()
    body = b"{}"
    bad_ts = {"x-sig": sign(secret, body, "1"), "x-ts": "1"}
    cases = [
        (None, {}, None),  # unknown suffix
        (record("header", secret, header="X-T"), {"x-t": "nope"}, None),
        (record("bearer", secret), {"authorization": "Bearer nope"}, None),
        (record("hmac", secret, header="X-S"), {"x-s": "nope"}, body),
        (record("hmac", secret, header="X-Sig", ts_header="X-Ts"), bad_ts, body),
    ]
    for rec, headers, req_body in cases:
        scrypt_spy.clear()
        assert await run(rec, headers, req_body) is None
        assert len(scrypt_spy) == 1


async def test_success_of_secret_modes_costs_no_scrypt(scrypt_spy):
    secret = credentials.generate_secret()
    rec = record("header", secret, header="X-T")
    assert await run(rec, {"x-t": secret})
    assert scrypt_spy == []


async def test_secret_compares_are_constant_time(monkeypatch):
    calls = []
    real = hmac_lib.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    secret = credentials.generate_secret()
    body = b"x"
    for rec, headers, req_body in [
        (record("header", secret, header="X-T"), {"x-t": "short"}, None),
        (record("bearer", secret), {"authorization": "Bearer " + "L" * 500}, None),
        (record("hmac", secret, header="X-S"), {"x-s": "z"}, body),
    ]:
        calls.clear()
        assert await run(rec, headers, req_body) is None
        assert calls and all(pair == (32, 32) for pair in calls)  # fixed-length digests


async def test_wrong_key_or_moved_ciphertext_fails_closed(caplog):
    secret = credentials.generate_secret()
    rec = record("header", secret, header="X-T")
    other = SecretBox(secrets.token_bytes(KEY_BYTES))
    assert await run(rec, {"x-t": secret}, box=other) is None
    moved = AuthRecord(
        rec.endpoint.__class__(**{**rec.endpoint.__dict__, "id": 8}),
        None,
        rec.secret_ct,
        rec.secret_kid,
    )
    assert await run(moved, {"x-t": secret}) is None
    assert secret not in caplog.text


async def test_missing_secret_fails_closed():
    rec = record("header", None, header="X-T")
    assert await run(rec, {"x-t": ""}) is None
