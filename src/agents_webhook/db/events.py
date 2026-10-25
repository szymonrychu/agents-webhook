"""The events queue (design D5, D6, D16).

`events` is a multi-writer queue table: ingest inserts rows and replaces the payload of a
`new` row (latest wins), the worker moves status. Every UPDATE carries `AND status=<expected>`,
status moves go through `agents_common.db.transition`, and claims use
`FOR UPDATE SKIP LOCKED`. Keys are per endpoint: `(endpoint_id, dedup_key)`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg
from agents_common.db import claim, transition
from agents_common.runs import RunId, RunStatus
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from agents_webhook.db.endpoints import Endpoint
from agents_webhook.normalize import NormalizedEvent

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

RECORD_RETRIES = 3
# Advisory lock namespace of one (endpoint, dedup_key): record and release take it, so a
# release never returns a row to `new` while ingest inserts a `new` row of the same key.
_KEY_LOCK = 0x77686B79  # "whky"
# Serialises claims, so the count-based caps hold with more than one claimer.
_CLAIM_LOCK = 0x77685F636C61696D  # "wh_claim"

_COLUMNS = (
    "id, seq, priority, received_at, updated_at, endpoint_id, endpoint_suffix, type, source, "
    "kind, action, repo, number, title, body, comment_body, url, author, actor, labels, payload, "
    "delivery_id, "
    "dedup_key, status, reason, preset_id, prompt_template, update_of, duplicate_of, deliveries, "
    "cluster_id, cluster_slug, message_id, started_at, finished_at, draft, changed"
)
# Ignored rows keep at most this much of a payload; only `new` rows keep it whole.
IGNORED_PAYLOAD_CAP = 4096
_CAP_IGNORED_SQL = (
    "UPDATE events SET payload = jsonb_build_object('truncated', true, "
    "'size', octet_length(payload::text), 'head', left(payload::text, %(cap)s)) "
    "WHERE id = %(id)s AND status = 'ignored' AND octet_length(payload::text) > %(cap)s"
)
# Columns latest-wins copies from a newer delivery of the same key onto the `new` row.
_LATEST = (
    "action",
    "title",
    "body",
    "comment_body",
    "url",
    "author",
    "actor",
    "labels",
    "draft",
    "changed",
    "payload",
)


@dataclass(frozen=True)
class Event:
    id: int
    seq: int
    priority: int
    received_at: datetime
    updated_at: datetime
    endpoint_id: int | None
    endpoint_suffix: str
    type: str
    source: str
    kind: str
    action: str | None
    repo: str | None
    number: int | None
    title: str | None
    body: str | None
    comment_body: str | None
    url: str | None
    author: str | None
    actor: str | None
    labels: Any
    payload: Any
    delivery_id: str | None
    dedup_key: str
    status: str
    reason: str | None
    preset_id: str | None
    prompt_template: str | None
    update_of: int | None
    duplicate_of: int | None
    deliveries: int
    cluster_id: str | None
    cluster_slug: str | None
    message_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    draft: bool
    changed: Any


@dataclass(frozen=True)
class Recorded:
    id: int
    status: str
    reason: str | None
    duplicate_of: int | None


@dataclass(frozen=True)
class Claimed:
    event: Event
    prior: Event | None


class RecordConflict(RuntimeError):
    """The key's `new` row kept being claimed between the insert and the replace."""


# --- record ---------------------------------------------------------------------------------


def _values(endpoint: Endpoint, event: NormalizedEvent, payload: Any) -> dict[str, Any]:
    return {
        "priority": event.priority,
        "endpoint_id": endpoint.id,
        "endpoint_suffix": endpoint.suffix,
        "type": endpoint.type,
        "source": event.source,
        "kind": event.kind,
        "action": event.action,
        "repo": event.repo,
        "number": event.number,
        "title": event.title,
        "body": event.body,
        "comment_body": event.comment_body,
        "url": event.url,
        "author": event.author,
        "actor": event.actor,
        "labels": Jsonb(event.labels),
        "draft": event.draft,
        "changed": Jsonb(list(event.changed)),
        "payload": Jsonb(payload),
        "delivery_id": event.delivery_id,
        "dedup_key": event.dedup_key,
        "preset_id": endpoint.preset_id,
        "prompt_template": endpoint.prompt_template,
    }


