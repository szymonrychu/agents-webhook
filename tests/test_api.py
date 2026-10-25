"""The UI API: endpoints create/list/rotate/delete, events, templates preview, meta."""

import hashlib
import json
import re
import secrets
import uuid
from typing import Any

import httpx
import psycopg
import pytest
from agents_common.db import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient

from agents_webhook import filters as filter_engine
from agents_webhook.api.app import create_api_app
from agents_webhook.db import endpoints, events
from agents_webhook.db.endpoints import EndpointIn
from agents_webhook.settings import WebhookSettings

from . import payloads as p
from .test_worker import ev

PW_A, PW_B, PW_C = (secrets.token_urlsafe(24) for _ in range(3))

P = "/webhooks/api/v1"


def make_settings(**over: Any) -> WebhookSettings:
    return WebhookSettings(
        database_url="postgresql://unused",
        orchestrator_url="http://orchestrator",
        orchestrator_token="t",
        public_base_url="https://hooks.example/",
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
def fake() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
def preset(fake) -> str:
    return str(fake.add_preset("dev").id)


@pytest.fixture
async def orch(fake):
    transport = httpx.ASGITransport(app=fake.app)
    async with OrchestratorClient("http://orch", fake.token, transport=transport) as c:
        yield c


@pytest.fixture
def settings() -> WebhookSettings:
    return make_settings()


@pytest.fixture
async def api(settings, pool, orch):
    app = create_api_app(settings, pool, orch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as c:
        yield c


def code(resp: httpx.Response) -> str:
    return resp.json()["error"]["code"]


async def create(api, preset, **body):
    return await api.post(f"{P}/endpoints", json={"type": "github-pr", "preset_id": preset, **body})


# --- endpoints ----------------------------------------------------------------------------


async def test_endpoints_create_defaults_random_credentials_and_full_url(api, preset, settings):
    resp = await create(api, preset)
    assert resp.status_code == 201
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["url"] == f"https://hooks.example/webhook/{body['suffix']}"
    assert len(body["password"]) >= settings.min_password_length
    assert body["username"] and body["suffix"]
    assert body["type"] == "github-pr" and body["preset_id"] == preset
    assert body["credentials_rotated_at"] is None


async def test_endpoints_create_with_user_suffix_and_credentials(api, preset, conn):
    resp = await create(api, preset, suffix="my-hook", username="bob", password=PW_A)
    assert resp.status_code == 201
    body = resp.json()
    assert (body["suffix"], body["username"]) == ("my-hook", "bob")
    assert body["password"] == PW_A
    assert await endpoints.authenticate(conn, "my-hook", _basic("bob", PW_A))


def _basic(u: str, pw: str) -> str:
    import base64

    return "Basic " + base64.b64encode(f"{u}:{pw}".encode()).decode()


async def test_endpoints_create_taken_suffix_conflict(api, preset):
    assert (await create(api, preset, suffix="dup")).status_code == 201
    resp = await create(api, preset, suffix="dup")
    assert resp.status_code == 409
    assert code(resp) == "suffix_taken"


async def test_endpoints_list_and_get_never_carry_password_or_hash(api, preset):
    created = (await create(api, preset)).json()
    listed = await api.get(f"{P}/endpoints")
    assert listed.status_code == 200
    rows = listed.json()
    assert [r["id"] for r in rows] == [created["id"]]
    text = listed.text
    assert created["password"] not in text
    assert "password" not in rows[0] and "password_hash" not in text
    assert rows[0]["url"] == created["url"]


async def test_endpoints_create_with_valid_template_stored(api, preset):
    resp = await create(api, preset, prompt_template="Fix {{ title }}")
    assert resp.status_code == 201
    assert resp.json()["prompt_template"] == "Fix {{ title }}"


# --- invalid ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"suffix": "bad suffix/"},
        {"password": "short"},
        {"username": "a:b"},
        {"type": "nope"},
    ],
)
async def test_invalid_request_422(api, preset, body):
    resp = await create(api, preset, **body)
    assert resp.status_code == 422
    assert code(resp) == "invalid_request"


