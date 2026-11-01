"""The public ingest app: per-endpoint basic auth, limits, normalisation and recording."""

import asyncio
import base64
import ipaddress
import json
import logging
from typing import Any

import httpx
import psycopg
import pytest
from agents_common.db import open_pool

from agents_webhook.db import endpoints, events
from agents_webhook.db.endpoints import EndpointIn
from agents_webhook.ingest import app as ingest_app
from agents_webhook.ingest.app import create_ingest_app
from agents_webhook.ingest.ratelimit import FailureLimiter, client_key
from agents_webhook.settings import WebhookSettings

from . import payloads as p
from .authhelp import BOX, make_secret_endpoint

TYPES = ["grafana-alert", "gitlab-issue", "gitlab-mr", "github-issue", "github-pr"]
MIB = 1024 * 1024
PEER = "203.0.113.7"
REALM = 'Basic realm="agents-webhook"'

# One handled event of each endpoint's own type.
ACCEPTED: dict[str, tuple[dict[str, str], dict[str, Any]]] = {
    "github-pr": (p.gh_headers("pull_request"), p.gh_pull_request()),
    "github-issue": (p.gh_headers("issues"), p.gh_issues()),
    "gitlab-mr": (p.gl_headers("Merge Request Hook"), p.gl_merge_request()),
    "gitlab-issue": (p.gl_headers("Issue Hook"), p.gl_issue()),
    "grafana-alert": ({}, p.grafana_alert()),
}
# A valid provider event that belongs to another endpoint type.
MISMATCH: dict[str, tuple[dict[str, str], dict[str, Any]]] = {
    "github-pr": (p.gh_headers("issues"), p.gh_issues()),
    "github-issue": (p.gh_headers("pull_request"), p.gh_pull_request()),
    "gitlab-mr": (p.gl_headers("Issue Hook"), p.gl_issue()),
    "gitlab-issue": (p.gl_headers("Merge Request Hook"), p.gl_merge_request()),
    "grafana-alert": (p.gh_headers("pull_request"), p.gh_pull_request()),
}
# Not a supported provider event at all.
UNSUPPORTED: dict[str, tuple[dict[str, str], Any]] = {
    "github-pr": (p.gh_headers("push"), {"ref": "refs/heads/main"}),
    "github-issue": (p.gh_headers("push"), {"ref": "refs/heads/main"}),
    "gitlab-mr": (p.gl_headers("Push Hook"), {"object_kind": "push"}),
    "gitlab-issue": (p.gl_headers("Push Hook"), {"object_kind": "push"}),
    "grafana-alert": ({}, [1, 2, 3]),
}


def basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def make_settings(**over: Any) -> WebhookSettings:
    return WebhookSettings(
        database_url="postgresql://unused",
        orchestrator_url="http://orchestrator",
        orchestrator_token="t",
        **over,
    )


@pytest.fixture
async def conn(db):
    async with await psycopg.AsyncConnection.connect(db, autocommit=True) as c:
        yield c


@pytest.fixture
async def pool(db):
    pool = await open_pool(db, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def settings() -> WebhookSettings:
    return make_settings()


@pytest.fixture
def app(settings, pool):
    return create_ingest_app(settings, pool, BOX)


def client_for(app, peer: str = PEER) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer, 50000))
    return httpx.AsyncClient(transport=transport, base_url="http://ingest")


@pytest.fixture
async def client(app):
    async with client_for(app) as c:
        yield c


class Hook:
    """An endpoint created in the test, with its plaintext credentials."""

    def __init__(self, endpoint: endpoints.Endpoint, auth: str, password: str) -> None:
        self.endpoint, self.auth, self.password = endpoint, auth, password
        self.url = f"/webhook/{endpoint.suffix}"


@pytest.fixture
def make_hook(conn):
    async def make(type_: str = "github-pr", preset: str = "p1", **extra: Any) -> Hook:
        ep, creds = await endpoints.create(
            conn, EndpointIn(type=type_, preset_id=preset, **extra), min_password_length=16, box=BOX
        )
        return Hook(ep, basic(creds.username, creds.password), creds.password)

    return make


async def post(
    client: httpx.AsyncClient,
    hook: Hook,
    headers: dict[str, str],
    payload: Any,
    auth: str | None = None,
) -> httpx.Response:
    return await client.post(
        hook.url,
        content=json.dumps(payload).encode(),
        headers={**headers, "Authorization": auth or hook.auth},
    )


async def event_rows(conn) -> list[tuple]:
    cur = await conn.execute("SELECT id, status, reason FROM events ORDER BY id")
    return await cur.fetchall()


# --- accepted and ignored: 202 and stored ---------------------------------------------------


