"""The public ingest app (design D3, D4, D12, D18): `POST /webhook/{suffix}` and health.

Per request, in order: the failed-auth limit (429, before any DB or hash work), basic auth
against the endpoint (401, identical for every failure), the body cap (413, read only after
auth so an anonymous caller cannot make the pod buffer a body), GitHub `ping` (202 pong),
JSON (400), normalize, classify and the endpoint's filters (a built-in reason wins; a filter
miss is stored ignored, `filter_error` when evaluation itself fails), record, NOTIFY,
202 `{id, status, reason}`. A DB failure is 503 so the forge retries; nothing else is 5xx.

Logs carry the endpoint id, event id, status, reason and client key; never the suffix of a
failed request, the body, the Authorization header or a password.
"""

import json
import logging
import math
from collections.abc import Mapping
from typing import Any

import psycopg
from agents_common.db import notify
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from agents_webhook import filters
from agents_webhook.db import endpoints, events
from agents_webhook.db.endpoints import Endpoint
from agents_webhook.ingest.ratelimit import FailureLimiter, client_key, parse_networks
from agents_webhook.normalize import NormalizedEvent, TypeMismatch, Unsupported, classify, normalize
from agents_webhook.settings import WebhookSettings

log = logging.getLogger(__name__)

CHANNEL = "webhook_events"
FILTER_ERROR = "filter_error"
REALM = 'Basic realm="agents-webhook"'
# Every DB failure the forge should retry: connection, pool and server errors, and a key
# whose `new` row kept being claimed. A DataError is the request's fault (400).
_DB_ERRORS = (psycopg.Error, events.RecordConflict)
# The kind an endpoint type stores when the payload cannot be normalised.
_KIND = {"github-pr": "mr", "github-issue": "issue", "gitlab-mr": "mr", "gitlab-issue": "issue"}