async def test_invalid_template_names_field_and_line(api, preset):
    resp = await create(api, preset, prompt_template="ok\n{% if %}")
    assert resp.status_code == 422
    assert code(resp) == "invalid_request"
    message = resp.json()["error"]["message"]
    assert "prompt_template" in message and "line 2" in message


async def test_invalid_unknown_filter_422(api, preset):
    resp = await create(api, preset, prompt_template="{{ title | nosuchfilter }}")
    assert resp.status_code == 422
    assert "prompt_template" in resp.json()["error"]["message"]


async def test_invalid_unknown_preset_422(api):
    resp = await create(api, "00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 422
    assert code(resp) == "unknown_preset"


async def test_invalid_missing_field_is_error_body(api):
    resp = await api.post(f"{P}/endpoints", json={"type": "github-pr"})
    assert resp.status_code == 422
    assert code(resp) == "invalid_request"


async def test_invalid_orchestrator_down_503(settings, pool, fake):
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("down", request=request)

    async with OrchestratorClient("http://orch", fake.token, transport=Down()) as orch:
        app = create_api_app(settings, pool, orch)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as c:
            resp = await create(c, str(uuid.uuid4()))
    assert resp.status_code == 503
    assert code(resp) == "orchestrator_unavailable"


class ServicePort(httpx.AsyncBaseTransport):
    """The fake behind the real service port's gate (agent_orchestrator.api.service_auth).

    A wrong bearer token gets 401 `unauthorized`; a preset path whose id is not a UUID is not
    in the allowlist and gets 403 `forbidden`.
    """

    UUID = re.compile(r"/api/v1/presets/[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

    def __init__(self, fake: FakeOrchestrator, token_sha256: str):
        self._inner = httpx.ASGITransport(app=fake.app)
        self._sha = token_sha256

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _, _, token = request.headers.get("authorization", "").partition(" ")
        if hashlib.sha256(token.encode()).hexdigest() != self._sha:
            return _gate(401, "unauthorized", "a valid service token is required")
        path = request.url.path
        if path.startswith("/api/v1/presets/") and not self.UUID.fullmatch(path):
            return _gate(403, "forbidden", "route is not served on the service port")
        return await self._inner.handle_async_request(request)


def _gate(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


async def test_preset_id_validation_and_orchestrator_auth_errors(settings, pool, fake, preset):
    sha = hashlib.sha256(fake.token.encode()).hexdigest()

    async def post(token: str, preset_id: str) -> httpx.Response:
        transport = ServicePort(fake, sha)
        async with OrchestratorClient("http://orch", token, transport=transport) as orch:
            app = create_api_app(settings, pool, orch)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://api"
            ) as c:
                return await create(c, preset_id)

    for bad in ("default", "not-a-uuid", "../presets", ""):
        resp = await post(fake.token, bad)
        assert resp.status_code == 422, bad
        assert code(resp) == "unknown_preset", bad
        assert "preset_id" in resp.json()["error"]["message"]

    resp = await post(fake.token, str(uuid.uuid4()))
    assert resp.status_code == 422 and code(resp) == "unknown_preset"

    resp = await post("revoked-token", preset)
    assert resp.status_code == 502
    assert code(resp) == "orchestrator_auth"

    resp = await post(fake.token, preset)
    assert resp.status_code == 201


# --- rotate / delete ----------------------------------------------------------------------


async def test_rotate_new_credentials_old_stop_authenticating(api, preset, conn):
    created = (await create(api, preset, password=PW_B)).json()
    resp = await api.post(f"{P}/endpoints/{created['id']}/rotate", json={})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    new = resp.json()
    assert new["password"] != created["password"]
    assert new["credentials_rotated_at"] is not None
    assert (
        await endpoints.authenticate(
            conn, created["suffix"], _basic(created["username"], created["password"])
        )
        is None
    )
    assert await endpoints.authenticate(
        conn, created["suffix"], _basic(new["username"], new["password"])
    )


async def test_rotate_given_credentials_and_short_password(api, preset):
    created = (await create(api, preset)).json()
    url = f"{P}/endpoints/{created['id']}/rotate"
    ok = await api.post(url, json={"username": "carol", "password": PW_C})
    assert ok.json()["username"] == "carol"
    bad = await api.post(url, json={"password": "x"})
    assert bad.status_code == 422 and code(bad) == "invalid_request"


async def test_rotate_unknown_404(api):
    resp = await api.post(f"{P}/endpoints/9999/rotate", json={})
    assert resp.status_code == 404 and code(resp) == "not_found"


async def test_rotate_without_body_ok(api, preset):
    created = (await create(api, preset)).json()
    resp = await api.post(f"{P}/endpoints/{created['id']}/rotate")
    assert resp.status_code == 200


async def test_delete_removes_endpoint_events_stay_listed(api, preset, conn):
    created = (await create(api, preset)).json()
    ep = await endpoints.get(conn, created["id"])
    await events.record(conn, ep, ev(), {"a": 1}, ignore_reason=None, max_queue=10)
    resp = await api.delete(f"{P}/endpoints/{created['id']}")
    assert resp.status_code == 204
    assert (await api.get(f"{P}/endpoints")).json() == []
    listed = (await api.get(f"{P}/events")).json()
    assert len(listed) == 1 and listed[0]["endpoint_suffix"] == created["suffix"]
    assert listed[0]["endpoint_id"] is None


async def test_delete_unknown_404(api):
    resp = await api.delete(f"{P}/endpoints/9999")
    assert resp.status_code == 404 and code(resp) == "not_found"


# --- events -------------------------------------------------------------------------------


async def seed(conn, preset, n=5, **kw):
    ep, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id=preset, **kw), min_password_length=16
    )
    ids = []
    for i in range(n):
        r = await events.record(
            conn,
            ep,
            ev(key=f"github/o/r/pr/{i}"),
            {"secret-payload": i},
            ignore_reason=None,
            max_queue=100,
        )
        ids.append(r.id)
    return ep, ids


