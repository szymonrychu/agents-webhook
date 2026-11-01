"""The token-gated admin listener: the same CRUD router behind ServiceTokenGate (WH1)."""

import base64
import secrets

import httpx
import pytest
from agents_common.db import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient
from authhelp import BOX

from agents_webhook import cli
from agents_webhook.api.app import create_admin_app, create_api_app
from agents_webhook.settings import WebhookSettings

P = "/webhooks/api/v1"
TOKEN = secrets.token_urlsafe(24)
OTHER = secrets.token_urlsafe(24)


def make_settings(**over) -> WebhookSettings:
    return WebhookSettings(
        database_url="postgresql://unused",
        orchestrator_url="http://orchestrator",
        orchestrator_token="t",
        public_base_url="https://hooks.example/",
        **over,
    )


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
async def orch(fake):
    transport = httpx.ASGITransport(app=fake.app)
    async with OrchestratorClient("http://orch", fake.token, transport=transport) as c:
        yield c


@pytest.fixture
async def admin(pool, orch):
    app = create_admin_app(make_settings(), pool, orch, BOX, [TOKEN, OTHER])
    # a peer far outside the api allowlist: the admin listener is gated by token, not by CIDR
    transport = httpx.ASGITransport(app=app, client=("10.9.9.9", 4242))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_admin_requires_a_valid_token(admin, fake):
    preset = str(fake.add_preset("dev").id)
    assert (await admin.get(f"{P}/endpoints")).status_code == 401
    assert (await admin.get(f"{P}/endpoints", headers=bearer("wrong"))).status_code == 401
    assert (await admin.get(f"{P}/endpoints", headers=bearer(TOKEN))).status_code == 200
    assert (await admin.get(f"{P}/endpoints", headers=bearer(OTHER))).status_code == 200
    created = await admin.post(
        f"{P}/endpoints", json={"type": "github-pr", "preset_id": preset}, headers=bearer("x")
    )
    assert created.status_code == 401


async def test_admin_health_is_exempt(admin):
    assert (await admin.get("/healthz")).status_code == 200
    assert (await admin.get("/readyz")).status_code == 200


async def test_admin_crud_and_passwords_in_responses(admin, fake):
    preset = str(fake.add_preset("dev").id)
    h = bearer(TOKEN)
    resp = await admin.post(
        f"{P}/endpoints", json={"type": "github-pr", "preset_id": preset}, headers=h
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["password"]
    assert resp.headers["cache-control"] == "no-store"
    rotated = await admin.post(f"{P}/endpoints/{body['id']}/rotate", headers=h)
    assert rotated.status_code == 200
    assert rotated.json()["password"] and rotated.json()["password"] != body["password"]
    listed = await admin.get(f"{P}/endpoints", headers=h)
    assert [e["id"] for e in listed.json()] == [body["id"]]
    assert "password" not in listed.json()[0]
    assert (await admin.delete(f"{P}/endpoints/{body['id']}", headers=h)).status_code == 204
    assert (await admin.get(f"{P}/endpoints", headers=h)).json() == []


async def test_ui_app_stays_open_to_localhost_without_token(pool, orch):
    app = create_api_app(make_settings(), pool, orch, BOX)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4242))
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
        assert (await c.get(f"{P}/endpoints")).status_code == 200


def test_admin_settings_defaults():
    s = make_settings()
    assert s.admin_port == 8001
    assert s.admin_tokens_file == ""
    assert make_settings(admin_port=9000).admin_port == 9000


def test_build_admin_config(monkeypatch):
    settings = make_settings(admin_port=8123)
    config = cli.build_admin_config(settings, pool=None, client=None, box=None, tokens=[TOKEN])
    assert config.port == 8123
    assert config.host == settings.http_host
    assert config.proxy_headers is False
    assert config.app.title == "agents-webhook API"


def test_admin_tokens_loaded_from_file(tmp_path):
    f = tmp_path / "tokens"
    f.write_text(f"{TOKEN}\n\n{OTHER}\n")
    assert cli.load_admin_tokens(make_settings(admin_tokens_file=str(f))) == [TOKEN, OTHER]
    assert cli.load_admin_tokens(make_settings()) is None
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(ValueError):
        cli.load_admin_tokens(make_settings(admin_tokens_file=str(empty)))


def test_admin_port_must_differ_from_http_port():
    with pytest.raises(ValueError):
        make_settings(admin_port=8000, admin_tokens_file="/x")


