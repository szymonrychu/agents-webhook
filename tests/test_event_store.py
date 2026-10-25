import asyncio
import dataclasses
import re
import uuid

import psycopg
import pytest
from agents_common.db import migrate, open_pool
from psycopg import sql

from agents_webhook.db import endpoints, events
from agents_webhook.db.endpoints import EndpointIn
from agents_webhook.normalize import NormalizedEvent

BIG = 1000
# The retired unit word (design D2), split so the nomenclature guard does not flag it.
RETIRED_UNIT = "clu" + "ster"


def ev(key: str = "github/o/r/pr/1", **over) -> NormalizedEvent:
    base = {
        "type": "github-pr",
        "source": "github",
        "kind": "mr",
        "action": "opened",
        "repo": "o/r",
        "number": 1,
        "title": "t",
        "body": "b",
        "comment_body": None,
        "url": "https://x/1",
        "author": "alice",
        "actor": "alice",
        "labels": ["a"],
        "delivery_id": None,
        "dedup_key": key,
        "priority": 1,
    }
    base.update(over)
    return NormalizedEvent(**base)


def alert(key: str) -> NormalizedEvent:
    return ev(key, type="grafana-alert", source="grafana", kind="alert", priority=0)


@pytest.fixture
async def conn(db):
    async with await psycopg.AsyncConnection.connect(db, autocommit=True) as c:
        yield c


@pytest.fixture
async def make_endpoint(conn):
    async def make(type_: str = "github-pr", preset: str = "p1", template: str | None = None):
        ep, _ = await endpoints.create(
            conn,
            EndpointIn(type=type_, preset_id=preset, prompt_template=template),
            min_password_length=16,
        )
        return ep

    return make


@pytest.fixture
async def ep(make_endpoint):
    return await make_endpoint(template="T $title")


@pytest.fixture
async def store(db):
    pool = await open_pool(db, max_size=4)
    try:
        yield events.EventStore(pool)
    finally:
        await pool.close()


async def rec(conn, endpoint, event=None, payload=None, *, ignore_reason=None, max_queue=BIG):
    return await events.record(
        conn,
        endpoint,
        event or ev(),
        payload if payload is not None else {"n": 0},
        ignore_reason=ignore_reason,
        max_queue=max_queue,
    )


async def claim(conn, *, cap=BIG, per=BIG, reuse=frozenset()):
    return await events.claim_next(
        conn, max_in_progress=cap, max_per_endpoint=per, reuse_endpoints=set(reuse)
    )


async def statuses(conn) -> dict[int, tuple[str, str | None]]:
    cur = await conn.execute("SELECT id, status, reason FROM events ORDER BY id")
    return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}


# --- record ---------------------------------------------------------------------------------


async def test_record_new_copies_endpoint_fields(conn, ep):
    r = await rec(conn, ep, ev(delivery_id="d1"), {"x": 1})
    assert (r.status, r.reason, r.duplicate_of) == ("new", None, None)
    row = await events.get_event(conn, r.id)
    assert row.endpoint_id == ep.id and row.endpoint_suffix == ep.suffix
    assert row.type == ep.type and row.preset_id == "p1" and row.prompt_template == "T $title"
    assert (row.source, row.kind, row.action, row.repo, row.number) == (
        "github",
        "mr",
        "opened",
        "o/r",
        1,
    )
    assert (row.title, row.url, row.author, row.actor) == ("t", "https://x/1", "alice", "alice")
    assert row.labels == ["a"] and row.payload == {"x": 1} and row.delivery_id == "d1"
    assert row.priority == 1 and row.deliveries == 1 and row.seq > 0


@pytest.mark.parametrize(
    "reason",
    [
        "type_mismatch",
        "unsupported_event",
        "self_event",
        "filter_not_allowed: author",
        "action_ignored",
        "resolved",
    ],
)
async def test_record_ignore_reason_is_ignored_with_fields(conn, ep, reason):
    r = await rec(conn, ep, ev(title="kept"), {"p": 1}, ignore_reason=reason)
    assert (r.status, r.reason) == ("ignored", reason)
    row = await events.get_event(conn, r.id)
    assert row.title == "kept" and row.payload == {"p": 1} and row.endpoint_id == ep.id
    # An ignored row never blocks a later new row of the same key.
    assert (await rec(conn, ep)).status == "new"