def cap_payload(payload: Any) -> Any:
    """`payload` as stored on an ignored row: whole when small, else a truncated stand-in."""
    text = json.dumps(payload, ensure_ascii=False)
    size = len(text.encode())
    if size <= IGNORED_PAYLOAD_CAP:
        return payload
    return {"truncated": True, "size": size, "head": text[:IGNORED_PAYLOAD_CAP]}


async def _cap_ignored(conn: psycopg.AsyncConnection, id: int) -> None:
    """Truncate the payload of a row that just became ignored."""
    await conn.execute(_CAP_IGNORED_SQL, {"id": id, "cap": IGNORED_PAYLOAD_CAP})


def _insert_sql(values: dict[str, Any], tail: str = "") -> str:
    cols = ", ".join([*values, "status", "reason", "duplicate_of"])
    params = ", ".join(f"%({c})s" for c in [*values, "status", "reason", "duplicate_of"])
    return f"INSERT INTO events ({cols}) VALUES ({params}) {tail} RETURNING id"


async def _insert(
    conn: psycopg.AsyncConnection,
    values: dict[str, Any],
    status: str,
    reason: str | None = None,
    duplicate_of: int | None = None,
) -> Recorded:
    params = {**values, "status": status, "reason": reason, "duplicate_of": duplicate_of}
    if status == "ignored":
        params["payload"] = Jsonb(cap_payload(values["payload"].obj))
    cur = await conn.execute(_insert_sql(values), params)
    row = await cur.fetchone()
    assert row is not None
    return Recorded(row[0], status, reason, duplicate_of)


async def _insert_new(conn: psycopg.AsyncConnection, values: dict[str, Any]) -> int | None:
    """Insert the `new` row unless the key already has one; its id, or None."""
    params = {**values, "status": "new", "reason": None, "duplicate_of": None}
    sql = _insert_sql(
        values, "ON CONFLICT (endpoint_id, dedup_key) WHERE status = 'new' DO NOTHING"
    )
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return row[0] if row else None


