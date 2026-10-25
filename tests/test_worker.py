import asyncio
import logging
import uuid

import httpx
import psycopg
import pytest
from agents_common.db import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient
from agents_common.runs import marker
from agents_common.runtime import LostLock

from agents_webhook import prompt, worker
from agents_webhook.db import endpoints, events
from agents_webhook.db.endpoints import EndpointIn
from agents_webhook.normalize import NormalizedEvent
from agents_webhook.settings import WebhookSettings

PAYLOAD = {"pull_request": {"number": 1}, "x": "from-payload"}


def ev(key: str = "github/o/r/pr/1", **over) -> NormalizedEvent:
    base = {
        "type": "github-pr",
        "source": "github",
        "kind": "mr",
        "action": "opened",
        "repo": "o/r",
        "number": 1,
        "title": "Fix the thing",
        "body": "Body <b>",
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


class Switch(httpx.AsyncBaseTransport):
    """ASGI transport to the fake that can be taken down and counts the requests it carried."""

    def __init__(self, fake: FakeOrchestrator):
        self._inner = httpx.ASGITransport(app=fake.app)
        self.down = False
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("orchestrator down", request=request)
        return await self._inner.handle_async_request(request)


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
def preset(fake) -> str:
    return str(fake.add_preset("dev").id)


@pytest.fixture
def switch(fake) -> Switch:
    return Switch(fake)


@pytest.fixture
async def client(fake, switch):
    async with OrchestratorClient("http://orch", fake.token, transport=switch) as c:
        yield c


@pytest.fixture
async def pool(db):
    p = await open_pool(db, max_size=6)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def conn(db):
    async with await psycopg.AsyncConnection.connect(db, autocommit=True) as c:
        yield c


def make_settings(db: str, **over) -> WebhookSettings:
    values = {
        "database_url": db,
        "orchestrator_url": "http://orch",
        "orchestrator_token": "test-token",
        "poll_seconds": 0.05,
        "max_in_progress": 3,
        "max_in_progress_per_endpoint": 1,
    }
    values.update(over)
    return WebhookSettings(**values)


@pytest.fixture
def settings(db) -> WebhookSettings:
    return make_settings(db)


@pytest.fixture
def make_worker(pool, client, settings):
    def make(**over) -> worker.Worker:
        s = settings.model_copy(update=over) if over else settings
        return worker.Worker(s, pool, client)

    return make


@pytest.fixture
async def make_endpoint(conn, preset):
    async def make(template: str | None = None, preset_id: str | None = None):
        ep, _ = await endpoints.create(
            conn,
            EndpointIn(type="github-pr", preset_id=preset_id or preset, prompt_template=template),
            min_password_length=16,
        )
        return ep

    return make


async def rec(conn, endpoint, event: NormalizedEvent | None = None) -> int:
    recorded = await events.record(
        conn, endpoint, event or ev(), PAYLOAD, ignore_reason=None, max_queue=1000
    )
    assert recorded.status == "new"
    return recorded.id


async def row(conn, id: int) -> events.Event:
    event = await events.get_event(conn, id)
    assert event is not None
    return event


async def statuses(conn, ids) -> list[str]:
    return [(await row(conn, i)).status for i in ids]


def cluster_of(fake: FakeOrchestrator, slug: str) -> dict:
    found = [c for c in fake.clusters.values() if c["slug"] == slug]
    assert len(found) == 1, f"{len(found)} clusters with slug {slug}"
    return found[0]


def messages_of(fake: FakeOrchestrator, cluster_id: str) -> list[dict]:
    return [m for m in fake.messages.values() if m["cluster_id"] == cluster_id]


# --- happy path -----------------------------------------------------------------------------


async def test_happy_path_new_in_progress_handled(conn, fake, preset, make_endpoint, make_worker):
    ep = await make_endpoint()
    id = await rec(conn, ep)
    w = make_worker()

    await w.tick()

    r = await row(conn, id)
    assert r.status == "in-progress"
    assert r.cluster_slug == f"wh-{id}"
    cluster = cluster_of(fake, f"wh-{id}")
    assert r.cluster_id == cluster["id"]
    assert cluster["preset_id"] == preset
    msgs = messages_of(fake, cluster["id"])
    assert len(msgs) == 1
    assert r.message_id == str(msgs[0]["id"])
    expected = prompt.render(events.to_normalized(r), None)
    assert msgs[0]["text"] == f"{expected}\n\n{marker('webhook:event', id)}"
    assert "<!-- agents-webhook:event:" in msgs[0]["text"]

    await w.tick()
    assert (await row(conn, id)).status == "in-progress"

    fake.complete_turn(msgs[0]["id"], "done")
    await w.tick()
    r = await row(conn, id)
    assert r.status == "handled"
    assert r.reason is None
    assert r.finished_at is not None
    assert len(fake.clusters) == 1


async def test_happy_path_posts_the_endpoint_template(conn, fake, make_endpoint, make_worker):
    ep = await make_endpoint(template="Please fix {{ title }} in {{ repo }} ({{ payload.x }})")
    id = await rec(conn, ep)

    await make_worker().tick()

    cluster = cluster_of(fake, f"wh-{id}")
    [msg] = messages_of(fake, cluster["id"])
    assert msg["text"] == (
        f"Please fix Fix the thing in o/r (from-payload)\n\n{marker('webhook:event', id)}"
    )


async def test_happy_notify_wakes_the_loop(db, conn, fake, pool, client, make_endpoint):
    ep = await make_endpoint()
    stop = asyncio.Event()
    task = asyncio.create_task(
        worker.run_worker(make_settings(db, poll_seconds=60), pool, client, stop)
    )
    try:
        await asyncio.sleep(0.5)
        id = await rec(conn, ep)
        await conn.execute("SELECT pg_notify('webhook_events', %s)", (str(id),))
        await _until(lambda: _status(conn, id), "in-progress", timeout=5)
        cluster_of(fake, f"wh-{id}")
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


# --- caps -----------------------------------------------------------------------------------


async def test_caps_overall_fourth_waits_fifo(conn, fake, make_endpoint, make_worker):
    eps = [await make_endpoint() for _ in range(5)]
    ids = [await rec(conn, ep) for ep in eps]
    w = make_worker(max_in_progress=3, max_in_progress_per_endpoint=1)

    await w.tick()
    assert await statuses(conn, ids) == ["in-progress"] * 3 + ["new", "new"]

    await w.tick()
    assert await statuses(conn, ids) == ["in-progress"] * 3 + ["new", "new"]

    second = await row(conn, ids[1])
    fake.complete_turn(int(second.message_id), "ok")
    await w.tick()
    assert await statuses(conn, ids) == [
        "in-progress",
        "handled",
        "in-progress",
        "in-progress",
        "new",
    ]
    assert len(fake.clusters) == 4


async def test_caps_per_endpoint_one_after_the_other(conn, fake, make_endpoint, make_worker):
    ep = await make_endpoint()
    first = await rec(conn, ep, ev("github/o/r/pr/1"))
    second = await rec(conn, ep, ev("github/o/r/pr/2", number=2))
    w = make_worker(max_in_progress=3, max_in_progress_per_endpoint=1)

    await w.tick()
    assert await statuses(conn, [first, second]) == ["in-progress", "new"]

    fake.complete_turn(int((await row(conn, first)).message_id), "ok")
    await w.tick()
    assert await statuses(conn, [first, second]) == ["handled", "in-progress"]
    cluster_of(fake, f"wh-{second}")


# --- same key, reuse off --------------------------------------------------------------------


async def test_same_key_waits_then_starts_its_own_cluster(conn, fake, make_endpoint, make_worker):
    ep = await make_endpoint()
    first = await rec(conn, ep)
    w = make_worker(max_in_progress=5, max_in_progress_per_endpoint=5)
    await w.tick()
    second = await rec(conn, ep, ev(action="synchronize"))

    await w.tick()
    assert await statuses(conn, [first, second]) == ["in-progress", "new"]

    fake.complete_turn(int((await row(conn, first)).message_id), "ok")
    await w.tick()
    assert await statuses(conn, [first, second]) == ["handled", "in-progress"]
    a, b = await row(conn, first), await row(conn, second)
    assert b.cluster_slug == f"wh-{second}"
    assert b.cluster_id != a.cluster_id
    assert b.update_of is None
    assert len(fake.clusters) == 2


# --- restart --------------------------------------------------------------------------------


class Crash(Exception):
    pass


async def test_restart_after_create_makes_no_second_cluster(
    conn, fake, make_endpoint, make_worker, monkeypatch
):
    ep = await make_endpoint()
    id = await rec(conn, ep)
    original = events.EventStore.set_cluster

    async def crash(self, run_id, cluster_id, *, slug=None):
        raise Crash("killed after create")

    monkeypatch.setattr(events.EventStore, "set_cluster", crash)
    with pytest.raises(Crash):
        await make_worker().tick()
    assert len(fake.clusters) == 1
    r = await row(conn, id)
    assert (r.status, r.cluster_id, r.message_id) == ("in-progress", None, None)

    monkeypatch.setattr(events.EventStore, "set_cluster", original)
    await make_worker().tick()

    assert len(fake.clusters) == 1
    r = await row(conn, id)
    assert r.cluster_id == cluster_of(fake, f"wh-{id}")["id"]
    assert len(fake.messages) == 1
    assert r.message_id == str(next(iter(fake.messages)))


async def test_restart_after_post_makes_no_second_message(
    conn, fake, make_endpoint, make_worker, monkeypatch
):
    ep = await make_endpoint()
    id = await rec(conn, ep)
    original = events.EventStore.set_message

    async def crash(self, run_id, message_id):
        raise Crash("killed after post")

    monkeypatch.setattr(events.EventStore, "set_message", crash)
    with pytest.raises(Crash):
        await make_worker().tick()
    r = await row(conn, id)
    assert r.cluster_id is not None and r.message_id is None
    assert len(fake.messages) == 1

    monkeypatch.setattr(events.EventStore, "set_message", original)
    await make_worker().tick()

    r = await row(conn, id)
    assert len(fake.clusters) == 1
    assert len(fake.messages) == 1
    assert r.message_id == str(next(iter(fake.messages)))
    fake.complete_turn(int(r.message_id), "ok")
    await make_worker().tick()
    assert (await row(conn, id)).status == "handled"


async def test_restart_resumes_before_new_claims(conn, fake, make_endpoint, make_worker):
    ep = await make_endpoint()
    id = await rec(conn, ep)
    async with conn.transaction():
        claimed = await events.claim_next(
            conn, max_in_progress=1, max_per_endpoint=1, reuse_endpoints=set()
        )
    assert claimed is not None and claimed.event.id == id
    other = await rec(conn, await make_endpoint())

    await make_worker(max_in_progress=1).tick()

    assert await statuses(conn, [id, other]) == ["in-progress", "new"]
    assert (await row(conn, id)).message_id is not None
    assert len(fake.clusters) == 1


# --- failed paths ---------------------------------------------------------------------------


async def test_failed_preset_missing(conn, fake, make_endpoint, make_worker):
    ep = await make_endpoint(preset_id=str(uuid.uuid4()))
    id = await rec(conn, ep)

    await make_worker().tick()

    r = await row(conn, id)
    assert (r.status, r.reason) == ("failed", "preset_not_found")
    assert r.finished_at is not None
    assert fake.clusters == {}


async def test_failed_4xx_on_create_or_post_records_the_code(
    conn, fake, make_endpoint, make_worker
):
    first = await rec(conn, await make_endpoint())
    second = await rec(conn, await make_endpoint())
    fake.fail_next("create_cluster", 422)
    fake.fail_next("post_message", 400)

    await make_worker(max_in_progress=1).tick()
    await make_worker(max_in_progress=1).tick()

    a, b = await row(conn, first), await row(conn, second)
    assert (a.status, a.reason) == ("failed", "internal")
    assert (b.status, b.reason) == ("failed", "internal")
    assert b.cluster_id is not None
    assert fake.messages == {}


async def test_failed_turn(conn, fake, make_endpoint, make_worker):
    id = await rec(conn, await make_endpoint())
    w = make_worker()
    await w.tick()

    fake.fail_turn(int((await row(conn, id)).message_id))
    await w.tick()

    r = await row(conn, id)
    assert r.status == "failed"
    assert r.reason == "message_failed"


async def test_failed_timeout(conn, fake, make_endpoint, make_worker):
    id = await rec(conn, await make_endpoint())
    w = make_worker(run_timeout_seconds=3600)
    await w.tick()
    await conn.execute(
        "UPDATE events SET started_at = now() - interval '2 hours' WHERE id = %s", (id,)
    )

    await w.tick()

    r = await row(conn, id)
    assert (r.status, r.reason) == ("failed", "timeout")


async def test_failed_template_error(conn, fake, make_endpoint, make_worker):
    id = await rec(conn, await make_endpoint(template="{{ no_such_variable }}"))

    await make_worker().tick()

    r = await row(conn, id)
    assert (r.status, r.reason) == ("failed", "template_error")
    assert fake.clusters == {}


async def test_failed_orchestrator_down_leaves_rows_then_recovers(
    conn, fake, switch, make_endpoint, make_worker, caplog
):
    id = await rec(conn, await make_endpoint())
    w = make_worker()
    switch.down = True
    caplog.set_level(logging.WARNING, logger="agents_webhook.worker")

    await w.tick()
    await w.tick()

    r = await row(conn, id)
    assert (r.status, r.reason, r.cluster_id, r.message_id) == ("in-progress", None, None, None)
    assert fake.clusters == {}
    unavailable = [m for m in caplog.messages if "unavailable" in m and f"event {id}" in m]
    assert len(unavailable) == 1

    switch.down = False
    await w.tick()
    r = await row(conn, id)
    assert r.message_id is not None

    switch.down = True
    await w.tick()
    assert (await row(conn, id)).status == "in-progress"

    switch.down = False
    fake.complete_turn(int(r.message_id), "ok")
    await w.tick()
    assert (await row(conn, id)).status == "handled"
    assert len(fake.clusters) == 1 and len(fake.messages) == 1


# --- route hook -----------------------------------------------------------------------------


async def test_route_hook_true_skips_first_dispatch(
    conn, fake, pool, client, settings, make_endpoint
):
    seen: list[int] = []

    class Routed(worker.Worker):
        async def route(self, claimed: events.Claimed) -> bool:
            seen.append(claimed.event.id)
            return True

    id = await rec(conn, await make_endpoint())
    await Routed(settings, pool, client).tick()

    assert seen == [id]
    assert fake.clusters == {}
    assert (await row(conn, id)).status == "in-progress"


async def test_route_hook_default_is_first_dispatch(make_worker, conn, make_endpoint):
    w = make_worker()
    id = await rec(conn, await make_endpoint())
    async with conn.transaction():
        claimed = await events.claim_next(
            conn, max_in_progress=1, max_per_endpoint=1, reuse_endpoints=set()
        )
    assert claimed is not None and claimed.event.id == id
    assert await w.route(claimed) is False


# --- singleton ------------------------------------------------------------------------------


async def _status(conn, id: int) -> str:
    return (await row(conn, id)).status


async def _until(probe, expected, timeout: float) -> None:
    async with asyncio.timeout(timeout):
        while (value := await probe()) != expected:
            await asyncio.sleep(0.05)
    assert value == expected


async def test_singleton_only_one_worker_dispatches(db, conn, fake, make_endpoint):
    ep = await make_endpoint()
    first_pool = await open_pool(db, max_size=4)
    second_pool = await open_pool(db, max_size=4)
    first_switch, second_switch = Switch(fake), Switch(fake)
    first_client = OrchestratorClient("http://orch", fake.token, transport=first_switch)
    second_client = OrchestratorClient("http://orch", fake.token, transport=second_switch)
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    settings = make_settings(db, max_in_progress=5, max_in_progress_per_endpoint=5)
    try:
        first = asyncio.create_task(
            worker.run_worker(settings, first_pool, first_client, first_stop)
        )
        await asyncio.sleep(0.3)
        second = asyncio.create_task(
            worker.run_worker(settings, second_pool, second_client, second_stop)
        )
        ids = [await rec(conn, ep, ev(f"k{i}")) for i in range(3)]
        for id in ids:
            await _until(lambda id=id: _status(conn, id), "in-progress", timeout=5)
        await asyncio.sleep(0.3)
        assert len(fake.clusters) == 3
        assert second_switch.calls == 0
        assert first_switch.calls > 0

        first_stop.set()
        await asyncio.wait_for(first, 5)
        late = await rec(conn, ep, ev("late"))
        await _until(lambda: _status(conn, late), "in-progress", timeout=5)
        assert second_switch.calls > 0
        assert len(fake.clusters) == 4
        second_stop.set()
        await asyncio.wait_for(second, 5)
    finally:
        first_stop.set()
        second_stop.set()
        await first_client.aclose()
        await second_client.aclose()
        await first_pool.close()
        await second_pool.close()


async def test_singleton_lost_lock_stops_with_lost_lock(db, conn, pool, client, make_endpoint):
    task = asyncio.create_task(
        worker.run_worker(make_settings(db), pool, client, asyncio.Event(), heartbeat_seconds=0.1)
    )
    await asyncio.sleep(0.5)
    await conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_locks "
        "WHERE locktype = 'advisory' AND pid <> pg_backend_pid() "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    with pytest.raises(LostLock):
        await asyncio.wait_for(task, 5)
