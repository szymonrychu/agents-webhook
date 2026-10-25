"""The UI API app (design D7, D11, D12, D14, D17): everything under `/webhooks/api/v1`.

It sits behind SSO at the ingress; it is the only writer of `endpoints` and never writes
`events`. Errors are `{error: {code, message}}`. Logs never carry a password, a hash or an
Authorization header.
"""

import ipaddress
import logging

import psycopg
from agents_common.orchestrator import OrchestratorClient
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_webhook.api.routes import PREFIX, ApiError, build_router
from agents_webhook.settings import WebhookSettings

log = logging.getLogger(__name__)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


class SourceAllowlist:
    """403 for a direct socket peer outside the allowlist; X-Forwarded-For is never read.

    The oauth2-proxy sidecar is the only intended caller; its port-8080 Service port reaches
    this app directly, so without this any in-cluster pod could skip SSO. Probes stay open.
    """

    def __init__(self, app: ASGIApp, networks: list):
        self.app = app
        self.networks = list(networks)

    def allowed(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            addr = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr.version == n.version and addr in n for n in self.networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS or self.allowed(scope):
            await self.app(scope, receive, send)
            return
        await _error(403, "forbidden", "source address is not allowed")(scope, receive, send)


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
    app.add_middleware(SourceAllowlist, networks=settings.api_networks())

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