def _fake_serving(monkeypatch):
    ports: list[int] = []

    class Pool:
        async def close(self):
            pass

    async def fake_open_pool(url, *a, **k):
        return Pool()

    class FakeServer:
        def __init__(self, config):
            ports.append(config.port)
            self.should_exit = False

        async def serve(self):
            pass

    monkeypatch.setattr(cli, "open_pool", fake_open_pool)
    monkeypatch.setattr(cli, "_Server", FakeServer)
    return ports


def _env(monkeypatch, tmp_path, **extra):
    key_file = tmp_path / "endpoint-key"
    key_file.write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    base = {
        "WEBHOOK_ENDPOINT_KEY_FILE": str(key_file),
        "WEBHOOK_DATABASE_URL": "postgresql://unused",
        "WEBHOOK_ORCHESTRATOR_URL": "http://orch:8001",
        "WEBHOOK_ORCHESTRATOR_TOKEN": "t",
        **extra,
    }
    for key in list(__import__("os").environ):
        if key.startswith("WEBHOOK_"):
            monkeypatch.delenv(key)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


def test_api_serves_both_ports_when_tokens_are_configured(tmp_path, monkeypatch):
    f = tmp_path / "tokens"
    f.write_text(TOKEN + "\n")
    _env(monkeypatch, tmp_path, WEBHOOK_ADMIN_TOKENS_FILE=str(f))
    ports = _fake_serving(monkeypatch)
    assert cli.main(["api"]) == 0
    assert sorted(ports) == [8000, 8001]


def test_api_serves_only_the_ui_port_without_tokens(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    ports = _fake_serving(monkeypatch)
    assert cli.main(["api"]) == 0
    assert ports == [8000]


def test_api_with_unreadable_tokens_file_exits_78(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path, WEBHOOK_ADMIN_TOKENS_FILE=str(tmp_path / "missing"))
    _fake_serving(monkeypatch)
    assert cli.main(["api"]) == 78
    assert "WEBHOOK_ADMIN_TOKENS_FILE" in capsys.readouterr().err


def test_api_without_a_valid_endpoint_key_exits_78(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path, WEBHOOK_ENDPOINT_KEY_FILE=str(tmp_path / "missing"))
    _fake_serving(monkeypatch)
    assert cli.main(["api"]) == 78
    assert "WEBHOOK_ENDPOINT_KEY_FILE" in capsys.readouterr().err


async def test_admin_new_routes_behave_like_the_ui_listener(admin, fake):
    preset = str(fake.add_preset("dev").id)
    h = bearer(TOKEN)
    created = await admin.post(
        f"{P}/endpoints",
        json={
            "type": "github-pr",
            "preset_id": preset,
            "auth_mode": "header",
            "auth_header": "X-T",
        },
        headers=h,
    )
    assert created.status_code == 201 and created.json()["secret"]
    eid = created.json()["id"]
    patched = await admin.patch(f"{P}/endpoints/{eid}", json={"suffix": "via-admin"}, headers=h)
    assert patched.status_code == 200 and patched.json()["suffix"] == "via-admin"
    assert patched.json()["secret"] is None
    changed = await admin.patch(f"{P}/endpoints/{eid}", json={"auth_mode": "bearer"}, headers=h)
    assert changed.status_code == 200 and changed.json()["secret"]
    assert changed.headers["cache-control"] == "no-store"
    rotated = await admin.post(f"{P}/endpoints/{eid}/rotate", headers=h)
    assert rotated.status_code == 200 and rotated.json()["secret"]
    cloned = await admin.post(f"{P}/endpoints/{eid}/clone", headers=h)
    assert cloned.status_code == 201 and cloned.json()["secret"]
    assert cloned.headers["cache-control"] == "no-store"
    listed = (await admin.get(f"{P}/endpoints", headers=h)).json()
    assert len(listed) == 2 and all("secret" not in e and e["has_secret"] for e in listed)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", f"{P}/endpoints/1"),
        ("POST", f"{P}/endpoints/1/clone"),
        ("POST", f"{P}/endpoints/1/rotate"),
    ],
)
async def test_admin_new_routes_require_the_token(admin, method, path):
    assert (await admin.request(method, path)).status_code == 401
    assert (await admin.request(method, path, headers=bearer("wrong"))).status_code == 401
