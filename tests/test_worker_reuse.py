"""Agent reuse routing in the worker (design D16, D17)."""

import psycopg
import pytest
from agents_common.db import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient, OrchestratorUnavailable
from agents_common.runs import marker
from test_worker import Switch, cluster_of, ev, make_settings, messages_of, rec, row, statuses

from agents_webhook import prompt, worker
from agents_webhook.db import endpoints, events
from agents_webhook.db.endpoints import EndpointIn

KEY = "github/o/r/pr/1"
OTHER = "github/o/r/pr/2"


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator()


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


@pytest.fixture
def make_worker(db, pool, client):
    def make(**over) -> worker.Worker:
        values = {"max_in_progress": 5, "max_in_progress_per_endpoint": 5, **over}
        return worker.Worker(make_settings(db, **values), pool, client)

    return make


@pytest.fixture
async def make_endpoint(conn, fake):
    async def make(spec: dict | None = None, *, preset_id: str | None = None):
        pid = preset_id or str(fake.add_preset("dev", spec=spec).id)
        ep, _ = await endpoints.create(
            conn, EndpointIn(type="github-pr", preset_id=pid), min_password_length=16
        )
        return ep

    return make


REUSE = {"reuse_running_agent": True}


def update_text(r: events.Event) -> str:
    text = prompt.render(events.to_normalized(r), None, update=True)
    return f"{text}\n\n{marker('webhook:event', r.id)}"


def first_text(r: events.Event) -> str:
    return f"{prompt.render(events.to_normalized(r), None)}\n\n{marker('webhook:event', r.id)}"


def preset_reads(fake: FakeOrchestrator) -> int:
    return sum(1 for r in fake.requests if r.url.path.startswith("/api/v1/presets/"))


async def start_first(conn, fake, w, ep, key: str = KEY) -> tuple[events.Event, dict]:
    """Record and dispatch the first event of `key`; its row and cluster."""
    id = await rec(conn, ep, ev(key))
    await w.tick()
    first = await row(conn, id)
    assert first.status == "in-progress" and first.message_id is not None
    return first, cluster_of(fake, f"wh-{id}")


# --- reuse on -------------------------------------------------------------------------------


async def test_reuse_on_while_first_turn_runs_goes_to_the_same_cluster(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)

    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()

    r = await row(conn, second)
    assert r.status == "in-progress"
    assert r.update_of == first.id
    assert (r.cluster_id, r.cluster_slug) == (cluster["id"], f"wh-{first.id}")
    msgs = messages_of(fake, cluster["id"])
    assert [m["id"] for m in msgs] == [int(first.message_id), int(r.message_id)]
    assert msgs[1]["text"] == update_text(r)
    assert msgs[1]["text"].startswith(prompt.UPDATE_PREFIX)
    assert len(fake.clusters) == 1

    fake.mark_sent(int(r.message_id))
    await w.tick()
    r = await row(conn, second)
    assert (r.status, r.reason, r.update_of) == ("handled", None, first.id)
    assert r.finished_at is not None
    assert (await row(conn, first.id)).status == "in-progress"
    assert len(fake.clusters) == 1