@pytest.mark.parametrize("type_", TYPES)
async def test_accepted_own_type_stored_new(client, conn, make_hook, type_):
    hook = await make_hook(type_, preset=f"preset-{type_}")
    headers, payload = ACCEPTED[type_]
    resp = await post(client, hook, headers, payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "new" and body["reason"] is None
    row = await events.get_event(conn, body["id"])
    assert row.endpoint_id == hook.endpoint.id and row.type == type_
    assert row.status == "new" and row.preset_id == f"preset-{type_}"
    assert row.payload == payload


async def test_accepted_github_pr_on_github_pr_endpoint_new_with_preset(client, conn, make_hook):
    hook = await make_hook("github-pr", preset="reviewer")
    resp = await post(client, hook, p.gh_headers("pull_request", "d-pr"), p.gh_pull_request())
    assert resp.status_code == 202
    row = await events.get_event(conn, resp.json()["id"])
    assert (row.status, row.reason, row.preset_id) == ("new", None, "reviewer")
    assert (row.source, row.kind, row.repo, row.number) == ("github", "mr", "octo/repo", 7)
    assert row.delivery_id == "d-pr"


async def test_accepted_pr_event_on_github_issue_endpoint_type_mismatch(client, conn, make_hook):
    hook = await make_hook("github-issue")
    resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.status_code == 202
    body = resp.json()
    assert (body["status"], body["reason"]) == ("ignored", "type_mismatch")
    row = await events.get_event(conn, body["id"])
    assert (row.status, row.reason) == ("ignored", "type_mismatch")
    assert row.endpoint_id == hook.endpoint.id and row.payload == p.gh_pull_request()


@pytest.mark.parametrize("type_", TYPES)
async def test_accepted_type_mismatch_ignored(client, conn, make_hook, type_):
    hook = await make_hook(type_)
    headers, payload = MISMATCH[type_]
    resp = await post(client, hook, headers, payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["reason"]) == ("ignored", "type_mismatch")
    row = await events.get_event(conn, body["id"])
    assert (row.status, row.reason, row.type) == ("ignored", "type_mismatch", type_)
    assert row.payload == payload


@pytest.mark.parametrize("type_", TYPES)
async def test_accepted_unsupported_event_ignored(client, conn, make_hook, type_):
    hook = await make_hook(type_)
    headers, payload = UNSUPPORTED[type_]
    resp = await post(client, hook, headers, payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["reason"]) == ("ignored", "unsupported_event")
    row = await events.get_event(conn, body["id"])
    assert (row.status, row.reason) == ("ignored", "unsupported_event")
    assert row.payload == payload


async def test_accepted_classified_ignore_reason_stored(client, conn, make_hook):
    hook = await make_hook("github-pr")
    payload = p.gh_pull_request(action="closed")
    resp = await post(client, hook, p.gh_headers("pull_request"), payload)
    assert resp.status_code == 202
    body = resp.json()
    assert (body["status"], body["reason"]) == ("ignored", "action_ignored")
    assert (await events.get_event(conn, body["id"])).reason == "action_ignored"


FILTER_DENY = {"deny": [{"field": "title", "match": "regex", "values": ["^Fix"]}]}
FILTER_DENY_MISS = {"deny": [{"field": "title", "match": "regex", "values": ["^WIP"]}]}


@pytest.fixture
def notify_spy(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def spy(conn, channel, payload):
        sent.append(payload)

    monkeypatch.setattr(ingest_app, "notify", spy)
    return sent


FILTER_ALLOW = {"allow": [{"field": "repo", "values": ["nobody/else"]}]}


async def test_deny_filter_ignores_delivery(client, conn, make_hook, notify_spy):
    hook = await make_hook("github-pr", filters=FILTER_DENY)
    resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.status_code == 202
    body = resp.json()
    assert (body["status"], body["reason"]) == ("ignored", "filter_denied: title")
    row = await events.get_event(conn, body["id"])
    assert (row.status, row.reason) == ("ignored", "filter_denied: title")
    assert notify_spy == []


async def test_allow_filter_miss_ignores_delivery(client, conn, make_hook):
    hook = await make_hook("github-pr", filters=FILTER_ALLOW)
    resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.status_code == 202
    body = resp.json()
    assert (body["status"], body["reason"]) == ("ignored", "filter_not_allowed: repo")
    assert (await events.get_event(conn, body["id"])).status == "ignored"


async def test_passing_filters_queues_delivery(client, conn, make_hook, notify_spy):
    filters = {**FILTER_DENY_MISS, "allow": [{"field": "kind", "values": ["mr"]}]}
    hook = await make_hook("github-pr", filters=filters)
    resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.status_code == 202
    body = resp.json()
    assert (body["status"], body["reason"]) == ("new", None)
    assert notify_spy == [str(body["id"])]


async def test_builtin_reason_wins_over_filter(client, conn, make_hook):
    filters = {"deny": [{"field": "action", "values": ["closed"]}]}
    hook = await make_hook("github-pr", filters=filters)
    payload = p.gh_pull_request(action="closed")
    resp = await post(client, hook, p.gh_headers("pull_request"), payload)
    assert resp.status_code == 202
    assert resp.json()["reason"] == "action_ignored"


async def test_filters_never_run_before_auth(client, conn, make_hook, monkeypatch):
    calls = []
    monkeypatch.setattr(ingest_app.filters, "evaluate", lambda *a: calls.append(a))
    hook = await make_hook("github-pr", filters=FILTER_DENY)
    resp = await post(
        client, hook, p.gh_headers("pull_request"), p.gh_pull_request(), auth=basic("x", "y")
    )
    assert resp.status_code == 401
    assert calls == []


async def test_filter_verdict_runs_off_event_loop(client, conn, make_hook, monkeypatch):
    import threading

    threads = []

    def spy(*_a):
        threads.append(threading.current_thread())

    monkeypatch.setattr(ingest_app.filters, "evaluate", spy)
    hook = await make_hook("github-pr", filters=FILTER_DENY)
    resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.status_code == 202
    assert threads
    assert all(t is not threading.main_thread() for t in threads)
    # The preview route shares the same awaitable verdict.
    threads.clear()
    event = ingest_app.normalize("github-pr", p.gh_headers("pull_request"), p.gh_pull_request())
    await ingest_app.async_verdict(event, FILTER_DENY, make_settings(), "test")
    assert threads
    assert all(t is not threading.main_thread() for t in threads)


async def test_filter_error_never_crashes_ingest(client, conn, make_hook, monkeypatch):
    def boom(*_a):
        raise RuntimeError("secret rule text")

    monkeypatch.setattr(ingest_app.filters, "evaluate", boom)
    hook = await make_hook("github-pr", filters=FILTER_DENY)
    resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert resp.json()["reason"] == "filter_error"


async def test_filters_skipped_for_type_mismatch(client, conn, make_hook):
    filters = {"allow": [{"field": "repo", "values": ["z/z"]}]}
    hook = await make_hook("github-issue", filters=filters)
    resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.json()["reason"] == "type_mismatch"


async def test_accepted_bot_actor_is_self_event(pool, conn, make_hook):
    app = create_ingest_app(make_settings(bot_logins=["alice"]), pool, BOX)
    hook = await make_hook("github-pr")
    async with client_for(app) as c:
        resp = await post(c, hook, p.gh_headers("pull_request"), p.gh_pull_request())
    assert resp.status_code == 202
    assert (resp.json()["status"], resp.json()["reason"]) == ("ignored", "self_event")


async def test_accepted_redelivery_ignored_duplicate(client, conn, make_hook):
    hook = await make_hook("gitlab-mr")
    headers, payload = p.gl_headers("Merge Request Hook", "uuid-1"), p.gl_merge_request()
    first = await post(client, hook, headers, payload)
    again = await post(client, hook, headers, payload)
    assert first.status_code == again.status_code == 202
    assert first.json()["status"] == "new"
    assert (again.json()["status"], again.json()["reason"]) == ("ignored", "duplicate")
    row = await events.get_event(conn, again.json()["id"])
    assert row.duplicate_of == first.json()["id"]


@pytest.mark.parametrize("type_", ["github-pr", "github-issue"])
async def test_accepted_github_ping_pong_not_stored(client, conn, make_hook, type_):
    hook = await make_hook(type_)
    resp = await post(client, hook, p.gh_headers("ping"), {"zen": "Keep it simple."})
    assert resp.status_code == 202
    assert resp.json() == {"status": "pong"}
    assert await event_rows(conn) == []


async def test_accepted_new_event_notifies_worker(client, db, make_hook):
    hook = await make_hook("github-pr")
    async with await psycopg.AsyncConnection.connect(db, autocommit=True) as listener:
        await listener.execute("LISTEN webhook_events")
        resp = await post(client, hook, p.gh_headers("pull_request"), p.gh_pull_request())
        assert resp.status_code == 202
        got = [n.payload async for n in listener.notifies(timeout=5, stop_after=1)]
    assert got == [str(resp.json()["id"])]


# --- 400 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("type_", TYPES)
async def test_invalid_json_is_bad_request(client, conn, make_hook, type_):
    hook = await make_hook(type_)
    headers, _ = ACCEPTED[type_]
    resp = await client.post(
        hook.url, content=b"{not json", headers={**headers, "Authorization": hook.auth}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
    assert await event_rows(conn) == []


async def test_invalid_json_too_deeply_nested_is_bad_request(client, conn, make_hook):
    hook = await make_hook("grafana-alert")
    deep = b"[" * 100000 + b"]" * 100000
    resp = await client.post(hook.url, content=deep, headers={"Authorization": hook.auth})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


async def test_unavailable_not_for_unstorable_text(client, conn, make_hook):
    # A NUL character cannot be stored in Postgres text or jsonb: the request is bad, the DB
    # is fine, so this is 400 and not a 5xx the forge would retry forever.
    hook = await make_hook("github-pr")
    payload = p.gh_pull_request()
    payload["pull_request"]["title"] = "bad\u0000title"
    resp = await post(client, hook, p.gh_headers("pull_request"), payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


# --- 401 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("type_", TYPES)
async def test_auth_only_own_credentials_accepted(client, conn, make_hook, type_):
    hook = await make_hook(type_)
    other = await make_hook(type_)
    headers, payload = ACCEPTED[type_]
    assert (await post(client, other, headers, payload, auth=hook.auth)).status_code == 401
    assert (await post(client, hook, headers, payload, auth=other.auth)).status_code == 401
    assert (await post(client, hook, headers, payload)).status_code == 202
    assert len(await event_rows(conn)) == 1


@pytest.mark.parametrize("type_", TYPES)
async def test_auth_failures_identical_401(client, conn, make_hook, type_):
    hook = await make_hook(type_)
    gone = await make_hook(type_)
    await endpoints.delete(conn, gone.endpoint.id)
    username = hook.endpoint.username
    headers, payload = ACCEPTED[type_]
    body = json.dumps(payload).encode()
    attempts = [
        ("/webhook/no-such-suffix", hook.auth),
        (gone.url, gone.auth),
        (hook.url, None),
        (hook.url, "Bearer abc"),
        (hook.url, "Basic !!!not-base64"),
        (hook.url, basic("someone-else", hook.password)),
        (hook.url, basic(username, hook.password + "x")),
    ]
    seen = set()
    for url, auth in attempts:
        req_headers = dict(headers)
        if auth is not None:
            req_headers["Authorization"] = auth
        resp = await client.post(url, content=body, headers=req_headers)
        assert resp.status_code == 401, (url, auth)
        assert resp.headers["www-authenticate"] == REALM
        assert resp.json() == {"error": {"code": "unauthorized", "message": "unauthorized"}}
        seen.add((resp.content, resp.headers["content-type"], resp.headers["www-authenticate"]))
    assert len(seen) == 1
    assert await event_rows(conn) == []


async def test_auth_before_body_read(client, conn, make_hook):
    hook = await make_hook("github-pr")
    pulled = 0

    async def body():
        nonlocal pulled
        for _ in range(8):
            pulled += 1
            yield b"x" * MIB

    resp = await client.post(
        hook.url, content=body(), headers={"Authorization": basic("foo", "-".join(["not", "real"]))}
    )
    assert resp.status_code == 401
    assert pulled == 0


async def test_auth_logs_never_carry_secrets(client, conn, make_hook, caplog):
    caplog.set_level(logging.DEBUG)
    hook = await make_hook("github-pr")
    payload = p.gh_pull_request()
    payload["pull_request"]["body"] = "BODY-MARKER-42"
    bad_auth = basic(hook.endpoint.username, "WRONG-PASSWORD-MARKER")
    secret_suffix = "secret-suffix-marker"
    await client.post(
        f"/webhook/{secret_suffix}",
        content=json.dumps(payload).encode(),
        headers={"Authorization": hook.auth},
    )
    await post(client, hook, p.gh_headers("pull_request"), payload, auth=bad_auth)
    ok = await post(client, hook, p.gh_headers("pull_request"), payload)
    assert ok.status_code == 202
    # Only the service's own records: the test client's httpx log names the URL it called.
    ours = [r for r in caplog.records if r.name.startswith("agents_webhook")]
    text = "\n".join(f"{r.getMessage()} {r.args}" for r in ours)
    for secret in (
        secret_suffix,
        "WRONG-PASSWORD-MARKER",
        bad_auth.split(" ", 1)[1],
        hook.auth.split(" ", 1)[1],
        hook.password,
        "BODY-MARKER-42",
    ):
        assert secret not in text
    failed = [r.getMessage() for r in ours if "unauthorized" in r.getMessage()]
    assert len(failed) == 2 and all(PEER in m for m in failed)
    assert hook.endpoint.suffix not in "".join(failed)
    accepted = [r.getMessage() for r in ours if f"event={ok.json()['id']}" in r.getMessage()]
    assert len(accepted) == 1
    line = accepted[0]
    assert f"endpoint={hook.endpoint.id}" in line and "status=new" in line
    assert "reason=None" in line and PEER in line


# --- 413 -----------------------------------------------------------------------------------


@pytest.fixture
def normalize_spy(monkeypatch):
    calls: list[str] = []
    real = ingest_app.normalize

    def spy(endpoint_type, headers, payload):
        calls.append(endpoint_type)
        return real(endpoint_type, headers, payload)

    monkeypatch.setattr(ingest_app, "normalize", spy)
    return calls


@pytest.mark.parametrize("type_", TYPES)
async def test_too_large_6mib_non_json_is_413(client, conn, make_hook, normalize_spy, type_):
    hook = await make_hook(type_)
    headers, _ = ACCEPTED[type_]
    resp = await client.post(
        hook.url, content=b"x" * (6 * MIB), headers={**headers, "Authorization": hook.auth}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert normalize_spy == []
    assert await event_rows(conn) == []


async def test_too_large_chunked_body_stops_reading_at_cap(client, conn, make_hook, normalize_spy):
    hook = await make_hook("github-pr")
    pulled = 0

    async def body():
        nonlocal pulled
        for _ in range(12):
            pulled += 1
            yield b"x" * MIB

    resp = await client.post(
        hook.url,
        content=body(),
        headers={**p.gh_headers("pull_request"), "Authorization": hook.auth},
    )
    assert resp.status_code == 413
    assert pulled == 6
    assert normalize_spy == []


async def test_too_large_cap_is_inclusive(pool, conn, make_hook):
    hook = await make_hook("grafana-alert")
    raw = json.dumps(p.grafana_alert()).encode()
    padded = raw[:-1] + b" " * 50 + b"}"
    app = create_ingest_app(make_settings(max_body_bytes=len(padded)), pool, BOX)
    async with client_for(app) as c:
        exact = await c.post(hook.url, content=padded, headers={"Authorization": hook.auth})
        over = await c.post(hook.url, content=padded + b" ", headers={"Authorization": hook.auth})
    assert exact.status_code == 202
    assert over.status_code == 413


# --- 429 -----------------------------------------------------------------------------------


@pytest.fixture
def auth_spy(monkeypatch):
    calls: list[str] = []
    real = ingest_app.endpoints.load_for_auth

    async def spy(conn, suffix):
        calls.append(suffix)
        return await real(conn, suffix)

    monkeypatch.setattr(ingest_app.endpoints, "load_for_auth", spy)
    return calls


async def test_limit_eleventh_failed_attempt_is_429_other_client_401(app, make_hook, auth_spy):
    hook = await make_hook("github-pr")
    headers, payload = ACCEPTED["github-pr"]
    wrong = basic(hook.endpoint.username, "wrong-password-123")
    async with (
        client_for(app, "198.51.100.10") as attacker,
        client_for(app, "198.51.100.20") as bystander,
    ):
        for _ in range(10):
            assert (await post(attacker, hook, headers, payload, auth=wrong)).status_code == 401
        blocked = await post(attacker, hook, headers, payload, auth=wrong)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "too_many_requests"
        assert 1 <= int(blocked.headers["retry-after"]) <= 60
        calls = len(auth_spy)
        # Blocked before any DB or hash work, even with the right credentials.
        assert (await post(attacker, hook, headers, payload)).status_code == 429
        assert len(auth_spy) == calls
        assert (await post(bystander, hook, headers, payload, auth=wrong)).status_code == 401
        assert (await post(bystander, hook, headers, payload)).status_code == 202


async def test_limit_forwarded_for_only_from_trusted_proxy(pool, make_hook):
    app = create_ingest_app(make_settings(trusted_proxies=["10.0.0.0/8"]), pool, BOX)
    hook = await make_hook("github-pr")
    headers, payload = ACCEPTED["github-pr"]
    wrong = basic(hook.endpoint.username, "wrong-password-123")

    async def attempt(c, forwarded: str) -> int:
        resp = await c.post(
            hook.url,
            content=json.dumps(payload).encode(),
            headers={**headers, "Authorization": wrong, "X-Forwarded-For": forwarded},
        )
        return resp.status_code

    async with client_for(app, "10.1.2.3") as proxy, client_for(app, "203.0.113.50") as direct:
        for _ in range(10):
            assert await attempt(proxy, "192.0.2.1, 198.51.100.1") == 401
        assert await attempt(proxy, "192.0.2.99, 198.51.100.1") == 429
        # Another client behind the same proxy is keyed apart.
        assert await attempt(proxy, "198.51.100.2") == 401
        # A direct (untrusted) peer cannot spoof its key through X-Forwarded-For.
        for i in range(10):
            assert await attempt(direct, f"192.0.2.{i}") == 401
        assert await attempt(direct, "192.0.2.200") == 429


def test_limit_unit_blocks_after_per_minute_failures():
    now = [1000.0]
    limiter = FailureLimiter(3, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.blocked("a") is None
        limiter.record_failure("a")
        now[0] += 1
    assert limiter.blocked("a") == pytest.approx(57.0)
    assert limiter.blocked("b") is None


def test_limit_unit_sliding_window_releases_oldest():
    now = [0.0]
    limiter = FailureLimiter(2, clock=lambda: now[0])
    limiter.record_failure("a")
    now[0] = 30.0
    limiter.record_failure("a")
    assert limiter.blocked("a") == pytest.approx(30.0)
    now[0] = 60.0
    assert limiter.blocked("a") is None
    limiter.record_failure("a")
    assert limiter.blocked("a") == pytest.approx(30.0)
    now[0] = 200.0
    assert limiter.blocked("a") is None


def test_limit_unit_evicts_oldest_keys_beyond_max_keys():
    limiter = FailureLimiter(1, max_keys=2, clock=lambda: 0.0)
    limiter.record_failure("a")
    limiter.record_failure("b")
    limiter.record_failure("c")
    assert limiter.blocked("a") is None
    assert limiter.blocked("b") is not None and limiter.blocked("c") is not None
    limiter.record_failure("b")  # refreshes b, so a new key evicts c
    limiter.record_failure("d")
    assert limiter.blocked("c") is None
    assert limiter.blocked("b") is not None and limiter.blocked("d") is not None
    assert len(limiter) == 2


def test_limit_unit_rejects_bad_arguments():
    with pytest.raises(ValueError):
        FailureLimiter(0)
    with pytest.raises(ValueError):
        FailureLimiter(1, max_keys=0)


TRUSTED = [ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8")]


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        ("203.0.113.1", "198.51.100.1", "203.0.113.1"),
        ("203.0.113.1", None, "203.0.113.1"),
        ("10.0.0.1", None, "10.0.0.1"),
        ("10.0.0.1", "", "10.0.0.1"),
        ("10.0.0.1", "198.51.100.1", "198.51.100.1"),
        ("10.0.0.1", "192.0.2.9, 198.51.100.1", "198.51.100.1"),
        ("10.0.0.1", "192.0.2.9, 198.51.100.1, 10.2.0.1", "198.51.100.1"),
        ("10.0.0.1", " 198.51.100.1 ,10.2.0.1 ", "198.51.100.1"),
        ("10.0.0.1", "10.3.0.1, 10.2.0.1", "10.3.0.1"),
        ("10.0.0.1", "garbage, 10.2.0.1", "garbage"),
        ("fd00::1", "2001:db8::5", "2001:db8::5"),
        ("::ffff:10.0.0.1", "198.51.100.1", "198.51.100.1"),
    ],
)
def test_limit_unit_client_key(peer, forwarded, expected):
    assert client_key(peer, forwarded, TRUSTED) == expected


def test_limit_unit_client_key_accepts_cidr_strings():
    assert client_key("10.0.0.1", "198.51.100.1", ["10.0.0.0/8"]) == "198.51.100.1"


# --- 503 and routes ------------------------------------------------------------------------


@pytest.mark.parametrize("type_", TYPES)
async def test_unavailable_db_failure_on_record_is_503(client, make_hook, monkeypatch, type_):
    hook = await make_hook(type_)

    async def broken(*args, **kwargs):
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(ingest_app.events, "record", broken)
    headers, payload = ACCEPTED[type_]
    resp = await post(client, hook, headers, payload)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "unavailable"


async def test_unavailable_record_conflict_is_503(client, make_hook, monkeypatch):
    hook = await make_hook("github-pr")

    async def conflict(*args, **kwargs):
        raise events.RecordConflict("kept being claimed")

    monkeypatch.setattr(ingest_app.events, "record", conflict)
    resp = await post(client, hook, *ACCEPTED["github-pr"])
    assert resp.status_code == 503


async def test_unavailable_db_down_503_and_not_ready(db, settings, make_hook):
    hook = await make_hook("github-pr")
    pool = await open_pool(db, max_size=2)
    app = create_ingest_app(settings, pool, BOX)
    await pool.close()
    async with client_for(app) as c:
        resp = await post(c, hook, *ACCEPTED["github-pr"])
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "unavailable"
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 503


async def test_routes_only_webhook_and_health(client, make_hook):
    hook = await make_hook("github-pr")
    assert (await client.get("/healthz")).status_code == 200
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json() == {"status": "ok"}
    for method, path in [
        ("GET", "/"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/redoc"),
        ("POST", "/webhook"),
        ("POST", "/webhook/"),
        ("POST", f"/webhook/{hook.endpoint.suffix}/extra"),
        ("POST", "/webhooks/github"),
        ("GET", "/webhooks/api/v1/endpoints"),
        ("GET", hook.url),
        ("PUT", hook.url),
        ("POST", "/healthz"),
    ]:
        resp = await client.request(method, path, headers={"Authorization": hook.auth})
        assert resp.status_code == 404, (method, path, resp.status_code)
        assert resp.json()["error"]["code"] == "not_found"


async def test_routes_concurrent_deliveries_all_recorded(client, conn, make_hook):
    hook = await make_hook("github-issue")
    results = await asyncio.gather(
        *(post(client, hook, p.gh_headers("issues", f"d-{i}"), p.gh_issues()) for i in range(4))
    )
    assert sorted(r.status_code for r in results) == [202] * 4
    statuses = sorted(r.json()["status"] for r in results)
    assert statuses == ["ignored", "ignored", "ignored", "new"]


async def test_request_timeout_body_shape():
    from agents_webhook import cli

    async def app(scope, receive, send):
        await receive()

    async def stall():
        await asyncio.sleep(5)

    async def receive():
        return await stall()

    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    await cli.BodyReadTimeout(app, 0.05)({"type": "http"}, receive, send)
    assert sent[0]["status"] == 408
    assert json.loads(sent[1]["body"]) == {
        "error": {"code": "request_timeout", "message": "request timeout"}
    }


# --- header, bearer and hmac modes ------------------------------------------------------------


def _sign(secret: str, body: bytes, ts: str | None = None) -> str:
    import hashlib
    import hmac

    message = (ts.encode() + b":" if ts is not None else b"") + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


async def _send(client, suffix, headers, body):
    return await client.post(f"/webhook/{suffix}", content=body, headers=headers)


async def test_header_mode_accepts_gitlab_token_and_stores_event(client, conn):
    ep, secret = await make_secret_endpoint(
        conn, mode="header", auth_header="X-Gitlab-Token", type_="gitlab-mr"
    )
    headers, payload = ACCEPTED["gitlab-mr"]
    body = json.dumps(payload).encode()
    ok = await _send(client, ep.suffix, {**headers, "X-Gitlab-Token": secret}, body)
    assert ok.status_code == 202 and ok.json()["status"] == "new"
    assert len(await event_rows(conn)) == 1
    bad = await _send(client, ep.suffix, {**headers, "X-Gitlab-Token": secret + "x"}, body)
    assert bad.status_code == 401


async def test_bearer_mode_accepts_bearer(client, conn):
    ep, secret = await make_secret_endpoint(conn, mode="bearer", type_="grafana-alert")
    body = json.dumps(ACCEPTED["grafana-alert"][1]).encode()
    ok = await _send(client, ep.suffix, {"Authorization": f"bearer {secret}"}, body)
    assert ok.status_code == 202
    assert (await _send(client, ep.suffix, {"Authorization": secret}, body)).status_code == 401


async def test_hmac_mode_verifies_raw_body_bytes(client, conn):
    ep, secret = await make_secret_endpoint(
        conn, mode="hmac", auth_header="X-Hub-Signature-256", hmac_prefix="sha256="
    )
    headers, payload = ACCEPTED["github-pr"]
    body = json.dumps(payload, indent=3).encode()  # whatever bytes the sender used
    sig = {"X-Hub-Signature-256": "sha256=" + _sign(secret, body)}
    ok = await _send(client, ep.suffix, {**headers, **sig}, body)
    assert ok.status_code == 202 and ok.json()["status"] == "new"
    reencoded = json.dumps(payload).encode()
    assert (await _send(client, ep.suffix, {**headers, **sig}, reencoded)).status_code == 401


async def test_hmac_timestamp_replay_window_over_http(client, conn):
    import time

    ep, secret = await make_secret_endpoint(
        conn, mode="hmac", auth_header="X-Sig", timestamp_header="X-Ts", type_="grafana-alert"
    )
    body = json.dumps(ACCEPTED["grafana-alert"][1]).encode()

    def hdrs(ts: int) -> dict[str, str]:
        return {"X-Sig": _sign(secret, body, str(ts)), "X-Ts": str(ts)}

    now = int(time.time())
    assert (await _send(client, ep.suffix, hdrs(now), body)).status_code == 202
    assert (await _send(client, ep.suffix, hdrs(now - 3600), body)).status_code == 401


async def test_hmac_body_over_cap_is_the_identical_401(pool, conn):
    app = create_ingest_app(make_settings(max_body_bytes=64), pool, BOX)
    ep, secret = await make_secret_endpoint(conn, mode="hmac", auth_header="X-Sig")
    body = b"x" * 200
    async with client_for(app) as c:
        resp = await _send(c, ep.suffix, {"X-Sig": _sign(secret, body)}, body)
        unknown = await _send(c, "no-such-suffix", {"X-Sig": _sign(secret, body)}, body)
    assert resp.status_code == 401
    assert (resp.content, resp.headers["www-authenticate"]) == (
        unknown.content,
        unknown.headers["www-authenticate"],
    )


async def test_secret_mode_413_only_after_auth(pool, conn):
    app = create_ingest_app(make_settings(max_body_bytes=64), pool, BOX)
    ep, secret = await make_secret_endpoint(conn, mode="header", auth_header="X-Tok")
    body = b"x" * 200
    async with client_for(app) as c:
        assert (await _send(c, ep.suffix, {"X-Tok": "wrong"}, body)).status_code == 401
        assert (await _send(c, ep.suffix, {"X-Tok": secret}, body)).status_code == 413


async def test_secret_mode_does_not_read_body_before_auth(client, conn):
    ep, _ = await make_secret_endpoint(conn, mode="header", auth_header="X-Tok")
    pulled = 0

    async def body():
        nonlocal pulled
        for _ in range(8):
            pulled += 1
            yield b"x" * MIB

    resp = await client.post(f"/webhook/{ep.suffix}", content=body(), headers={"X-Tok": "wrong"})
    assert resp.status_code == 401 and pulled == 0


async def test_every_mode_failure_is_the_identical_401(client, conn, make_hook):
    header_ep, header_secret = await make_secret_endpoint(conn, mode="header", auth_header="X-Tok")
    bearer_ep, _ = await make_secret_endpoint(conn, mode="bearer")
    hmac_ep, hmac_secret = await make_secret_endpoint(
        conn, mode="hmac", auth_header="X-Sig", timestamp_header="X-Ts"
    )
    basic_hook = await make_hook("github-pr")
    body = b"{}"
    attempts = [
        ("no-such-suffix", {"X-Tok": header_secret}),
        (header_ep.suffix, {}),
        (header_ep.suffix, {"X-Tok": "wrong"}),
        (bearer_ep.suffix, {"Authorization": "Bearer wrong"}),
        (hmac_ep.suffix, {"X-Sig": "deadbeef"}),
        (hmac_ep.suffix, {"X-Sig": _sign(hmac_secret, body, "1"), "X-Ts": "1"}),  # stale
        (hmac_ep.suffix, {"X-Sig": _sign(hmac_secret, body), "X-Ts": "x"}),
        (basic_hook.endpoint.suffix, {"Authorization": "Bearer wrong"}),
    ]
    seen = set()
    for suffix, headers in attempts:
        resp = await _send(client, suffix, headers, body)
        assert resp.status_code == 401, (suffix, headers)
        seen.add((resp.content, resp.headers["content-type"], resp.headers["www-authenticate"]))
    assert len(seen) == 1
    assert await event_rows(conn) == []


async def test_secret_mode_limiter_runs_first_and_counts_failures(app, conn):
    ep, secret = await make_secret_endpoint(conn, mode="header", auth_header="X-Tok")
    body = b"{}"
    async with client_for(app, "198.51.100.30") as attacker:
        for _ in range(10):
            assert (await _send(attacker, ep.suffix, {"X-Tok": "no"}, body)).status_code == 401
        assert (await _send(attacker, ep.suffix, {"X-Tok": secret}, body)).status_code == 429


async def test_secret_mode_logs_carry_no_secret_or_signature(client, conn, caplog):
    caplog.set_level(logging.DEBUG)
    ep, secret = await make_secret_endpoint(conn, mode="hmac", auth_header="X-Sig")
    headers, payload = ACCEPTED["github-pr"]
    body = json.dumps(payload).encode()
    good = _sign(secret, body)
    await _send(client, ep.suffix, {**headers, "X-Sig": good}, body)
    await _send(client, ep.suffix, {**headers, "X-Sig": "BAD-SIG-MARKER"}, body)
    ours = [r for r in caplog.records if r.name.startswith("agents_webhook")]
    text = "\n".join(f"{r.getMessage()} {r.args}" for r in ours)
    for value in (secret, good, "BAD-SIG-MARKER", ep.suffix):
        assert value not in text