async def _lock_new(conn: psycopg.AsyncConnection, endpoint_id: int, key: str) -> int | None:
    """Lock the key's `new` row; None when it was claimed meanwhile."""
    cur = await conn.execute(
        "SELECT id FROM events WHERE endpoint_id = %s AND dedup_key = %s AND status = 'new' "
        "FOR UPDATE",
        (endpoint_id, key),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def _replace_new(conn: psycopg.AsyncConnection, id: int, values: dict[str, Any]) -> bool:
    sets = ", ".join(f"{c} = %({c})s" for c in _LATEST)
    cur = await conn.execute(
        f"UPDATE events SET {sets}, updated_at = now(), deliveries = deliveries + 1 "
        "WHERE id = %(id)s AND status = 'new'",
        {**{c: values[c] for c in _LATEST}, "id": id},
    )
    return cur.rowcount == 1


async def record(
    conn: psycopg.AsyncConnection,
    endpoint: Endpoint,
    event: NormalizedEvent,
    payload: Any,
    *,
    ignore_reason: str | None,
    max_queue: int,
) -> Recorded:
    """Store one delivery: ignored, a duplicate, queue_full, a new row or latest-wins.

    Raises RecordConflict when the key's `new` row was claimed between the insert and the
    replace on every one of `RECORD_RETRIES` retries.
    """
    values = _values(endpoint, event, payload)
    async with conn.transaction():
        if ignore_reason is not None:
            return await _insert(conn, values, "ignored", ignore_reason)
        if event.delivery_id is not None:
            cur = await conn.execute(
                "SELECT min(id) FROM events WHERE endpoint_id = %s AND delivery_id = %s",
                (endpoint.id, event.delivery_id),
            )
            first = (await cur.fetchone() or (None,))[0]
            if first is not None:
                return await _insert(conn, values, "ignored", "duplicate", first)
        cur = await conn.execute("SELECT count(*) FROM events WHERE status = 'new'")
        if (await cur.fetchone() or (0,))[0] >= max_queue:
            return await _insert(conn, values, "ignored", "queue_full")
        await _lock_key(conn, "%s::bigint, %s::text", (endpoint.id, event.dedup_key))
        for _ in range(RECORD_RETRIES + 1):
            new_id = await _insert_new(conn, values)
            if new_id is not None:
                return Recorded(new_id, "new", None, None)
            target = await _lock_new(conn, endpoint.id, event.dedup_key)
            if target is not None and await _replace_new(conn, target, values):
                return await _insert(conn, values, "ignored", "duplicate", target)
    raise RecordConflict(f"events: key {event.dedup_key!r} kept being claimed")


# --- claim ----------------------------------------------------------------------------------

# Per-key order: a same-key row in progress without a cluster, or an update in flight, blocks;
# without reuse any same-key row in progress blocks. A reuse candidate (reuse endpoint with a
# prior run) bypasses the caps; any other row needs room under both.
_PICK = """
SELECT e.id, e.endpoint_id, now() AS now FROM events e
WHERE e.status = 'new' AND (e.endpoint_id IS NULL OR (
    NOT EXISTS (
        SELECT 1 FROM events b
        WHERE b.endpoint_id = e.endpoint_id AND b.dedup_key = e.dedup_key
          AND b.status = 'in-progress'
          AND (b.cluster_id IS NULL OR b.update_of IS NOT NULL
               OR NOT e.endpoint_id = ANY(%(reuse)s))
    )
    AND (
        (e.endpoint_id = ANY(%(reuse)s) AND EXISTS (
            SELECT 1 FROM events p
            WHERE p.endpoint_id = e.endpoint_id AND p.dedup_key = e.dedup_key
              AND p.update_of IS NULL AND p.cluster_id IS NOT NULL))
        OR (
            (SELECT count(*) FROM events c
             WHERE c.status = 'in-progress' AND c.update_of IS NULL) < %(cap)s
            AND (SELECT count(*) FROM events c
                 WHERE c.status = 'in-progress' AND c.update_of IS NULL
                   AND c.endpoint_id = e.endpoint_id) < %(per)s
        )
    )
))
ORDER BY e.priority, e.seq
LIMIT 1
FOR UPDATE OF e SKIP LOCKED
"""


async def claim_next(
    conn: psycopg.AsyncConnection,
    *,
    max_in_progress: int,
    max_per_endpoint: int,
    reuse_endpoints: set[int],
) -> Claimed | None:
    """Move the oldest eligible `new` row to in-progress; None when nothing is claimable.

    `new` rows whose endpoint was deleted are moved to ignored `endpoint_removed` on the way.
    """
    params = {"reuse": list(reuse_endpoints), "cap": max_in_progress, "per": max_per_endpoint}
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CLAIM_LOCK,))
        while (row := await claim(conn, _PICK, params)) is not None:
            if row["endpoint_id"] is None:
                await transition(
                    conn, "events", row["id"], "new", "ignored", reason="endpoint_removed"
                )
                await _cap_ignored(conn, row["id"])
                continue
            if not await transition(
                conn, "events", row["id"], "new", "in-progress", started_at=row["now"]
            ):
                continue
            event = await get_event(conn, row["id"])
            assert event is not None
            return Claimed(event, await latest_run(conn, event.endpoint_id, event.dedup_key))
    return None


# --- worker transitions ---------------------------------------------------------------------


async def mark_update(conn: psycopg.AsyncConnection, id: int, parent_id: int) -> bool:
    """Mark an in-progress row as an update delivered to `parent_id`'s cluster."""
    return await transition(conn, "events", id, "in-progress", "in-progress", update_of=parent_id)


