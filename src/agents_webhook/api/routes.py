"""Routes of the UI API: endpoints, events, template preview and meta."""

import logging
from typing import Annotated, Any

from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from fastapi import APIRouter, Query, Response
from psycopg_pool import AsyncConnectionPool

from agents_webhook import filters, prompt
from agents_webhook.api import schemas
from agents_webhook.db import endpoints, events
from agents_webhook.db.endpoints import Endpoint, SuffixTaken
from agents_webhook.normalize import TypeMismatch, Unsupported, normalize
from agents_webhook.settings import WebhookSettings

log = logging.getLogger(__name__)

PREFIX = "/webhooks/api/v1"
INGEST_PREFIX = "/webhook/"
NO_STORE = {"Cache-Control": "no-store"}
# The provider header `normalize` needs to recognise an example of each type.
_PREVIEW_HEADERS = {
    "github-pr": {"X-GitHub-Event": "pull_request"},
    "github-issue": {"X-GitHub-Event": "issues"},
    "gitlab-mr": {"X-Gitlab-Event": "Merge Request Hook"},
    "gitlab-issue": {"X-Gitlab-Event": "Issue Hook"},
    "grafana-alert": {},
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _invalid(message: str) -> ApiError:
    return ApiError(422, "invalid_request", message)


def build_router(
    settings: WebhookSettings, pool: AsyncConnectionPool, client: OrchestratorClient
) -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    def view(endpoint: Endpoint) -> dict[str, Any]:
        return {
            **vars(endpoint),
            "url": f"{settings.public_base_url}{INGEST_PREFIX}{endpoint.suffix}",
        }

    def check_template(template: str | None) -> None:
        if template is None:
            return
        try:
            prompt.validate_template(template)
        except ValueError as exc:
            raise _invalid(f"prompt_template: {exc}") from exc

    @router.get("/endpoints", response_model=list[schemas.EndpointView])
    async def list_endpoints() -> list[dict[str, Any]]:
        async with pool.connection() as conn:
            return [view(e) for e in await endpoints.list_all(conn)]

    @router.post("/endpoints", status_code=201, response_model=schemas.EndpointCreated)
    async def create_endpoint(body: schemas.EndpointIn, response: Response) -> dict[str, Any]:
        check_template(body.prompt_template)
        try:
            await client.get_preset(body.preset_id)
        except NotFound as exc:
            raise ApiError(
                422, "unknown_preset", f"preset_id: unknown preset {body.preset_id}"
            ) from exc
        except OrchestratorError as exc:
            if exc.status in (401, 403):
                log.error("api: orchestrator refused the service token (%s)", exc.status)
                raise ApiError(
                    502, "orchestrator_auth", "orchestrator rejected the service credentials"
                ) from exc
            log.warning("api: orchestrator unavailable (%s)", type(exc).__name__)
            raise ApiError(503, "orchestrator_unavailable", "orchestrator unavailable") from exc
        except OrchestratorUnavailable as exc:
            log.warning("api: orchestrator unavailable (%s)", type(exc).__name__)
            raise ApiError(503, "orchestrator_unavailable", "orchestrator unavailable") from exc
        data = endpoints.EndpointIn(**body.model_dump())
        try:
            async with pool.connection() as conn:
                endpoint, creds = await endpoints.create(
                    conn, data, min_password_length=settings.min_password_length
                )
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        except SuffixTaken as exc:
            raise ApiError(409, "suffix_taken", f"suffix: already taken: {exc.suffix}") from exc
        response.headers.update(NO_STORE)
        log.info("api: endpoint created id=%s type=%s", endpoint.id, endpoint.type)
        return {**view(endpoint), "password": creds.password}

    @router.put("/endpoints/{id}/filters", response_model=schemas.EndpointView)
    async def put_filters(id: int, body: schemas.Filters) -> dict[str, Any]:
        try:
            async with pool.connection() as conn:
                endpoint = await endpoints.set_filters(conn, id, body.model_dump())
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        if endpoint is None:
            raise ApiError(404, "not_found", "endpoint not found")
        log.info("api: endpoint filters replaced id=%s", id)
        return view(endpoint)

    @router.post("/endpoints/{id}/rotate", response_model=schemas.EndpointCreated)
    async def rotate_endpoint(
        id: int, response: Response, body: schemas.RotateIn | None = None
    ) -> dict[str, Any]:
        body = body or schemas.RotateIn()
        try:
            async with pool.connection() as conn:
                creds = await endpoints.rotate(
                    conn,
                    id,
                    username=body.username,
                    password=body.password,
                    min_password_length=settings.min_password_length,
                )
                endpoint = await endpoints.get(conn, id) if creds else None
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        if creds is None or endpoint is None:
            raise ApiError(404, "not_found", "endpoint not found")
        response.headers.update(NO_STORE)
        log.info("api: endpoint credentials rotated id=%s", id)
        return {**view(endpoint), "username": creds.username, "password": creds.password}

    @router.delete("/endpoints/{id}", status_code=204)
    async def delete_endpoint(id: int) -> Response:
        async with pool.connection() as conn:
            deleted = await endpoints.delete(conn, id)
        if not deleted:
            raise ApiError(404, "not_found", "endpoint not found")
        log.info("api: endpoint deleted id=%s", id)
        return Response(status_code=204)

    @router.get("/events", response_model=list[schemas.EventView])
    async def list_events(
        status: str | None = None,
        endpoint_id: int | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before_id: int | None = None,
    ) -> list[events.Event]:
        async with pool.connection() as conn:
            return await events.list_events(
                conn, status=status, endpoint_id=endpoint_id, limit=limit, before_id=before_id
            )

    @router.get("/events/{id}", response_model=schemas.EventDetail)
    async def get_event(id: int) -> events.Event:
        async with pool.connection() as conn:
            event = await events.get_event(conn, id)
        if event is None:
            raise ApiError(404, "not_found", "event not found")
        return event

    @router.post("/templates/preview", response_model=schemas.PreviewOut)
    async def preview_template(body: schemas.PreviewIn) -> dict[str, Any]:
        """Render a template and/or evaluate filters against an example; nothing is stored."""
        if body.template is None and body.filters is None:
            raise _invalid("template or filters: required")
        if body.type not in endpoints.TYPES:
            raise _invalid(f"type: must be one of {', '.join(endpoints.TYPES)}")
        try:
            event = normalize(body.type, _PREVIEW_HEADERS[body.type], body.example)
        except (TypeMismatch, Unsupported) as exc:
            raise _invalid(f"example: not a {body.type} payload ({exc})") from exc
        result = None
        if body.filters is not None:
            try:
                canonical = filters.validate(body.type, body.filters.model_dump())
            except ValueError as exc:
                raise _invalid(str(exc)) from exc
            reason = filters.evaluate(event, canonical)
            result = {"passed": reason is None, "reason": reason}
        text = None
        if body.template is not None:
            payload = body.example if isinstance(body.example, dict) else None
            try:
                text = await prompt.preview_async(
                    body.template,
                    payload,
                    event,
                    timeout=settings.template_timeout_seconds,
                    memory_mb=settings.template_memory_mb,
                )
            except (ValueError, prompt.TemplateError) as exc:
                raise _invalid(f"template: {exc}") from exc
        return {"prompt": text, "filters": result}

    @router.get("/meta", response_model=schemas.Meta)
    async def meta() -> dict[str, Any]:
        return {
            "public_base_url": settings.public_base_url,
            "ingest_path_prefix": INGEST_PREFIX,
            "types": list(endpoints.TYPES),
            "filter_fields": {name: list(fields) for name, fields in filters.FIELDS.items()},
            "limits": {name: getattr(settings, name) for name in schemas.Limits.model_fields},
        }

    return router