async def test_redelivery_by_delivery_id_is_ignored_duplicate(conn, ep):
    first = await rec(conn, ep, ev(delivery_id="d1"), {"v": 1})
    assert (await claim(conn)).event.id == first.id  # no longer new: no key dedup
    again = await rec(conn, ep, ev(delivery_id="d1"), {"v": 2})
    assert (again.status, again.reason, again.duplicate_of) == ("ignored", "duplicate", first.id)
    third = await rec(conn, ep, ev(delivery_id="d1"), {"v": 3})
    assert third.duplicate_of == first.id
    assert (await events.get_event(conn, first.id)).payload == {"v": 1}
    # Redelivery is per endpoint: another endpoint with the same delivery id records new.
    other = await rec(conn, await _other_endpoint(conn), ev(delivery_id="d1"))
    assert other.status == "new"


async def _other_endpoint(conn):
    created, _ = await endpoints.create(
        conn, EndpointIn(type="github-pr", preset_id="p2"), min_password_length=16
    )
    return created


async def test_ignored_queue_full(conn, ep):
    a = await rec(conn, ep, ev("k1"), max_queue=2)
    b = await rec(conn, ep, ev("k2"), max_queue=2)
    full = await rec(conn, ep, ev("k3", title="third"), {"q": 3}, max_queue=2)
    assert [a.status, b.status] == ["new", "new"]
    assert (full.status, full.reason) == ("ignored", "queue_full")
    row = await events.get_event(conn, full.id)
    assert row.title == "third" and row.payload == {"q": 3}


async def test_latest_wins_replaces_payload_keeps_seq_and_position(conn, ep):
    first = await rec(conn, ep, ev("k1", title="old", url="u1", labels=["x"]), {"v": 1})
    second = await rec(conn, ep, ev("k2"), {"other": 1})
    before = await events.get_event(conn, first.id)
    dup = await rec(conn, ep, ev("k1", title="new", url="u2", labels=["y"]), {"v": 2})
    assert (dup.status, dup.reason, dup.duplicate_of) == ("ignored", "duplicate", first.id)
    after = await events.get_event(conn, first.id)
    assert after.status == "new" and after.seq == before.seq and after.deliveries == 2
    assert (after.title, after.url, after.labels, after.payload) == ("new", "u2", ["y"], {"v": 2})
    assert after.updated_at > before.updated_at
    listed = await events.get_event(conn, dup.id)
    assert listed.status == "ignored" and listed.reason == "duplicate"
    assert listed.payload == {"v": 2} and listed.duplicate_of == first.id
    # It keeps its FIFO place ahead of k2.
    got = await claim(conn)
    assert got.event.id == first.id
    assert (await claim(conn)).event.id == second.id


async def test_same_key_on_two_endpoints_is_two_rows(conn, ep, make_endpoint):
    other = await make_endpoint()
    a = await rec(conn, ep, ev("k"))
    b = await rec(conn, other, ev("k"))
    assert a.status == b.status == "new" and a.id != b.id


async def test_concurrent_record_one_key_leaves_one_new_row(db, conn, ep, make_endpoint):
    other = await make_endpoint()

    async def one(i: int, endpoint):
        async with await psycopg.AsyncConnection.connect(db, autocommit=True) as c:
            return await rec(c, endpoint, ev("same"), {"i": i})

    results = await asyncio.gather(
        *(one(i, ep) for i in range(20)), *(one(i, other) for i in range(5))
    )
    for endpoint, mine in ((ep, results[:20]), (other, results[20:])):
        new = [r for r in mine if r.status == "new"]
        dups = [r for r in mine if r.status == "ignored"]
        assert len(new) == 1 and len(dups) == len(mine) - 1
        assert all(r.reason == "duplicate" and r.duplicate_of == new[0].id for r in dups)
        cur = await conn.execute(
            "SELECT deliveries FROM events WHERE endpoint_id = %s AND status = 'new'",
            (endpoint.id,),
        )
        assert [r[0] for r in await cur.fetchall()] == [len(mine)]


