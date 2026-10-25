"""The singleton queue worker: first dispatch and run tracking (design D6, D8).

One worker holds the `agents-webhook-worker` advisory lock and ticks every `poll_seconds`, or
sooner on NOTIFY `webhook_events`. A tick:

1. reads the preset of every endpoint with `new` rows, once per endpoint (D17); the rows of an
   endpoint whose preset the orchestrator could not serve are not claimed this tick;
2. resumes every in-progress row without a message (crash recovery; `runs.dispatch` creates no
   second session and posts no second prompt, `runs.deliver` posts no second update);
3. tracks every in-progress row with a message (`runs.track`: turn completed -> handled,
   message or turn failed, or older than `run_timeout_seconds` -> failed; an update row through
   `runs.track_delivery`), which frees the slots the claims below use;
4. claims `new` rows within the caps until none is claimable; each goes through `route` first
   and, when it answers False, is dispatched as a new session `wh-<id>`.

Agent reuse (design D16, D17): when the endpoint's preset has `reuse_running_agent`, a claimed
row whose key has an earlier run is marked an update of that run and, while the run's session
is running (exists, desired `active`, first turn completed or not), delivered there as an update
message; it bypasses the caps and is handled once the message is `sent`. A session that is gone
at check time, refuses the post, or cancels or fails the message before `sent` makes the row
fall back once to a new session `wh-<id>` when the caps allow, else back to `new` keeping its
FIFO place; a 4xx on the session check or the post falls back the same way. A session that
disappears under a first dispatch fails the row `session_not_found`. A fallback row that then
fails records `reuse_fallback` in its reason. An update row still without a message after
`run_timeout_seconds` (the orchestrator stayed unavailable) fails `timeout`.

An unavailable orchestrator leaves the row as it is for the next tick (logged once per row per
minute); a 4xx on create or post fails the row with the error code. The service never deletes
sessions. A lost lock raises `LostLock` out of `run_worker` (the process exits non-zero and the
Deployment restarts it).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agents_common import runs
from agents_common.db import listen, transition
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
REUSE_FALLBACK = "reuse_fallback"
PRUNE_INTERVAL_SECONDS = 3600.0
PRUNE_BATCH = 1000


def run_ref(event: events.Event) -> runs.RunRef:
    """The run columns of an in-progress row (`slug` is the recorded session's, else its own)."""
    assert event.started_at is not None
    return runs.RunRef(
        id=event.id,
        slug=event.session_slug or f"wh-{event.id}",
        preset_id=event.preset_id or "",
        session_id=event.session_id,
        message_id=event.message_id,
        started_at=event.started_at,
    )


class _FallbackStore:
    """The run store of a reuse-fallback row: `EventStore`, with a failure reason prefixed
    `reuse_fallback`."""

    def __init__(self, store: events.EventStore) -> None:
        self._store = store

    async def set_session(self, run_id: runs.RunId, session_id: str) -> None:
        await self._store.set_session(run_id, session_id)

    async def set_message(self, run_id: runs.RunId, message_id: str) -> None:
        await self._store.set_message(run_id, message_id)

    async def finish(self, run_id: runs.RunId, status: runs.RunStatus, reason: str | None) -> bool:
        if status == "failed":
            reason = f"{REUSE_FALLBACK}: {reason}" if reason else REUSE_FALLBACK
        return await self._store.finish(run_id, status, reason)


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
        self.run_store: runs.RunStore = self.store
        self._fallback_store = _FallbackStore(self.store)
        self._clock = clock
        self._unavailable_logged: dict[tuple[str, int], float] = {}
        self.prune_batch = PRUNE_BATCH
        self._last_prune: float | None = None
        # Per tick: endpoints whose preset opts in to reuse, and endpoints whose preset could
        # not be read (their claimed rows are held and released at the end of the tick).
        self._reuse: set[int] = set()
        self._skip: set[int] = set()

    async def tick(self) -> None:
        await self._read_presets()
        rows = await self._in_progress()
        for row in rows:
            if row.message_id is None:
                await self._resume(row)
        for row in rows:
            if row.message_id is not None:
                await self._track(row)
        held: list[int] = []
        while (claimed := await self._claim()) is not None:
            if claimed.event.endpoint_id in self._skip:
                held.append(claimed.event.id)
            elif not await self.route(claimed):
                await self.dispatch(claimed.event)
        for id in held:
            async with self.pool.connection() as conn:
                await events.release(conn, id)
        await self.prune_events()

    async def prune_events(self) -> None:
        """Delete final events past the retention window, in batches, at most hourly."""
        now = self._clock()
        if self._last_prune is not None and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        total = 0
        while True:
            async with self.pool.connection() as conn:
                n = await events.prune(
                    conn,
                    retention_days=self.settings.events_retention_days,
                    batch=self.prune_batch,
                )
            total += n
            if n < self.prune_batch:
                break
        if total:
            log.info("retention: pruned %d events", total)

    async def route(self, claimed: events.Claimed) -> bool:
        """True when the row was routed (delivered to a running agent, fallen back, or left
        for the next tick) and needs no first dispatch; False: first dispatch."""
        event, prior = claimed.event, claimed.prior
        if event.reason == REUSE_FALLBACK:  # fell back once already: never reused again
            return not await self._room_or_release(event)
        if prior is None or event.endpoint_id not in self._reuse:
            return False
        async with self.pool.connection() as conn:
            await events.mark_update(conn, event.id, prior.id)
        await self._deliver(dataclasses.replace(event, update_of=prior.id), prior)
        return True

    async def dispatch(self, event: events.Event) -> None:
        """Create the row's session and post its rendered prompt, each at most once."""
        store = self._store_of(event)
        if not event.preset_id:
            await store.finish(event.id, "failed", "preset_not_found")
            return
        text = await self._render(event, store)
        if text is None:
            return
        try:
            await runs.dispatch(
                self.client, store, run_ref(event), text, runs.marker(RUN_KIND, event.id)
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(("event", event.id), exc)
        except NotFound as exc:  # dispatch already failed the row
            log.warning("event %s: failed: %s", event.id, exc)
        except OrchestratorError as exc:
            if not 400 <= exc.status < 500:
                raise
            log.warning("event %s: failed: %s", event.id, exc)
            await store.finish(event.id, "failed", exc.code)
        else:
            self._unavailable_logged.pop(("event", event.id), None)

    # --- reuse ------------------------------------------------------------------------------

    async def _deliver(self, event: events.Event, parent: events.Event) -> None:
        """Deliver update row `event` to `parent`'s session while it runs, else fall back."""
        assert parent.session_id is not None
        try:
            if await runs.running_session(self.client, parent.session_id) is None:
                log.info("event %s: session of event %s is not running", event.id, parent.id)
                await self._fallback(event)
                return
            if event.session_id is None:
                await self.store.set_session(event.id, parent.session_id, slug=parent.session_slug)
                event = dataclasses.replace(
                    event, session_id=parent.session_id, session_slug=parent.session_slug
                )
            text = await self._render(event, self.run_store, update=True)
            if text is None:
                return
            delivered = await runs.deliver(
                self.client,
                self.run_store,
                run_ref(event),
                parent.session_id,
                text,
                runs.marker(RUN_KIND, event.id),
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(("event", event.id), exc)
            return
        except OrchestratorError as exc:
            if not 400 <= exc.status < 500:
                raise
            log.warning("event %s: delivery refused, falling back: %s", event.id, exc)
            await self._fallback(event)
            return
        self._unavailable_logged.pop(("event", event.id), None)
        if delivered is None:
            log.info("event %s: session of event %s refused the update", event.id, parent.id)
            await self._fallback(event)

    async def _track_delivery(self, event: events.Event) -> None:
        try:
            outcome = await runs.track_delivery(
                self.client,
                self.run_store,
                run_ref(event),
                timeout_seconds=self.settings.run_timeout_seconds,
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(("event", event.id), exc)
            return
        except OrchestratorError as exc:
            log.warning("event %s: tracking error, retrying: %s", event.id, exc)
            return
        self._unavailable_logged.pop(("event", event.id), None)
        if outcome == "gone":
            log.info("event %s: update message cancelled or failed before sent", event.id)
            await self._fallback(event)
        elif outcome is not None:
            log.info("event %s: %s (update of %s)", event.id, outcome, event.update_of)

    async def _fallback(self, event: events.Event) -> None:
        """Make the update row a first-dispatch row (once): dispatch it when the caps allow,
        else return it to `new` keeping its FIFO place."""
        async with self.pool.connection() as conn:
            if event.reason != REUSE_FALLBACK:
                await transition(
                    conn, "events", event.id, "in-progress", "in-progress", reason=REUSE_FALLBACK
                )
            await events.clear_update(conn, event.id)
            fresh = await events.get_event(conn, event.id)
        if fresh is None or fresh.status != "in-progress":
            return
        if await self._room_or_release(fresh):
            await self.dispatch(fresh)

    async def _room_or_release(self, event: events.Event) -> bool:
        """True when a first dispatch of `event` fits the caps; else release it to `new`, and
        stop claiming its endpoint past the caps for the rest of the tick."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE endpoint_id = %s) FROM events "
                "WHERE status = 'in-progress' AND update_of IS NULL AND id <> %s",
                (event.endpoint_id, event.id),
            )
            total, per = await cur.fetchone() or (0, 0)
            if (
                total < self.settings.max_in_progress
                and per < self.settings.max_in_progress_per_endpoint
            ):
                return True
            await events.release(conn, event.id)
        log.info("event %s: caps full, back to new", event.id)
        if event.endpoint_id is not None:
            self._reuse.discard(event.endpoint_id)
        return False

    async def _read_presets(self) -> None:
        """Read each preset of the endpoints with `new` rows once: which opt in to reuse, and
        which could not be read (orchestrator unavailable)."""
        self._reuse, self._skip = set(), set()
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT ep.id, ep.preset_id FROM events e "
                "JOIN endpoints ep ON ep.id = e.endpoint_id WHERE e.status = 'new'"
            )
            pending = await cur.fetchall()
        for endpoint_id, preset_id in pending:
            try:
                preset = await self.client.get_preset(preset_id)
            except OrchestratorUnavailable as exc:
                self._unavailable(("endpoint", endpoint_id), exc)
                self._skip.add(endpoint_id)
                continue
            except OrchestratorError as exc:  # missing preset: reuse off
                log.info("endpoint %s: preset %s: %s", endpoint_id, preset_id, exc)
                continue
            self._unavailable_logged.pop(("endpoint", endpoint_id), None)
            if preset.reuse_running_agent:
                self._reuse.add(endpoint_id)

    # --- shared -----------------------------------------------------------------------------

    async def _resume(self, event: events.Event) -> None:
        """Continue an in-progress row that has no message yet (crash or unavailability)."""
        if event.update_of is None:
            await self.dispatch(event)
            return
        if event.reason == REUSE_FALLBACK:  # crashed between marking and clearing
            await self._fallback(event)
            return
        assert event.started_at is not None
        if (datetime.now(UTC) - event.started_at).total_seconds() > (
            self.settings.run_timeout_seconds
        ):
            log.warning("event %s: update not delivered in time", event.id)
            await self.store.finish(event.id, "failed", "timeout")
            return
        async with self.pool.connection() as conn:
            parent = await events.get_event(conn, event.update_of)
        if parent is None or parent.session_id is None:
            await self._fallback(event)
            return
        await self._deliver(event, parent)

    async def _render(
        self, event: events.Event, store: runs.RunStore, *, update: bool = False
    ) -> str | None:
        """The row's prompt; None after failing the row on a template error."""
        try:
            return await prompt.render_async(
                events.to_normalized(event),
                event.prompt_template,
                update=update,
                payload=event.payload if isinstance(event.payload, dict) else None,
                timeout=self.settings.template_timeout_seconds,
                memory_mb=self.settings.template_memory_mb,
            )
        except prompt.TemplateError as exc:
            log.warning("event %s: template error: %s", event.id, exc)
            await store.finish(event.id, "failed", "template_error")
            return None

    def _store_of(self, event: events.Event) -> runs.RunStore:
        return self._fallback_store if event.reason == REUSE_FALLBACK else self.run_store

    async def _track(self, event: events.Event) -> None:
        if event.update_of is not None:
            await self._track_delivery(event)
            return
        try:
            status = await runs.track(
                self.client,
                self._store_of(event),
                run_ref(event),
                datetime.now(UTC),
                timeout_seconds=self.settings.run_timeout_seconds,
            )
        except OrchestratorUnavailable as exc:
            self._unavailable(("event", event.id), exc)
            return
        except OrchestratorError as exc:
            log.warning("event %s: tracking error, retrying: %s", event.id, exc)
            return
        if status is not None:
            self._unavailable_logged.pop(("event", event.id), None)
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
                reuse_endpoints=self._reuse,
            )

    def _unavailable(self, key: tuple[str, int], exc: Exception) -> None:
        now = self._clock()
        last = self._unavailable_logged.get(key)
        if last is None or now - last >= UNAVAILABLE_LOG_SECONDS:
            self._unavailable_logged[key] = now
            log.warning("%s %s: orchestrator unavailable, retrying: %s", *key, exc)


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
