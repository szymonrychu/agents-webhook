"""README API coverage and the committed OpenAPI document (design D14)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from agents_webhook.api import openapi
from agents_webhook.api.app import create_api_app
from agents_webhook.api.schemas import EventView
from agents_webhook.settings import WebhookSettings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
PREFIX = "/webhooks/api/v1"


def section(title: str) -> str:
    """The body of the `## title` section (up to the next H2)."""
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _walk(router, prefix: str = ""):
    """(path, methods) of every route; FastAPI includes routers lazily, so descend into them."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.methods
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router, prefix + route.include_context.prefix)


def app_routes() -> set[str]:
    settings = WebhookSettings(
        database_url="postgresql://unused/db",
        orchestrator_url="http://orchestrator",
        orchestrator_token="t",
    )
    app = create_api_app(settings, None, None)
    out = set()
    for path, methods in _walk(app):
        if path.startswith(PREFIX):
            out.update(f"{m} {path}" for m in methods - {"HEAD"})
    return out


def readme_routes() -> set[str]:
    found = re.findall(
        rf"^### `(GET|POST|PATCH|PUT|DELETE) ({re.escape(PREFIX)}\S*)`\s*$",
        section("API"),
        re.MULTILINE,
    )
    return {f"{m} {p}" for m, p in found}


def test_every_route_documented() -> None:
    expected = app_routes()
    assert len(expected) >= 8
    missing = expected - readme_routes()
    assert not missing, f"undocumented routes: {sorted(missing)}"


def test_no_stale_route_in_readme() -> None:
    stale = readme_routes() - app_routes()
    assert not stale, f"README documents routes the app lacks: {sorted(stale)}"


def test_every_route_has_curl_example() -> None:
    api = section("API")
    for route in app_routes():
        method, path = route.split(" ", 1)
        block = api.split(f"### `{route}`", 1)[1].split("\n### ", 1)[0]
        assert "curl" in block, f"no curl example for {route}"
        assert path.removeprefix(PREFIX).split("{")[0].rstrip("/") in block


def test_openapi_json_drift() -> None:
    committed = (ROOT / "openapi.json").read_text(encoding="utf-8")
    assert committed == openapi.export(), (
        "run: uv run python -m agents_webhook.api.openapi > openapi.json"
    )
    assert committed.endswith("\n")


def test_template_language_documented() -> None:
    text = section("API") + README
    for needle in (
        "sandbox",
        "from_json",
        "template_error",
        "templates/preview",
        "StrictUndefined",
        "payload",
        "comment_body",
        "labels",
        "SKIP_JWT_BEARER_TOKENS",
        "port-forward",
    ):
        assert needle in text, f"README does not mention {needle}"


def test_event_view_fields_documented() -> None:
    block = section("API").split("### `GET /webhooks/api/v1/events`", 1)[1].split("\n### ", 1)[0]
    assert "An event view carries" in block, "README does not list the event view fields"
    documented = re.findall(r"`([a-z_]+)`", block.split("An event view carries", 1)[1])
    assert set(EventView.model_fields) <= set(documented), "README event view fields drifted"
    assert not [f for f in documented if "cluster" in f]