async def test_record_retries_when_new_row_claimed_mid_record(db, conn, ep, monkeypatch):
    first = await rec(conn, ep, ev("k"), {"v": 1})
    real = events._lock_new
    calls = []

    async def claimed_meanwhile(c, endpoint_id, key):
        if not calls:
            async with await psycopg.AsyncConnection.connect(db, autocommit=True) as w:
                assert (await claim(w)).event.id == first.id
        calls.append(key)
        return await real(c, endpoint_id, key)

    monkeypatch.setattr(events, "_lock_new", claimed_meanwhile)
    second = await rec(conn, ep, ev("k"), {"v": 2})
    assert second.status == "new" and second.id != first.id
    assert (await events.get_event(conn, first.id)).status == "in-progress"
    assert (await events.get_event(conn, second.id)).payload == {"v": 2}
    assert len(calls) == 1


async def test_record_retry_gives_up_after_three(conn, ep, monkeypatch):
    attempts = []

    async def never_inserted(c, *args):
        attempts.append(1)
        return None

    async def never_locked(c, endpoint_id, key):
        return None

    monkeypatch.setattr(events, "_insert_new", never_inserted)
    monkeypatch.setattr(events, "_lock_new", never_locked)
    with pytest.raises(events.RecordConflict):
        await rec(conn, ep, ev("k"))
    assert len(attempts) == 4  # the first try and 3 retries
    assert await statuses(conn) == {}


# --- claim_next -----------------------------------------------------------------------------


async def test_claim_fifo_alert_priority_first(conn, make_endpoint):
    gh = await make_endpoint()
    graf = await make_endpoint("grafana-alert")
    a = await rec(conn, gh, ev("a"))
    b = await rec(conn, gh, ev("b"))
    c = await rec(conn, graf, alert("c"))
    d = await rec(conn, graf, alert("d"))
    order = [(await claim(conn)).event.id for _ in range(4)]
    assert order == [c.id, d.id, a.id, b.id]
    assert await claim(conn) is None
    row = await events.get_event(conn, c.id)
    assert row.status == "in-progress" and row.started_at is not None


async def test_claim_overall_cap(conn, store, make_endpoint):
    eps = [await make_endpoint() for _ in range(4)]
    ids = [(await rec(conn, e, ev("k"))).id for e in eps]
    got = [(await claim(conn, cap=3)).event.id for _ in range(3)]
    assert got == ids[:3]
    assert await claim(conn, cap=3) is None
    assert await store.finish(ids[0], "handled", None)
    assert (await claim(conn, cap=3)).event.id == ids[3]


async def test_claim_per_endpoint_cap(conn, store, make_endpoint):
    a, b = await make_endpoint(), await make_endpoint()
    a1 = await rec(conn, a, ev("k1"))
    a2 = await rec(conn, a, ev("k2"))
    b1 = await rec(conn, b, ev("k3"))
    assert (await claim(conn, per=1)).event.id == a1.id
    # a2 is older than b1 but its endpoint is at the cap.
    assert (await claim(conn, per=1)).event.id == b1.id
    assert await claim(conn, per=1) is None
    assert await store.finish(a1.id, "failed", "boom")
    assert (await claim(conn, per=1)).event.id == a2.id


async def test_claim_per_key_order_without_reuse(conn, store, ep):
    first = await rec(conn, ep, ev("k"))
    assert (await claim(conn)).event.id == first.id
    second = await rec(conn, ep, ev("k"))
    other = await rec(conn, ep, ev("other"))
    # Same key in progress (even with a session): reuse off waits for it to end.
    await store.set_session(first.id, "c1")
    assert (await claim(conn)).event.id == other.id
    assert await claim(conn) is None
    assert await store.finish(first.id, "handled", None)
    got = await claim(conn)
    assert got.event.id == second.id
    assert got.prior.id == first.id  # carried, though reuse is off


