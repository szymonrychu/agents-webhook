"""Command line entrypoint: migrate, ingest, api and worker subcommands."""

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence

import uvicorn
from agents_common.db.migrate import run_migrations
from agents_common.db.pool import open_pool
from agents_common.orchestrator import OrchestratorClient
from agents_common.runtime import run_service
from agents_common.service_gate import load_tokens
from agents_common.settings import SettingsError, load_settings

from agents_webhook import __version__
from agents_webhook.api.app import create_admin_app, create_api_app
from agents_webhook.ingest.app import create_ingest_app
from agents_webhook.settings import LocalSettings, WebhookSettings
from agents_webhook.worker import run_worker

EXIT_BAD_SETTINGS = 78
KEEP_ALIVE_SECONDS = 5
H11_MAX_INCOMPLETE_EVENT_SIZE = 16 * 1024
BODY_READ_TIMEOUT_SECONDS = 10.0

SUBCOMMANDS = {
    "migrate": "apply database migrations",
    "ingest": "run the webhook ingest listener",
    "api": "run the HTTP API",
    "worker": "run the queue worker",
}

log = logging.getLogger("agents_webhook")


class BodyReadTimeout:
    """ASGI wrapper: a request body that stalls longer than `seconds` is answered 408."""

    def __init__(self, app, seconds: float):
        self.app = app
        self.seconds = seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def guarded_receive():
            try:
                async with asyncio.timeout(self.seconds):
                    return await receive()
            except TimeoutError:
                raise _BodyTimeout from None

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except _BodyTimeout:
            if not started:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 408,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"connection", b"close"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": _TIMEOUT_BODY})


_TIMEOUT_BODY = json.dumps(
    {"error": {"code": "request_timeout", "message": "request timeout"}}
).encode()


class _BodyTimeout(Exception):
    pass


class _Server(uvicorn.Server):
    # uvicorn re-raises a captured signal after a graceful stop; run_service owns the signals.
    def capture_signals(self):
        return contextlib.nullcontext()


def _base_config(
    app, settings: WebhookSettings, *, port: int | None = None, **extra
) -> uvicorn.Config:
    # Proxy headers stay off: the peer address is the TCP peer, X-Forwarded-For cannot spoof it.
    return uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port if port is None else port,
        proxy_headers=False,
        log_config=None,
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
        **extra,
    )


def build_ingest_config(settings: WebhookSettings, pool) -> uvicorn.Config:
    """Public listener: no access log (it would print the suffix), tight connection limits."""
    app = BodyReadTimeout(create_ingest_app(settings, pool), BODY_READ_TIMEOUT_SECONDS)
    return _base_config(
        app,
        settings,
        access_log=False,
        http="h11",
        h11_max_incomplete_event_size=H11_MAX_INCOMPLETE_EVENT_SIZE,
        timeout_keep_alive=KEEP_ALIVE_SECONDS,
    )


def build_api_config(settings: WebhookSettings, pool, client) -> uvicorn.Config:
    return _base_config(create_api_app(settings, pool, client), settings)


def build_admin_config(
    settings: WebhookSettings, pool, client, tokens: Sequence[str]
) -> uvicorn.Config:
    """Service listener: the UI routes behind a bearer token, on its own port."""
    return _base_config(
        create_admin_app(settings, pool, client, tokens), settings, port=settings.admin_port
    )


def load_admin_tokens(settings: WebhookSettings) -> list[str] | None:
    """The accepted admin tokens, or None when the admin listener is not configured."""
    if not settings.admin_tokens_file:
        return None
    return load_tokens(settings.admin_tokens_file)


def _client(settings: WebhookSettings) -> OrchestratorClient:
    return OrchestratorClient(
        settings.orchestrator_url, settings.orchestrator_token.get_secret_value()
    )


def _run(settings: WebhookSettings, body: Callable[..., Awaitable[None]], *, client: bool) -> int:
    """Open the pool (and orchestrator client), run `body(pool, client, stop)`, close both."""

    async def main(stop: asyncio.Event) -> None:
        pool = await open_pool(settings.database_url.get_secret_value())
        orch = _client(settings) if client else None
        try:
            await body(pool, orch, stop)
        finally:
            if orch is not None:
                await orch.aclose()
            await pool.close()

    return asyncio.run(run_service(main, grace_seconds=settings.shutdown_grace_seconds))


async def _serve(config: uvicorn.Config, stop: asyncio.Event) -> None:
    server = _Server(config)

    async def watch() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()


def _cmd_migrate(settings: WebhookSettings) -> int:
    applied = run_migrations(settings.database_url.get_secret_value(), "agents_webhook")
    log.info("migrations applied: %s", ", ".join(applied) or "none")
    return 0


def _cmd_ingest(settings: WebhookSettings) -> int:
    async def body(pool, _client, stop):
        await _serve(build_ingest_config(settings, pool), stop)

    return _run(settings, body, client=False)


def _cmd_api(settings: WebhookSettings) -> int:
    try:
        tokens = load_admin_tokens(settings)
    except (OSError, ValueError) as exc:
        print(f"WEBHOOK_ADMIN_TOKENS_FILE: {exc}", file=sys.stderr)
        return EXIT_BAD_SETTINGS

    async def body(pool, client, stop):
        async with asyncio.TaskGroup() as group:
            group.create_task(_serve(build_api_config(settings, pool, client), stop))
            if tokens is not None:
                config = build_admin_config(settings, pool, client, tokens)
                group.create_task(_serve(config, stop))

    return _run(settings, body, client=True)


def _cmd_worker(settings: WebhookSettings) -> int:
    async def body(pool, client, stop):
        await run_worker(settings, pool, client, stop)

    return _run(settings, body, client=True)


LOCAL_COMMANDS = {"migrate", "ingest"}  # no orchestrator URL or token

COMMANDS = {
    "migrate": _cmd_migrate,
    "ingest": _cmd_ingest,
    "api": _cmd_api,
    "worker": _cmd_worker,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents-webhook", description="Agents webhook service")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}")
    for name, help_text in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def _load(command: str) -> WebhookSettings:
    """migrate and ingest load without the orchestrator variables; api and worker require them."""
    if command not in LOCAL_COMMANDS:
        return load_settings(WebhookSettings)
    try:
        return load_settings(LocalSettings)
    except SettingsError as exc:
        problems = {k: v for k, v in exc.problems.items() if "ORCHESTRATOR" not in k}
        if not problems:
            # only the orchestrator variables were reported: the local settings are valid
            return LocalSettings()
        raise SettingsError(problems) from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        settings = _load(args.command)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return EXIT_BAD_SETTINGS
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return COMMANDS[args.command](settings)


if __name__ == "__main__":
    sys.exit(main())