async def test_events_newest_first_with_pagination(api, conn, preset):
    _, ids = await seed(conn, preset)
    page = (await api.get(f"{P}/events", params={"limit": 2})).json()
    assert [e["id"] for e in page] == [ids[4], ids[3]]
    nxt = (await api.get(f"{P}/events", params={"limit": 2, "before_id": ids[3]})).json()
    assert [e["id"] for e in nxt] == [ids[2], ids[1]]


async def test_events_limit_bounds(api):
    assert (await api.get(f"{P}/events", params={"limit": 201})).status_code == 422
    assert (await api.get(f"{P}/events", params={"limit": 0})).status_code == 422


async def test_events_filters_status_and_endpoint(api, conn, preset):
    ep, ids = await seed(conn, preset, 2)
    ep2, ids2 = await seed(conn, preset, 1)
    await events.record(conn, ep2, ev(key="x"), {}, ignore_reason="self_event", max_queue=100)
    by_ep = (await api.get(f"{P}/events", params={"endpoint_id": ep.id})).json()
    assert {e["id"] for e in by_ep} == set(ids)
    ignored = (await api.get(f"{P}/events", params={"status": "ignored"})).json()
    assert [e["endpoint_id"] for e in ignored] == [ep2.id]
    both = (await api.get(f"{P}/events", params={"status": "new", "endpoint_id": ep2.id})).json()
    assert [e["id"] for e in both] == ids2


async def test_events_list_has_no_payload_detail_has(api, conn, preset):
    _, ids = await seed(conn, preset, 1)
    listed = await api.get(f"{P}/events")
    assert "payload" not in listed.json()[0] and "secret-payload" not in listed.text
    detail = (await api.get(f"{P}/events/{ids[0]}")).json()
    assert detail["payload"] == {"secret-payload": 0}
    assert detail["labels"] == ["a"]
    assert detail["status"] == "new" and detail["id"] == ids[0]


async def test_events_unknown_404(api):
    resp = await api.get(f"{P}/events/9999")
    assert resp.status_code == 404 and code(resp) == "not_found"


# --- endpoint filters ---------------------------------------------------------------------

DENY_ALICE = {"deny": [{"field": "author", "values": ["alice"]}]}
CANONICAL_DENY_ALICE = {
    "allow": [],
    "deny": [{"field": "author", "match": "equals", "values": ["alice"]}],
}


