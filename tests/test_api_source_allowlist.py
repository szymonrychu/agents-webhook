"""The api answers only peers inside WEBHOOK_API_ALLOWED_CIDRS (direct peer, never XFF)."""

import httpx
import pytest
from pydantic import ValidationError

from agents_webhook.api.app import create_api_app
from agents_webhook.settings import WebhookSettings

OPENAPI = "/webhooks/api/v1/openapi.json"


def make(cidrs: str | None = None) -> WebhookSettings:
    extra = {} if cidrs is None else {"api_allowed_cidrs": cidrs}
    return WebhookSettings(
        database_url="postgresql://unused",
        orchestrator_url="http://orchestrator",
        orchestrator_token="t",
        **extra,
    )


def client(settings: WebhookSettings, peer: str) -> httpx.AsyncClient:
    app = create_api_app(settings, None, None)
    transport = httpx.ASGITransport(app=app, client=(peer, 4242))
    return httpx.AsyncClient(transport=transport, base_url="http://api")


async def test_api_rejects_peer_outside_allowed_cidrs():
    async with client(make("127.0.0.1/32"), "10.1.2.3") as c:
        resp = await c.get(OPENAPI, headers={"X-Forwarded-For": "127.0.0.1"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"
        assert (await c.get("/healthz")).status_code == 200
    async with client(make("127.0.0.1/32"), "127.0.0.1") as c:
        assert (await c.get(OPENAPI)).status_code == 200


async def test_api_allowlist_accepts_any_listed_cidr_and_mapped_ipv4():
    async with client(make("10.0.0.0/8, 127.0.0.1/32"), "10.9.9.9") as c:
        assert (await c.get(OPENAPI)).status_code == 200
    async with client(make("127.0.0.1/32"), "::ffff:127.0.0.1") as c:
        assert (await c.get(OPENAPI)).status_code == 200


def test_api_allowed_cidrs_default_and_validation():
    assert make().api_allowed_cidrs == ["127.0.0.1/32"]
    with pytest.raises(ValidationError):
        make("not-a-cidr")