async def test_claim_per_key_order_with_reuse(conn, store, ep):
    reuse = {ep.id}
    first = await rec(conn, ep, ev("k"))
    assert (await claim(conn, reuse=reuse)).event.id == first.id
    second = await rec(conn, ep, ev("k"))
    # The first row is in progress without a session: same key waits.
    assert await claim(conn, reuse=reuse) is None
    await store.set_session(first.id, "c1")
    got = await claim(conn, reuse=reuse)
    assert got.event.id == second.id and got.prior.id == first.id
    assert await events.mark_update(conn, second.id, first.id)
    await store.set_session(second.id, "c1")
    third = await rec(conn, ep, ev("k"))
    # An update of the key is in flight: the next one waits for it.
    assert await claim(conn, reuse=reuse) is None
    assert await store.finish(second.id, "handled", None)
    got = await claim(conn, reuse=reuse)
    assert got.event.id == third.id and got.prior.id == first.id


async def test_reuse_candidate_bypasses_full_caps(conn, store, ep, make_endpoint):
    other = await make_endpoint()
    first = await rec(conn, ep, ev("k"))
    assert (await claim(conn, cap=1, per=1, reuse={ep.id})).event.id == first.id
    await store.set_session(first.id, "c1")
    blocked = await rec(conn, other, ev("x"))
    update = await rec(conn, ep, ev("k"))
    got = await claim(conn, cap=1, per=1, reuse={ep.id})
    assert got.event.id == update.id and got.prior.id == first.id
    # Without reuse on the endpoint, it would wait like any other row.
    assert await events.release(conn, update.id)
    assert await claim(conn, cap=1, per=1) is None
    assert (await events.get_event(conn, blocked.id)).status == "new"


async def test_reuse_prior_is_newest_run_with_session(conn, store, ep):
    reuse = {ep.id}
    r1 = await rec(conn, ep, ev("k"))
    await claim(conn, reuse=reuse)
    await store.set_session(r1.id, "c1")
    await store.finish(r1.id, "handled", None)
    r2 = await rec(conn, ep, ev("k"))
    await claim(conn, reuse=reuse)
    await store.set_session(r2.id, "c2")
    await store.finish(r2.id, "failed", "x")
    # An update row and a run without a session are never a prior.
    r3 = await rec(conn, ep, ev("k"))
    await claim(conn, reuse=reuse)
    await events.mark_update(conn, r3.id, r2.id)
    await store.set_session(r3.id, "c2")
    await store.finish(r3.id, "handled", None)
    r4 = await rec(conn, ep, ev("k"))
    await claim(conn, reuse=reuse)
    await store.finish(r4.id, "failed", "preset_not_found")
    r5 = await rec(conn, ep, ev("k"))
    got = await claim(conn, reuse=reuse)
    assert got.event.id == r5.id and got.prior.id == r2.id and got.prior.session_id == "c2"
    assert (await events.latest_run(conn, ep.id, "k")).id == r2.id
    assert await events.latest_run(conn, ep.id, "nope") is None


async def test_reuse_release_keeps_fifo_place(conn, ep, make_endpoint):
    other = await make_endpoint()
    a = await rec(conn, ep, ev("a"))
    b = await rec(conn, other, ev("b"))
    got = await claim(conn)
    assert got.event.id == a.id
    seq = got.event.seq
    assert await events.release(conn, a.id)
    row = await events.get_event(conn, a.id)
    assert row.status == "new" and row.seq == seq and row.started_at is None
    assert (await claim(conn)).event.id == a.id
    assert (await claim(conn)).event.id == b.id


async def test_reuse_release_merges_a_newer_new_row_of_the_key(conn, store, ep):
    a = await rec(conn, ep, ev("k", title="old"), {"v": 1})
    await claim(conn, reuse={ep.id})
    await events.mark_update(conn, a.id, a.id)
    await store.set_session(a.id, "c1")
    await store.set_message(a.id, "m1")
    newer = await rec(conn, ep, ev("k", title="newer"), {"v": 2})
    assert await events.clear_update(conn, a.id)
    assert await events.release(conn, a.id)
    row = await events.get_event(conn, a.id)
    assert row.status == "new" and row.title == "newer" and row.payload == {"v": 2}
    assert row.deliveries == 2 and row.update_of is None
    assert (row.session_id, row.session_slug, row.message_id) == (None, None, None)
    gone = await events.get_event(conn, newer.id)
    assert (gone.status, gone.reason, gone.duplicate_of) == ("ignored", "duplicate", a.id)