async def test_create_endpoint_with_filters(api, preset):
    resp = await create(api, preset, filters=DENY_ALICE)
    assert resp.status_code == 201, resp.text
    assert resp.json()["filters"] == CANONICAL_DENY_ALICE
    listed = (await api.get(f"{P}/endpoints")).json()
    assert listed[0]["filters"] == CANONICAL_DENY_ALICE
    plain = await create(api, preset)
    assert plain.json()["filters"] == {"allow": [], "deny": []}
    listed = (await api.get(f"{P}/endpoints")).json()
    assert listed[1]["filters"] == {"allow": [], "deny": []}


async def test_create_endpoint_invalid_filters_422(api, preset, conn):
    resp = await create(api, preset, filters={"deny": [{"field": "nosuchfield", "values": ["x"]}]})
    assert resp.status_code == 422
    assert code(resp) == "invalid_request"
    assert "filters.deny[0]" in resp.json()["error"]["message"]
    cur = await conn.execute("SELECT count(*) FROM endpoints")
    assert await cur.fetchone() == (0,)


async def test_put_filters_replaces(api, preset, caplog):
    created = (await create(api, preset, filters=DENY_ALICE)).json()
    new = {"allow": [{"field": "repo", "match": "regex", "values": ["^o/"]}]}
    with caplog.at_level("INFO"):
        resp = await api.put(f"{P}/endpoints/{created['id']}/filters", json=new)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["filters"] == {
        "allow": [{"field": "repo", "match": "regex", "values": ["^o/"]}],
        "deny": [],
    }
    assert "password" not in body
    assert f"api: endpoint filters replaced id={created['id']}" in caplog.text
    assert "^o/" not in caplog.text
    listed = (await api.get(f"{P}/endpoints")).json()
    assert listed[0]["filters"] == body["filters"]


async def test_put_filters_errors(api, preset):
    missing = await api.put(f"{P}/endpoints/999/filters", json=DENY_ALICE)
    assert missing.status_code == 404
    assert code(missing) == "not_found"
    created = (
        await api.post(f"{P}/endpoints", json={"type": "grafana-alert", "preset_id": preset})
    ).json()
    bad = await api.put(
        f"{P}/endpoints/{created['id']}/filters",
        json={"deny": [{"field": "author", "values": ["alice"]}]},
    )
    assert bad.status_code == 422
    assert code(bad) == "invalid_request"
    assert "filters.deny[0]" in bad.json()["error"]["message"]


async def test_preview_reports_filters(api, conn):
    example = p.gh_pull_request()

    async def preview(filters):
        return await api.post(
            f"{P}/templates/preview",
            json={"type": "github-pr", "example": example, "filters": filters},
        )

    passed = await preview({"deny": [{"field": "author", "values": ["bob"]}]})
    assert passed.status_code == 200, passed.text
    assert passed.json() == {"prompt": None, "filters": {"passed": True, "reason": None}}
    denied = await preview(DENY_ALICE)
    assert denied.json() == {
        "prompt": None,
        "filters": {"passed": False, "reason": "filter_denied: author"},
    }
    invalid = await preview({"deny": [{"field": "nosuchfield", "values": ["x"]}]})
    assert invalid.status_code == 422
    assert "filters.deny[0]" in invalid.json()["error"]["message"]
    both = await api.post(
        f"{P}/templates/preview",
        json={
            "type": "github-pr",
            "example": example,
            "filters": DENY_ALICE,
            "template": "{{ title }}",
        },
    )
    assert both.json()["prompt"] == "Fix it"
    assert both.json()["filters"]["passed"] is False
    cur = await conn.execute(
        "SELECT (SELECT count(*) FROM endpoints), (SELECT count(*) FROM events)"
    )
    assert await cur.fetchone() == (0, 0)


async def test_preview_requires_template_or_filters(api):
    resp = await api.post(
        f"{P}/templates/preview", json={"type": "github-pr", "example": p.gh_pull_request()}
    )
    assert resp.status_code == 422
    assert code(resp) == "invalid_request"
    assert resp.json()["error"]["message"] == "template or filters: required"


async def test_meta_reports_filter_fields(api):
    body = (await api.get(f"{P}/meta")).json()
    assert body["filter_fields"] == {k: list(v) for k, v in filter_engine.FIELDS.items()}


# --- templates preview ----------------------------------------------------------------------