async def clear_update(conn: psycopg.AsyncConnection, id: int) -> bool:
    """Make an in-progress update row a first-dispatch row again (reuse fallback)."""
    return await transition(
        conn,
        "events",
        id,
        "in-progress",
        "in-progress",
        update_of=None,
        cluster_id=None,
        cluster_slug=None,
        message_id=None,
    )


async def _lock_key(conn: psycopg.AsyncConnection, source: str, params: Any) -> None:
    """Take the transaction lock of the key selected by `SELECT <source>`."""
    await conn.execute(
        f"SELECT pg_advisory_xact_lock({_KEY_LOCK}, hashtext(k.e::text || ':' || k.d)) "
        f"FROM (SELECT {source}) AS k (e, d)",
        params,
    )


async def _newer_new(conn: psycopg.AsyncConnection, id: int) -> int | None:
    """Lock in-progress row `id` and the `new` row of its key; the latter's id, or None."""
    cur = await conn.execute(
        "SELECT n.id FROM events r JOIN events n ON n.endpoint_id = r.endpoint_id "
        "AND n.dedup_key = r.dedup_key AND n.status = 'new' "
        "WHERE r.id = %s AND r.status = 'in-progress' FOR UPDATE",
        (id,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def release(conn: psycopg.AsyncConnection, id: int) -> bool:
    """`in-progress -> new`, keeping `seq` (its FIFO place) and clearing run columns.

    A newer `new` row of the key that arrived meanwhile is folded in (latest wins): its fields
    move onto this row and it becomes ignored `duplicate` of it.
    """
    async with conn.transaction():
        await _lock_key(conn, "endpoint_id, dedup_key FROM events WHERE id = %s", (id,))
        newer = await _newer_new(conn, id)
        if newer is not None:
            await transition(
                conn, "events", newer, "new", "ignored", reason="duplicate", duplicate_of=id
            )
        sets = ", ".join(f"{c} = n.{c}" for c in _LATEST)
        cur = await conn.execute(
            "UPDATE events r SET status = 'new', started_at = NULL, update_of = NULL, "
            "cluster_id = NULL, cluster_slug = NULL, message_id = NULL"
            + (
                f", {sets}, updated_at = now(), deliveries = r.deliveries + n.deliveries "
                "FROM events n WHERE n.id = %(newer)s AND "
                if newer is not None
                else " WHERE "
            )
            + "r.id = %(id)s AND r.status = 'in-progress'",
            {"id": id, "newer": newer},
        )
        if newer is not None:
            await _cap_ignored(conn, newer)
        return cur.rowcount == 1


class EventStore:
    """`agents_common.runs.RunStore` over the events table; every write is status-guarded."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def set_cluster(self, run_id: RunId, cluster_id: str, *, slug: str | None = None) -> None:
        """Record the cluster; `slug` defaults to the run's own `wh-<id>`."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE events SET cluster_id = %s, cluster_slug = COALESCE(%s, 'wh-' || id) "
                "WHERE id = %s AND status = 'in-progress'",
                (cluster_id, slug, run_id),
            )

    async def set_message(self, run_id: RunId, message_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE events SET message_id = %s WHERE id = %s AND status = 'in-progress'",
                (message_id, run_id),
            )

    async def finish(self, run_id: RunId, status: RunStatus, reason: str | None) -> bool:
        if status not in ("handled", "failed"):
            raise ValueError(f"status: must be handled or failed, not {status!r}")
        async with self._pool.connection() as conn:
            return await transition(
                conn,
                "events",
                run_id,
                "in-progress",
                status,
                reason=reason,
                finished_at=await _now(conn),
            )


async def prune(conn: psycopg.AsyncConnection, *, retention_days: int, batch: int) -> int:
    """Delete up to `batch` final events (handled, ignored, failed) idle for `retention_days`.

    Never touches `new` or `in-progress` rows. A row that survives but points at a pruned row
    through update_of/duplicate_of has the reference nulled first. Returns the rows deleted.
    """
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT id FROM events WHERE status IN ('handled', 'ignored', 'failed') "
            "AND updated_at < now() - make_interval(days => %s) ORDER BY id LIMIT %s "
            "FOR UPDATE SKIP LOCKED",
            (retention_days, batch),
        )
        ids = [r[0] for r in await cur.fetchall()]
        if not ids:
            return 0
        await conn.execute(
            "UPDATE events SET update_of = CASE WHEN update_of = ANY(%(ids)s) THEN NULL "
            "ELSE update_of END, duplicate_of = CASE WHEN duplicate_of = ANY(%(ids)s) THEN NULL "
            "ELSE duplicate_of END WHERE update_of = ANY(%(ids)s) OR duplicate_of = ANY(%(ids)s)",
            {"ids": ids},
        )
        await conn.execute("DELETE FROM events WHERE id = ANY(%s)", (ids,))
    return len(ids)