def _error(status: int, code: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": code.replace("_", " ")}},
        status_code=status,
        headers=dict(headers or {}),
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return headers.get(name.lower())


def _unparsed(endpoint: Endpoint, headers: Mapping[str, str], reason: str) -> NormalizedEvent:
    """The row shape of a delivery that normalize refused (stored ignored, never run)."""
    if _header(headers, "X-GitHub-Event"):
        source, delivery = "github", _header(headers, "X-GitHub-Delivery")
    elif _header(headers, "X-Gitlab-Event"):
        source, delivery = "gitlab", _header(headers, "X-Gitlab-Event-UUID")
    else:
        source, delivery = endpoint.type.split("-")[0], None
    return NormalizedEvent(
        type=endpoint.type,
        source=source,
        kind=_KIND.get(endpoint.type, "alert"),
        action=_header(headers, "X-GitHub-Event") or _header(headers, "X-Gitlab-Event") or "",
        repo=None,
        number=None,
        title="",
        body="",
        comment_body=None,
        url=None,
        author=None,
        actor=None,
        labels=[],
        delivery_id=delivery,
        dedup_key=f"{source}/{reason}",
        priority=1,
    )


def verdict(
    event: NormalizedEvent, rules: dict[str, Any], settings: WebhookSettings, context: str
) -> str | None:
    """The ignore reason for `event`, shared by ingest and the template preview.

    Built-in rules first, then the endpoint's filters; an evaluation failure fails closed and
    is logged by type with `context`.
    """
    reason = classify(event, bot_logins=settings.bot_logins)
    if reason is not None:
        return reason
    try:
        return filters.evaluate(event, rules)
    except Exception as exc:
        log.error("ingest: filter evaluation failed (%s) %s", type(exc).__name__, context)
        return FILTER_ERROR


class _TooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    """The raw body, raising _TooLarge as soon as it exceeds `limit` bytes."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > limit:
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise _TooLarge
    return bytes(body)


def create_ingest_app(settings: WebhookSettings, pool: AsyncConnectionPool) -> FastAPI:
    limiter = FailureLimiter(settings.auth_failures_per_minute)
    proxies = parse_networks(settings.trusted_proxies)
    app = FastAPI(title="agents-webhook ingest", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(StarletteHTTPException)
    async def _not_found(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Unknown paths and methods alike: the app serves only its three routes.
        return _error(404, "not_found")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except psycopg.Error as exc:
            log.warning("readyz: database unreachable (%s)", type(exc).__name__)
            return _error(503, "unavailable")
        return JSONResponse({"status": "ok"})

    @app.post("/webhook/{suffix}")
    async def webhook(suffix: str, request: Request) -> Response:
        peer = request.client.host if request.client else "unknown"
        client = client_key(peer, request.headers.get("x-forwarded-for"), proxies)
        retry = limiter.blocked(client)
        if retry is not None:
            log.warning("ingest: too many failed authentications client=%s", client)
            return _error(429, "too_many_requests", {"Retry-After": str(max(1, math.ceil(retry)))})
        try:
            async with pool.connection() as conn:
                endpoint = await endpoints.authenticate(
                    conn, suffix, request.headers.get("authorization")
                )
        except _DB_ERRORS as exc:
            log.error("ingest: authenticate failed (%s) client=%s", type(exc).__name__, client)
            return _error(503, "unavailable")
        if endpoint is None:
            limiter.record_failure(client)
            log.info("ingest: unauthorized client=%s", client)
            return _error(401, "unauthorized", {"WWW-Authenticate": REALM})
        return await _deliver(endpoint, request, client)

    async def _deliver(endpoint: Endpoint, request: Request, client: str) -> Response:
        try:
            raw = await _read_body(request, settings.max_body_bytes)
        except _TooLarge:
            log.info("ingest: body too large endpoint=%s client=%s", endpoint.id, client)
            return _error(413, "payload_too_large")
        except ClientDisconnect:
            return _error(400, "bad_request")
        headers = request.headers
        if endpoint.type.startswith("github") and _header(headers, "X-GitHub-Event") == "ping":
            log.info("ingest: ping endpoint=%s client=%s", endpoint.id, client)
            return JSONResponse({"status": "pong"}, status_code=202)
        try:
            payload: Any = json.loads(raw)
        except (ValueError, RecursionError):
            log.info("ingest: invalid JSON endpoint=%s client=%s", endpoint.id, client)
            return _error(400, "bad_request")
        try:
            event = normalize(endpoint.type, headers, payload)
            reason = verdict(
                event, endpoint.filters, settings, f"endpoint={endpoint.id} client={client}"
            )
        except TypeMismatch:
            event, reason = _unparsed(endpoint, headers, "type_mismatch"), "type_mismatch"
        except Unsupported:
            event, reason = _unparsed(endpoint, headers, "unsupported_event"), "unsupported_event"
        try:
            async with pool.connection() as conn:
                recorded = await events.record(
                    conn,
                    endpoint,
                    event,
                    payload,
                    ignore_reason=reason,
                    max_queue=settings.max_queue,
                )
                if recorded.status == "new":
                    await notify(conn, CHANNEL, str(recorded.id))
        except psycopg.DataError as exc:
            # Text Postgres cannot store (a NUL character): the request's fault, not the DB's.
            log.info(
                "ingest: unstorable payload (%s) endpoint=%s client=%s",
                type(exc).__name__,
                endpoint.id,
                client,
            )
            return _error(400, "bad_request")
        except _DB_ERRORS as exc:
            log.error(
                "ingest: record failed (%s) endpoint=%s client=%s",
                type(exc).__name__,
                endpoint.id,
                client,
            )
            return _error(503, "unavailable")
        log.info(
            "ingest: recorded endpoint=%s event=%s status=%s reason=%s client=%s",
            endpoint.id,
            recorded.id,
            recorded.status,
            recorded.reason,
            client,
        )
        return JSONResponse(
            {"id": recorded.id, "status": recorded.status, "reason": recorded.reason},
            status_code=202,
        )

    return app