async def test_preview_renders_normalized_example_without_storing(api, conn):
    resp = await api.post(
        f"{P}/templates/preview",
        json={
            "template": "PR {{ number }} {{ title }} by {{ author }}",
            "type": "github-pr",
            "example": p.gh_pull_request(),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"prompt": "PR 7 Fix it by alice", "filters": None}
    cur = await conn.execute(
        "SELECT (SELECT count(*) FROM endpoints), (SELECT count(*) FROM events)"
    )
    assert await cur.fetchone() == (0, 0)


async def test_preview_from_json_filter_parses_example_text(api):
    example = p.gh_issues()
    example["issue"]["body"] = '{"k": "v"}'
    resp = await api.post(
        f"{P}/templates/preview",
        json={
            "template": "{{ (body | from_json).k }} {{ payload.action }}",
            "type": "github-issue",
            "example": example,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"prompt": "v opened", "filters": None}


@pytest.mark.parametrize(
    "type_,example",
    [
        ("gitlab-mr", p.gl_merge_request()),
        ("gitlab-issue", p.gl_issue()),
        ("github-issue", p.gh_issues()),
        ("grafana-alert", p.grafana_alert()),
    ],
)
async def test_preview_each_type(api, type_, example):
    resp = await api.post(
        f"{P}/templates/preview",
        json={"template": "{{ title }}", "type": type_, "example": example},
    )
    assert resp.status_code == 200, resp.text
    assert "prompt" in resp.json()


async def test_preview_syntax_error_422_with_line(api):
    resp = await api.post(
        f"{P}/templates/preview",
        json={"template": "a\n{% if %}", "type": "github-pr", "example": p.gh_pull_request()},
    )
    assert resp.status_code == 422 and code(resp) == "invalid_request"
    assert "line 2" in resp.json()["error"]["message"]


async def test_preview_unnormalizable_example_422(api):
    resp = await api.post(
        f"{P}/templates/preview",
        json={"template": "x", "type": "github-pr", "example": {"nothing": 1}},
    )
    assert resp.status_code == 422 and code(resp) == "invalid_request"


async def test_preview_render_error_422(api):
    resp = await api.post(
        f"{P}/templates/preview",
        json={
            "template": "{{ 1 / 0 }}",
            "type": "github-pr",
            "example": p.gh_pull_request(),
        },
    )
    assert resp.status_code == 422 and code(resp) == "invalid_request"


async def test_preview_unknown_type_422(api):
    resp = await api.post(
        f"{P}/templates/preview", json={"template": "x", "type": "nope", "example": {}}
    )
    assert resp.status_code == 422


# --- meta, health, routes -----------------------------------------------------------------------


async def test_meta_reports_base_url_prefix_types_limits(api, settings):
    body = (await api.get(f"{P}/meta")).json()
    assert body["public_base_url"] == "https://hooks.example"
    assert body["ingest_path_prefix"] == "/webhook/"
    assert body["types"] == list(endpoints.TYPES)
    assert body["limits"] == {
        "max_in_progress": settings.max_in_progress,
        "max_in_progress_per_endpoint": settings.max_in_progress_per_endpoint,
        "max_queue": settings.max_queue,
        "max_body_bytes": settings.max_body_bytes,
        "run_timeout_seconds": settings.run_timeout_seconds,
        "auth_failures_per_minute": settings.auth_failures_per_minute,
    }


async def test_health_and_ready(api):
    assert (await api.get("/healthz")).status_code == 200
    assert (await api.get("/readyz")).status_code == 200


async def test_routes_only_under_prefix_besides_health(settings, pool, orch):
    app = create_api_app(settings, pool, orch)
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    outside = {x for x in paths if not x.startswith(P) and x not in {"/healthz", "/readyz"}}
    assert outside == set()
    assert app.openapi()["paths"]
    assert app.openapi_url == f"{P}/openapi.json"


async def test_no_password_in_logs(api, preset, caplog):
    caplog.set_level("DEBUG")
    created = (await create(api, preset)).json()
    await api.post(f"{P}/endpoints/{created['id']}/rotate", json={})
    assert created["password"] not in caplog.text
    assert json.dumps("hash") not in caplog.text
