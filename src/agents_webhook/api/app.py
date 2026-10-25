"""The UI API app (design D7, D11, D12, D14, D17): everything under `/webhooks/api/v1`.

It sits behind SSO at the ingress; it is the only writer of `endpoints` and never writes
`events`. Errors are `{error: {code, message}}`. Logs never carry a password, a hash or an
Authorization header.
"""

import logging

import psycopg
from agents_common.orchestrator import OrchestratorClient
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException

from agents_webhook.api.routes import PREFIX, ApiError, build_router
from agents_webhook.settings import WebhookSettings

log = logging.getLogger(__name__)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_api_app(
    settings: WebhookSettings, pool: AsyncConnectionPool, client: OrchestratorClient
) -> FastAPI:
    app = FastAPI(
        title="agents-webhook API",
        openapi_url=f"{PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(build_router(settings, pool, client))

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        return _error(422, "invalid_request", f"{where}: {first.get('msg', 'invalid request')}")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "invalid_request"
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(psycopg.Error)
    async def _db(request: Request, exc: psycopg.Error) -> JSONResponse:
        log.error("api: database error (%s)", type(exc).__name__)
        return _error(503, "unavailable", "database unavailable")

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
            return _error(503, "unavailable", "database unavailable")
        return JSONResponse({"status": "ok"})

    return app