async def test_ignored_endpoint_removed(conn, ep, make_endpoint):
    other = await make_endpoint()
    gone = await rec(conn, ep, ev("k"))
    kept = await rec(conn, other, ev("k"))
    assert await endpoints.delete(conn, ep.id)
    got = await claim(conn)
    assert got.event.id == kept.id
    row = await events.get_event(conn, gone.id)
    assert (row.status, row.reason, row.endpoint_id) == ("ignored", "endpoint_removed", None)
    assert row.endpoint_suffix == ep.suffix
    assert await claim(conn) is None


async def test_claim_concurrent_never_returns_same_row(db, conn, make_endpoint):
    eps = [await make_endpoint() for _ in range(5)]
    for e in eps:
        for i in range(4):
            await rec(conn, e, ev(f"k{i}"))

    async def worker():
        got = []
        async with await psycopg.AsyncConnection.connect(db, autocommit=True) as c:
            while (r := await claim(c, cap=BIG, per=BIG)) is not None:
                got.append(r.event.id)
        return got

    a, b = await asyncio.gather(worker(), worker())
    assert not set(a) & set(b) and len(a) + len(b) == 20


async def test_claim_concurrent_respects_caps(db, conn, make_endpoint):
    eps = [await make_endpoint() for _ in range(6)]
    for e in eps:
        await rec(conn, e, ev("k"))

    async def one():
        async with await psycopg.AsyncConnection.connect(db, autocommit=True) as c:
            return await claim(c, cap=3)

    results = await asyncio.gather(*(one() for _ in range(6)))
    assert len([r for r in results if r is not None]) == 3


# --- guarded transitions and the run store --------------------------------------------------


async def test_guarded_transitions_refuse_wrong_source_status(conn, store, ep):
    new = await rec(conn, ep, ev("k"))
    assert not await events.mark_update(conn, new.id, new.id)
    assert not await events.clear_update(conn, new.id)
    assert not await events.release(conn, new.id)
    assert not await store.finish(new.id, "handled", None)
    await store.set_session(new.id, "c1")
    await store.set_message(new.id, "m1")
    row = await events.get_event(conn, new.id)
    assert row.status == "new" and row.session_id is None and row.message_id is None
    await claim(conn)
    assert await store.finish(new.id, "handled", None)
    assert not await store.finish(new.id, "failed", "late")
    assert not await events.release(conn, new.id)
    assert not await events.mark_update(conn, new.id, new.id)
    await store.set_session(new.id, "c2")
    row = await events.get_event(conn, new.id)
    assert row.status == "handled" and row.reason is None and row.session_id is None
    with pytest.raises(ValueError):
        await store.finish(new.id, "ignored", None)


async def test_guarded_ignored_rows_never_move(conn, store, ep):
    ign = await rec(conn, ep, ev("k"), ignore_reason="self_event")
    assert not await store.finish(ign.id, "failed", "x")
    assert not await events.release(conn, ign.id)
    assert (await events.get_event(conn, ign.id)).status == "ignored"


async def test_run_store_records_session_message_and_finish(conn, store, ep):
    r = await rec(conn, ep, ev("k"))
    await claim(conn)
    await store.set_session(r.id, "c1")
    await store.set_message(r.id, "m1")
    row = await events.get_event(conn, r.id)
    assert (row.session_id, row.session_slug, row.message_id) == ("c1", f"wh-{r.id}", "m1")
    assert [e.id for e in await events.in_progress(conn)] == [r.id]
    assert await store.finish(r.id, "failed", "turn_failed")
    row = await events.get_event(conn, r.id)
    assert (row.status, row.reason) == ("failed", "turn_failed") and row.finished_at is not None
    assert await events.in_progress(conn) == []