async def test_reuse_on_after_first_turn_completed_goes_to_the_same_cluster(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    fake.complete_turn(int(first.message_id), "done")
    await w.tick()
    assert (await row(conn, first.id)).status == "handled"

    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    r = await row(conn, second)
    assert (r.status, r.update_of, r.cluster_id) == ("in-progress", first.id, cluster["id"])

    fake.mark_sent(int(r.message_id))
    await w.tick()
    r = await row(conn, second)
    assert (r.status, r.update_of) == ("handled", first.id)
    assert len(fake.clusters) == 1
    assert len(messages_of(fake, cluster["id"])) == 2


async def test_reuse_on_preset_flag_read_once_per_tick(conn, fake, make_endpoint, make_worker):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    await rec(conn, ep, ev(KEY))
    await rec(conn, ep, ev(OTHER, number=2))
    await rec(conn, ep, ev("github/o/r/pr/3", number=3))

    before = preset_reads(fake)
    await w.tick()
    assert preset_reads(fake) - before == 1

    before = preset_reads(fake)
    await w.tick()  # nothing new: no read
    assert preset_reads(fake) - before == 0


async def test_reuse_on_flag_change_takes_effect_for_later_events(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint()
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    fake.presets[ep.preset_id]["spec"] = dict(REUSE)

    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    r = await row(conn, second)
    assert (r.update_of, r.cluster_id) == (first.id, cluster["id"])


async def test_reuse_on_unavailable_preset_read_skips_the_endpoint(
    conn, fake, switch, make_endpoint, make_worker, monkeypatch
):
    down_ep = await make_endpoint(REUSE)
    up_ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, down_ep)
    original = OrchestratorClient.get_preset

    async def get_preset(self, preset_id):
        if str(preset_id) == down_ep.preset_id:
            raise OrchestratorUnavailable("down")
        return await original(self, preset_id)

    monkeypatch.setattr(OrchestratorClient, "get_preset", get_preset)
    skipped = await rec(conn, down_ep, ev(action="synchronize"))
    other = await rec(conn, up_ep, ev(OTHER, number=2))
    await w.tick()

    s = await row(conn, skipped)
    assert (s.status, s.update_of, s.cluster_id) == ("new", None, None)
    assert (await row(conn, other)).status == "in-progress"
    assert len(messages_of(fake, cluster["id"])) == 1

    monkeypatch.setattr(OrchestratorClient, "get_preset", original)
    await w.tick()
    s = await row(conn, skipped)
    assert (s.status, s.update_of, s.cluster_id) == ("in-progress", first.id, cluster["id"])


# --- reuse off ------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [None, {"reuse_running_agent": False}], ids=["unset", "false"])
async def test_reuse_off_starts_its_own_cluster_after_the_first_ends(
    conn, fake, make_endpoint, make_worker, spec
):
    ep = await make_endpoint(spec)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)

    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    assert await statuses(conn, [first.id, second]) == ["in-progress", "new"]
    assert len(messages_of(fake, cluster["id"])) == 1

    fake.complete_turn(int(first.message_id), "ok")
    await w.tick()
    r = await row(conn, second)
    assert (r.status, r.update_of, r.cluster_slug) == ("in-progress", None, f"wh-{second}")
    assert r.cluster_id != cluster["id"]
    assert messages_of(fake, r.cluster_id)[0]["text"] == first_text(r)
    assert len(fake.clusters) == 2


