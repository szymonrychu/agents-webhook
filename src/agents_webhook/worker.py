"""The singleton queue worker: first dispatch and run tracking (design D6, D8).

One worker holds the `agents-webhook-worker` advisory lock and ticks every `poll_seconds`, or
sooner on NOTIFY `webhook_events`. A tick:

1. resumes every in-progress row without a message (crash recovery; `runs.dispatch` creates no
   second cluster and posts no second prompt);
2. tracks every in-progress row with a message (`runs.track`: turn completed -> handled,
   message or turn failed, or older than `run_timeout_seconds` -> failed), which frees the slots
   the claims below use;
3. claims `new` rows within the caps until none is claimable; each goes through the `route`
   hook first (filled by agent reuse) and, when it answers False, is dispatched as a new
   cluster `wh-<id>`.

An unavailable orchestrator leaves the row as it is for the next tick (logged once per row per
minute); a 4xx on create or post fails the row with the error code. The service never deletes
clusters. A lost lock raises `LostLock` out of `run_worker` (the process exits non-zero and the
Deployment restarts it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agents_common import runs
from agents_common.db import listen
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from agents_common.runtime import advisory_singleton

from agents_webhook import prompt
from agents_webhook.db import events
from agents_webhook.settings import WebhookSettings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

LOCK_NAME = "agents-webhook-worker"
CHANNEL = "webhook_events"
RUN_KIND = "webhook:event"
UNAVAILABLE_LOG_SECONDS = 60.0


def run_ref(event: events.Event) -> runs.RunRef:
    """The run columns of an in-progress first-dispatch row."""
    assert event.started_at is not None
    return runs.RunRef(
        id=event.id,
        slug=f"wh-{event.id}",
        preset_id=event.preset_id or "",
        cluster_id=event.cluster_id,
        message_id=event.message_id,
        started_at=event.started_at,
    )


class Worker:
    """One tick of the worker; `run_worker` drives it under the singleton lock."""

    def __init__(
        self,
        settings: WebhookSettings,
        pool: AsyncConnectionPool,
        client: OrchestratorClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.client = client
        self.store = events.EventStore(pool)
        self._clock = clock
        self._unavailable_logged: dict[int, float] = {}

    async def tick(self) -> None:
        rows = await self._in_progress()
        for row in rows:
            if row.message_id is None:
                await self.dispatch(row)
        for row in rows:
            if row.message_id is not None:
                await self._track(row)
        while (claimed := await self._claim()) is not None:
            if not await self.route(claimed):
                await self.dispatch(claimed.event)

    async def route(self, claimed: events.Claimed) -> bool:
        """Hook per claimed row: True when the row was routed (to a running agent) and needs
        no first dispatch. First dispatch is the default."""
        return False

    async def dispatch(self, event: events.Event) -> None:
        """Create the row's cluster and post its rendered prompt, each at most once."""
        if not event.preset_id:
            await self.store.finish(event.id, "failed", "preset_not_found")
            return
        try:
            text = await prompt.render_async(
                events.to_normalized(event),
                event.prompt_template,
                payload=event.payload if isinstance(event.payload, dict) else None,
                timeout=self.settings.template_timeout_seconds,
            )
        except prompt.TemplateError as exc:
            log.warning("event %s: template error: %s", event.id, exc)
            await self.store.finish(event.id, "failed", "template_error")
            return
        try:
            await runs.dispatch(
                self.client, self.store, run_ref(event), text, runs.marker(RUN_KIND, event.id)
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(event.id, exc)
        except NotFound as exc:  # dispatch already failed the row
            log.warning("event %s: failed: %s", event.id, exc)
        except OrchestratorError as exc:
            if not 400 <= exc.status < 500:
                raise
            log.warning("event %s: failed: %s", event.id, exc)
            await self.store.finish(event.id, "failed", exc.code)
        else:
            self._unavailable_logged.pop(event.id, None)

    async def _track(self, event: events.Event) -> None:
        try:
            status = await runs.track(
                self.client,
                self.store,
                run_ref(event),
                datetime.now(UTC),
                timeout_seconds=self.settings.run_timeout_seconds,
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(event.id, exc)
            return
        except OrchestratorError as exc:
            log.warning("event %s: tracking error, retrying: %s", event.id, exc)
            return
        if status is not None:
            self._unavailable_logged.pop(event.id, None)
            log.info("event %s: %s", event.id, status)

    async def _in_progress(self) -> list[events.Event]:
        async with self.pool.connection() as conn:
            return await events.in_progress(conn)

    async def _claim(self) -> events.Claimed | None:
        async with self.pool.connection() as conn:
            return await events.claim_next(
                conn,
                max_in_progress=self.settings.max_in_progress,
                max_per_endpoint=self.settings.max_in_progress_per_endpoint,
                reuse_endpoints=set(),
            )

    def _unavailable(self, event_id: int, exc: Exception) -> None:
        now = self._clock()
        last = self._unavailable_logged.get(event_id)
        if last is None or now - last >= UNAVAILABLE_LOG_SECONDS:
            self._unavailable_logged[event_id] = now
            log.warning("event %s: orchestrator unavailable, retrying: %s", event_id, exc)


async def _wake_on_notify(url: str, wake: asyncio.Event) -> None:
    async for _ in listen(url, CHANNEL):
        wake.set()


async def run_worker(
    settings: WebhookSettings,
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    stop: asyncio.Event | None = None,
    *,
    heartbeat_seconds: float = 10,
) -> None:
    """Hold the worker lock and tick until `stop` is set; raises LostLock when the lock goes."""
    stop = stop or asyncio.Event()
    worker = Worker(settings, pool, client)
    async with advisory_singleton(pool, LOCK_NAME, heartbeat_seconds):
        log.info("worker lock acquired")
        wake = asyncio.Event()
        listener = asyncio.create_task(
            _wake_on_notify(settings.database_url.get_secret_value(), wake)
        )
        try:
            while not stop.is_set():
                wake.clear()
                await worker.tick()
                await _sleep(settings.poll_seconds, wake, stop)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener


async def _sleep(seconds: float, *events_: asyncio.Event) -> None:
    """Sleep `seconds` or until any of `events_` is set."""
    waits = [asyncio.create_task(e.wait()) for e in events_]
    try:
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()