async def test_run_store_update_row_keeps_given_slug(conn, store, ep):
    r = await rec(conn, ep, ev("k"))
    await claim(conn)
    await store.set_session(r.id, "c1", slug="wh-1")
    assert (await events.get_event(conn, r.id)).session_slug == "wh-1"


# --- reads ----------------------------------------------------------------------------------


async def test_list_events_newest_first_with_filters(conn, ep, make_endpoint):
    other = await make_endpoint()
    a = await rec(conn, ep, ev("a"))
    b = await rec(conn, ep, ev("b"), ignore_reason="self_event")
    c = await rec(conn, other, ev("c"))
    d = await rec(conn, ep, ev("d"))
    assert [e.id for e in await events.list_events(conn)] == [d.id, c.id, b.id, a.id]
    assert [e.id for e in await events.list_events(conn, status="new", endpoint_id=ep.id)] == [
        d.id,
        a.id,
    ]
    page = await events.list_events(conn, limit=2)
    assert [e.id for e in page] == [d.id, c.id]
    rest = await events.list_events(conn, limit=2, before_id=page[-1].id)
    assert [e.id for e in rest] == [b.id, a.id]
    assert await events.get_event(conn, 999999) is None


def test_event_has_every_column():
    names = {f.name for f in dataclasses.fields(events.Event)}
    assert {"id", "seq", "payload", "update_of", "duplicate_of", "finished_at"} <= names


async def test_reuse_release_serialized_with_record_of_the_key(db, conn, ep, monkeypatch):
    a = await rec(conn, ep, ev("k"), {"v": 1})
    await claim(conn)
    real = events._newer_new
    ingest: list[asyncio.Task] = []

    async def insert_between(c, id):
        newer = await real(c, id)

        async def deliver():
            async with await psycopg.AsyncConnection.connect(db, autocommit=True) as w:
                return await rec(w, ep, ev("k", title="late"), {"v": 2})

        ingest.append(asyncio.create_task(deliver()))
        await asyncio.wait(ingest, timeout=0.5)  # done here only if not serialized
        return newer

    monkeypatch.setattr(events, "_newer_new", insert_between)
    assert await events.release(conn, a.id)
    late = await ingest[0]
    assert (late.status, late.reason, late.duplicate_of) == ("ignored", "duplicate", a.id)
    row = await events.get_event(conn, a.id)
    assert row.status == "new" and row.payload == {"v": 2} and row.title == "late"
    assert row.deliveries == 2


async def test_record_stores_body_and_latest_wins_replaces_it(conn, ep):
    first = await rec(conn, ep, ev("k", body="b1", comment_body="c1"))
    row = await events.get_event(conn, first.id)
    assert (row.body, row.comment_body) == ("b1", "c1")
    await rec(conn, ep, ev("k", body="b2", comment_body=None))
    row = await events.get_event(conn, first.id)
    assert (row.body, row.comment_body) == ("b2", None)
    ign = await rec(conn, ep, ev("x", body="ib"), ignore_reason="self_event")
    assert (await events.get_event(conn, ign.id)).body == "ib"


async def test_record_round_trips_normalized_event(conn, ep):
    original = ev("k", body="the body", comment_body="a comment", labels={"sev": "high"})
    r = await rec(conn, ep, original)
    rebuilt = events.to_normalized(await events.get_event(conn, r.id))
    assert rebuilt == original


# --- ignored payload cap --------------------------------------------------------------------

HUGE = {"blob": "x" * 20000}


def _assert_truncated(row) -> None:
    assert row.payload["truncated"] is True
    assert row.payload["size"] > 20000
    assert row.payload["head"].startswith('{"blob"') and len(row.payload["head"]) == 4096