async def test_reuse_off_missing_preset_fails_preset_not_found(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(preset_id="00000000-0000-0000-0000-000000000000")
    id = await rec(conn, ep)
    await make_worker().tick()
    r = await row(conn, id)
    assert (r.status, r.reason) == ("failed", "preset_not_found")
    assert fake.clusters == {}


# --- order ----------------------------------------------------------------------------------


async def test_order_three_updates_delivered_one_at_a_time_in_arrival_order(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)

    ids: list[int] = []
    for n in range(3):
        ids.append(await rec(conn, ep, ev(action="synchronize", comment_body=f"update {n}")))
        await w.tick()
        await w.tick()  # the previous delivery is not sent: nothing else is posted
        delivered = await row(conn, ids[-1])
        assert (delivered.status, delivered.update_of) == ("in-progress", first.id)
        msgs = messages_of(fake, cluster["id"])
        assert len(msgs) == n + 2
        # Never merged: each message carries exactly one event's marker.
        assert msgs[-1]["text"] == update_text(delivered)
        assert msgs[-1]["text"].count("<!-- agents-webhook:event:") == 1
        fake.mark_sent(int(delivered.message_id))

    await w.tick()
    assert await statuses(conn, ids) == ["handled"] * 3
    msgs = messages_of(fake, cluster["id"])
    assert [m["id"] for m in msgs] == sorted(m["id"] for m in msgs)
    assert [m["text"].rsplit("\n\n", 1)[1] for m in msgs] == [
        marker("webhook:event", i) for i in [first.id, *ids]
    ]
    assert len(fake.clusters) == 1


async def test_order_next_waits_while_a_delivery_is_queued_behind_a_busy_turn(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)

    a = await rec(conn, ep, ev(action="synchronize", comment_body="a"))
    await w.tick()
    b = await rec(conn, ep, ev(action="synchronize", comment_body="b"))
    folded = await events.record(
        conn, ep, ev(action="synchronize", comment_body="c"), {}, ignore_reason=None, max_queue=99
    )
    c = folded.id  # latest wins: folds into b
    await w.tick()

    assert await statuses(conn, [a, b, c]) == ["in-progress", "new", "ignored"]
    assert len(messages_of(fake, cluster["id"])) == 2

    fake.mark_sent(int((await row(conn, a)).message_id))
    await w.tick()
    assert await statuses(conn, [a, b]) == ["handled", "in-progress"]
    rb = await row(conn, b)
    assert rb.comment_body == "c"
    assert messages_of(fake, cluster["id"])[-1]["text"] == update_text(rb)


# --- discard and autoclose ------------------------------------------------------------------


@pytest.mark.parametrize("how", ["discard", "remove"])
async def test_discard_before_delivery_starts_a_new_cluster(
    conn, fake, make_endpoint, make_worker, how
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    fake.complete_turn(int(first.message_id), "ok")
    await w.tick()
    getattr(fake, f"{how}_cluster")(cluster["id"])

    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()

    r = await row(conn, second)
    assert (r.status, r.update_of, r.cluster_slug) == ("in-progress", None, f"wh-{second}")
    new = cluster_of(fake, f"wh-{second}")
    assert messages_of(fake, new["id"])[0]["text"] == first_text(r)

    fake.complete_turn(int(r.message_id), "ok")
    await w.tick()
    r = await row(conn, second)
    assert (r.status, r.reason) == ("handled", None)


async def test_discard_mid_delivery_message_cancelled_starts_a_new_cluster(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    delivered = await row(conn, second)
    assert delivered.update_of == first.id

    fake.discard_cluster(cluster["id"])  # cancels the queued update message
    await w.tick()

    r = await row(conn, second)
    assert (r.status, r.update_of, r.cluster_slug) == ("in-progress", None, f"wh-{second}")
    new = cluster_of(fake, f"wh-{second}")
    [msg] = messages_of(fake, new["id"])
    assert msg["text"] == first_text(r)
    assert r.message_id == str(msg["id"])

    fake.complete_turn(msg["id"], "ok")
    await w.tick()
    assert (await row(conn, second)).status == "handled"


async def test_discard_race_post_answers_409_starts_a_new_cluster(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    fake.fail_next("post_message", 409)

    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()

    r = await row(conn, second)
    assert (r.status, r.update_of, r.cluster_slug) == ("in-progress", None, f"wh-{second}")
    assert len(messages_of(fake, cluster["id"])) == 1
    cluster_of(fake, f"wh-{second}")


async def test_discard_autoclose_deleting_ends_reuse(conn, fake, make_endpoint, make_worker):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    fake.complete_turn(int(first.message_id), "ok")
    await w.tick()
    fake.clusters[cluster["id"]]["desired"] = "deleting"

    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    r = await row(conn, second)
    assert (r.update_of, r.cluster_slug) == (None, f"wh-{second}")
    assert len(messages_of(fake, cluster["id"])) == 1


async def test_discard_fallback_happens_once_and_a_failure_records_reuse_fallback(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    fake.discard_cluster(cluster["id"])
    fake.fail_next("create_cluster", 422)

    await w.tick()
    r = await row(conn, second)
    assert r.status == "failed"
    assert r.reason == "reuse_fallback: internal"
    assert r.update_of is None


async def test_discard_new_cluster_becomes_the_target_of_later_events(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    fake.complete_turn(int(first.message_id), "ok")
    await w.tick()
    fake.discard_cluster(cluster["id"])
    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    new = cluster_of(fake, f"wh-{second}")

    third = await rec(conn, ep, ev(action="synchronize", comment_body="again"))
    await w.tick()
    r = await row(conn, third)
    assert (r.update_of, r.cluster_id, r.cluster_slug) == (second, new["id"], f"wh-{second}")


# --- caps -----------------------------------------------------------------------------------


async def test_caps_full_reuse_delivery_still_goes_and_a_fallback_row_is_released(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    w = make_worker(max_in_progress=1, max_in_progress_per_endpoint=1)
    # OTHER had a run whose cluster is gone.
    old, old_cluster = await start_first(conn, fake, w, ep, OTHER)
    fake.complete_turn(int(old.message_id), "ok")
    await w.tick()
    fake.discard_cluster(old_cluster["id"])
    # KEY runs now and fills the caps.
    first, cluster = await start_first(conn, fake, w, ep, KEY)

    update = await rec(conn, ep, ev(KEY, action="synchronize"))
    fallback = await rec(conn, ep, ev(OTHER, number=2, action="synchronize"))
    seq = (await row(conn, fallback)).seq
    await w.tick()

    u = await row(conn, update)
    assert (u.status, u.update_of, u.cluster_id) == ("in-progress", first.id, cluster["id"])
    f = await row(conn, fallback)
    assert (f.status, f.update_of, f.cluster_id, f.started_at) == ("new", None, None, None)
    assert f.seq == seq
    assert len(fake.clusters) == 2

    await w.tick()  # still full: stays new, no new cluster
    assert (await row(conn, fallback)).status == "new"
    assert len(fake.clusters) == 2

    fake.complete_turn(int(first.message_id), "ok")
    fake.mark_sent(int(u.message_id))
    await w.tick()
    assert await statuses(conn, [first.id, update]) == ["handled", "handled"]
    f = await row(conn, fallback)
    assert (f.status, f.update_of, f.cluster_slug) == ("in-progress", None, f"wh-{fallback}")
    assert messages_of(fake, f.cluster_id)[0]["text"] == first_text(f)

    fake.complete_turn(int(f.message_id), "ok")
    await w.tick()
    f = await row(conn, fallback)
    assert (f.status, f.reason) == ("handled", None)


async def test_caps_mid_delivery_fallback_released_when_full(
    conn, fake, make_endpoint, make_worker
):
    ep = await make_endpoint(REUSE)
    other_ep = await make_endpoint()
    w = make_worker(max_in_progress=2, max_in_progress_per_endpoint=2)
    first, cluster = await start_first(conn, fake, w, ep)
    second = await rec(conn, ep, ev(action="synchronize"))
    await w.tick()
    assert (await row(conn, second)).update_of == first.id
    filler = await rec(conn, other_ep, ev(OTHER, number=2))
    await w.tick()
    assert (await row(conn, filler)).status == "in-progress"

    fake.clusters[cluster["id"]]["desired"] = "deleting"
    fake.fail_turn(int((await row(conn, second)).message_id))  # failed before sent
    await w.tick()  # first is still in progress: caps 2/2 with first + filler

    r = await row(conn, second)
    assert (r.status, r.update_of, r.cluster_id) == ("new", None, None)
    assert len(fake.clusters) == 2


# --- restart --------------------------------------------------------------------------------


class Crash(Exception):
    pass


async def test_restart_between_deliver_and_set_message_adopts_the_marker_message(
    conn, fake, make_endpoint, make_worker, monkeypatch
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    second = await rec(conn, ep, ev(action="synchronize"))
    original = events.EventStore.set_message

    async def crash(self, run_id, message_id):
        raise Crash("killed after post")

    monkeypatch.setattr(events.EventStore, "set_message", crash)
    with pytest.raises(Crash):
        await make_worker().tick()
    r = await row(conn, second)
    assert (r.update_of, r.cluster_id, r.message_id) == (first.id, cluster["id"], None)
    assert len(messages_of(fake, cluster["id"])) == 2

    monkeypatch.setattr(events.EventStore, "set_message", original)
    await make_worker().tick()

    msgs = messages_of(fake, cluster["id"])
    assert len(msgs) == 2
    r = await row(conn, second)
    assert r.message_id == str(msgs[1]["id"])
    fake.mark_sent(msgs[1]["id"])
    await make_worker().tick()
    assert (await row(conn, second)).status == "handled"
    assert len(fake.clusters) == 1


async def test_restart_after_mark_update_resumes_the_delivery(
    conn, fake, make_endpoint, make_worker, monkeypatch
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    second = await rec(conn, ep, ev(action="synchronize"))
    original = events.EventStore.set_cluster

    async def crash(self, run_id, cluster_id, *, slug=None):
        raise Crash("killed after mark_update")

    monkeypatch.setattr(events.EventStore, "set_cluster", crash)
    with pytest.raises(Crash):
        await make_worker().tick()
    r = await row(conn, second)
    assert (r.update_of, r.cluster_id, r.message_id) == (first.id, None, None)

    monkeypatch.setattr(events.EventStore, "set_cluster", original)
    await make_worker().tick()
    r = await row(conn, second)
    assert (r.cluster_id, r.cluster_slug) == (cluster["id"], f"wh-{first.id}")
    assert len(messages_of(fake, cluster["id"])) == 2
    assert len(fake.clusters) == 1


async def test_restart_orchestrator_down_at_running_check_retries_next_tick(
    conn, fake, switch, make_endpoint, make_worker, monkeypatch
):
    ep = await make_endpoint(REUSE)
    w = make_worker()
    first, cluster = await start_first(conn, fake, w, ep)
    second = await rec(conn, ep, ev(action="synchronize"))
    original = OrchestratorClient.get_cluster

    async def down(self, cluster_id):
        raise OrchestratorUnavailable("down")

    monkeypatch.setattr(OrchestratorClient, "get_cluster", down)
    await w.tick()
    r = await row(conn, second)
    assert (r.status, r.update_of, r.message_id) == ("in-progress", first.id, None)
    assert len(fake.clusters) == 1

    monkeypatch.setattr(OrchestratorClient, "get_cluster", original)
    await w.tick()
    r = await row(conn, second)
    assert (r.update_of, r.cluster_id) == (first.id, cluster["id"])
    assert r.message_id is not None
    assert len(fake.clusters) == 1