async def _now(conn: psycopg.AsyncConnection) -> datetime:
    cur = await conn.execute("SELECT now()")
    row = await cur.fetchone()
    assert row is not None
    return row[0]


def to_normalized(event: Event) -> NormalizedEvent:
    """The normalised event of a row, for the prompt (`type` is the endpoint's type)."""
    return NormalizedEvent(
        type=event.type,
        source=event.source,
        kind=event.kind,
        action=event.action or "",
        repo=event.repo,
        number=event.number,
        title=event.title or "",
        body=event.body or "",
        comment_body=event.comment_body,
        url=event.url,
        author=event.author,
        actor=event.actor,
        labels=event.labels if event.labels is not None else [],
        delivery_id=event.delivery_id,
        dedup_key=event.dedup_key,
        priority=event.priority,
        draft=event.draft,
        changed=tuple(event.changed),
    )


# --- reads ----------------------------------------------------------------------------------


async def _fetch(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> list[Event]:
    cur = conn.cursor(row_factory=class_row(Event))
    await cur.execute(query, params)
    return await cur.fetchall()


async def get_event(conn: psycopg.AsyncConnection, id: int) -> Event | None:
    rows = await _fetch(conn, f"SELECT {_COLUMNS} FROM events WHERE id = %s", (id,))
    return rows[0] if rows else None


async def list_events(
    conn: psycopg.AsyncConnection,
    *,
    status: str | None = None,
    endpoint_id: int | None = None,
    limit: int = 100,
    before_id: int | None = None,
) -> list[Event]:
    """Events newest first; `before_id` pages to older ones."""
    return await _fetch(
        conn,
        f"SELECT {_COLUMNS} FROM events "
        "WHERE (%(status)s::text IS NULL OR status = %(status)s) "
        "AND (%(endpoint)s::bigint IS NULL OR endpoint_id = %(endpoint)s) "
        "AND (%(before)s::bigint IS NULL OR id < %(before)s) "
        "ORDER BY id DESC LIMIT %(limit)s",
        {"status": status, "endpoint": endpoint_id, "before": before_id, "limit": limit},
    )


async def in_progress(conn: psycopg.AsyncConnection) -> list[Event]:
    """Every in-progress row, oldest first (the worker's resume and tracking pass)."""
    return await _fetch(
        conn, f"SELECT {_COLUMNS} FROM events WHERE status = 'in-progress' ORDER BY id"
    )


async def latest_run(
    conn: psycopg.AsyncConnection, endpoint_id: int | None, dedup_key: str
) -> Event | None:
    """The newest first-dispatch row of the key that has a cluster, any status."""
    rows = await _fetch(
        conn,
        f"SELECT {_COLUMNS} FROM events WHERE endpoint_id = %s AND dedup_key = %s "
        "AND update_of IS NULL AND cluster_id IS NOT NULL ORDER BY id DESC LIMIT 1",
        (endpoint_id, dedup_key),
    )
    return rows[0] if rows else None