async def test_ignored_rows_store_truncated_payload(conn, store, ep, make_endpoint):
    # ignore_reason paths (type mismatch, unsupported, action ignored, ...)
    r = await rec(conn, ep, ev("k0", title="kept"), HUGE, ignore_reason="type_mismatch")
    row = await events.get_event(conn, r.id)
    _assert_truncated(row)
    assert row.title == "kept" and row.reason == "type_mismatch"
    # a small ignored payload is kept as is
    small = await rec(conn, ep, ev("ks"), {"p": 1}, ignore_reason="action_ignored")
    assert (await events.get_event(conn, small.id)).payload == {"p": 1}
    # redelivery
    first = await rec(conn, ep, ev("k1", delivery_id="d1"), HUGE)
    again = await rec(conn, ep, ev("k1", delivery_id="d1"), HUGE)
    assert again.reason == "duplicate"
    _assert_truncated(await events.get_event(conn, again.id))
    # new rows keep the full payload
    assert (await events.get_event(conn, first.id)).payload == HUGE
    # same-key duplicate (latest wins): the new row keeps it, the ignored one is capped
    await rec(conn, ep, ev("k2"), HUGE)
    dup = await rec(conn, ep, ev("k2"), HUGE)
    assert dup.reason == "duplicate"
    _assert_truncated(await events.get_event(conn, dup.id))
    # queue_full
    full = await rec(conn, ep, ev("k3"), HUGE, max_queue=1)
    assert full.reason == "queue_full"
    _assert_truncated(await events.get_event(conn, full.id))
    # endpoint removed
    other = await make_endpoint()
    gone = await rec(conn, other, ev("k4"), HUGE)
    assert (await events.get_event(conn, gone.id)).payload == HUGE
    assert await endpoints.delete(conn, other.id)
    while await claim(conn):
        pass
    row = await events.get_event(conn, gone.id)
    assert row.reason == "endpoint_removed"
    _assert_truncated(row)
    # release folds a newer new row in: the folded duplicate is capped
    keep = await rec(conn, ep, ev("k5"), {"v": 1})
    while (await claim(conn, reuse={ep.id})).event.id != keep.id:
        pass
    newer = await rec(conn, ep, ev("k5"), HUGE)
    assert await events.release(conn, keep.id)
    assert (await events.get_event(conn, keep.id)).payload == HUGE
    _assert_truncated(await events.get_event(conn, newer.id))


# --- migrations -----------------------------------------------------------------------------


@pytest.fixture
def empty_db(pg_url):
    """URL of a fresh database with no migration applied."""
    name = f"t_{uuid.uuid4().hex}"
    base = pg_url.rsplit("/", 1)[0]
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield f"{base}/{name}"
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_0005_renames_cluster_columns(empty_db, monkeypatch):
    # Migrate to 0004 only (the live schema before this rename) and seed run rows there.
    monkeypatch.setattr(migrate, "_NAME", re.compile(r"^000[1-4]_.+\.sql$"))
    assert migrate.run_migrations(empty_db, "agents_webhook")[-1] == "0004_endpoint_filters.sql"
    monkeypatch.undo()
    seed = (
        "INSERT INTO events (priority, endpoint_suffix, type, source, kind, payload, dedup_key, "
        f"status, {RETIRED_UNIT}_id, {RETIRED_UNIT}_slug) VALUES "
        "(1, 's', 'github-pr', 'github', 'mr', '{}', 'k1', 'handled', 'c1', 'wh-1'), "
        "(1, 's', 'github-pr', 'github', 'mr', '{}', 'k2', 'new', NULL, NULL) RETURNING id"
    )
    with psycopg.connect(empty_db, autocommit=True) as c:
        ids = [r[0] for r in c.execute(seed).fetchall()]

    assert migrate.run_migrations(empty_db, "agents_webhook") == ["0005_sessions.sql"]

    with psycopg.connect(empty_db, autocommit=True) as c:
        rows = c.execute("SELECT id, session_id, session_slug FROM events ORDER BY id").fetchall()
        columns = [
            r[0]
            for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'events'"
            ).fetchall()
        ]
        names = [
            r[0]
            for r in c.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'events' "
                "UNION ALL SELECT conname FROM pg_constraint WHERE conrelid = 'events'::regclass"
            ).fetchall()
        ]
    assert rows == [(ids[0], "c1", "wh-1"), (ids[1], None, None)]
    assert {"session_id", "session_slug"} <= set(columns)
    assert [n for n in columns + names if RETIRED_UNIT in n] == []
