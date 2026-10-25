"""Headless parity (design D14): every capability through HTTP to the real ingest and api
processes; the orchestrator is the FakeOrchestrator, the only thing the tests reach around HTTP
to drive (turn completion, discarded clusters)."""

import copy
import time
import uuid
from typing import Any

import pytest

from .. import payloads as p
from .conftest import Stack, basic, wait_for

MIB = 1024 * 1024
PR = (p.gh_headers("pull_request"), p.gh_pull_request())
# One event of each endpoint type, and a valid event that belongs to another type.
OWN: dict[str, tuple[dict[str, str], Any]] = {
    "github-pr": PR,
    "github-issue": (p.gh_headers("issues"), p.gh_issues()),
    "gitlab-mr": (p.gl_headers("Merge Request Hook"), p.gl_merge_request()),
    "gitlab-issue": (p.gl_headers("Issue Hook"), p.gl_issue()),
    "grafana-alert": ({}, p.grafana_alert()),
}
OTHER: dict[str, tuple[dict[str, str], Any]] = {
    "github-pr": OWN["github-issue"],
    "github-issue": OWN["github-pr"],
    "gitlab-mr": OWN["gitlab-issue"],
    "gitlab-issue": OWN["gitlab-mr"],
    "grafana-alert": OWN["github-pr"],
}


def delivery(headers: dict[str, str]) -> dict[str, str]:
    """`headers` with a fresh delivery id, so two sends are not redeliveries."""
    fresh = uuid.uuid4().hex
    return {
        k: (fresh if k in ("X-GitHub-Delivery", "X-Gitlab-Event-UUID") else v)
        for k, v in headers.items()
    }


def accept(stack: Stack, endpoint: dict, headers: dict, payload: Any, **kw) -> dict:
    r = stack.send(endpoint, delivery(headers), payload, **kw)
    assert r.status_code == 202, r.text
    return r.json()


def finish(stack: Stack, id: int) -> dict:
    """Wait for the row to run, complete its turn on the fake, wait for `handled`."""
    row = stack.wait_status(id, "in-progress")
    stack.fake.complete_turn(int(row["message_id"]), "done")
    return stack.wait_status(id, "handled")


def edited(payload: dict, **changes: Any) -> dict:
    out = copy.deepcopy(payload)
    out.update(changes)
    return out


# --- the happy path -------------------------------------------------------------------------


def test_create_endpoint_receive_event_and_complete(stack: Stack):
    created = stack.create_endpoint("github-pr")
    assert created["url"].startswith(f"{stack.ingest}/webhook/")
    assert created["url"].endswith(created["suffix"])
    assert created["username"] and created["password"]
    listed = next(
        e for e in stack.http.get(f"{stack.api}/endpoints").json() if e["id"] == created["id"]
    )
    assert "password" not in listed and listed["url"] == created["url"]

    accepted = accept(stack, created, *PR)
    assert accepted["status"] == "new"
    row = stack.wait_status(accepted["id"], "in-progress")
    assert row["cluster_slug"] == f"wh-{accepted['id']}"
    assert row["endpoint_suffix"] == created["suffix"]
    stack.cluster(row["cluster_slug"])

    stack.fake.complete_turn(int(row["message_id"]), "done")
    assert stack.wait_status(accepted["id"], "handled")["finished_at"] is not None


@pytest.mark.parametrize("type_", list(OWN))
def test_each_type_with_supplied_credentials(stack: Stack, type_: str):
    suffix = f"{type_}-{uuid.uuid4().hex[:8]}"
    ep = stack.create_endpoint(type_, suffix=suffix, username="hook", password="s" * 24)
    assert (ep["suffix"], ep["username"], ep["password"]) == (suffix, "hook", "s" * 24)

    finish(stack, accept(stack, ep, *OWN[type_])["id"])

    mismatch = accept(stack, ep, *OTHER[type_])
    assert (mismatch["status"], mismatch["reason"]) == ("ignored", "type_mismatch")


def test_grafana_resolved_is_ignored(stack: Stack):
    ep = stack.create_endpoint("grafana-alert")
    resolved = accept(stack, ep, {}, p.grafana_alert(status="resolved"))
    assert (resolved["status"], resolved["reason"]) == ("ignored", "resolved")
    assert stack.event(resolved["id"])["reason"] == "resolved"


# --- endpoint filters ------------------------------------------------------------------------


def titled(title: str) -> dict:
    payload = p.gh_pull_request()
    payload["pull_request"]["title"] = title
    return payload


def test_deny_filter_ignores_and_others_run(stack: Stack):
    ep = stack.create_endpoint(
        "github-pr", filters={"deny": [{"field": "title", "values": ["WIP: not ready"]}]}
    )
    denied = accept(stack, ep, PR[0], titled("WIP: not ready"))
    assert (denied["status"], denied["reason"]) == ("ignored", "filter_denied: title")
    assert stack.event(denied["id"])["reason"] == "filter_denied: title"

    ran = finish(stack, accept(stack, ep, PR[0], titled("Ship it"))["id"])
    assert ran["status"] == "handled"
    assert len([c for c in stack.fake.clusters.values() if c["slug"] == ran["cluster_slug"]]) == 1


def test_allow_filter_ignores_stranger(stack: Stack):
    ep = stack.create_endpoint(
        "github-pr", filters={"allow": [{"field": "author", "values": ["alice"]}]}
    )
    stranger = p.gh_pull_request()
    stranger["pull_request"]["user"] = {"login": "mallory"}
    out = accept(stack, ep, PR[0], stranger)
    assert (out["status"], out["reason"]) == ("ignored", "filter_not_allowed: author")
    assert stack.event(out["id"])["reason"] == "filter_not_allowed: author"
    finish(stack, accept(stack, ep, *PR)["id"])


def test_put_filters_changes_the_outcome(stack: Stack):
    ep = stack.create_endpoint(
        "github-pr", filters={"deny": [{"field": "title", "values": ["Fix it"]}]}
    )
    first = accept(stack, ep, *PR)
    assert (first["status"], first["reason"]) == ("ignored", "filter_denied: title")

    r = stack.http.put(f"{stack.api}/endpoints/{ep['id']}/filters", json={"allow": [], "deny": []})
    assert r.status_code == 200, r.text
    assert r.json()["filters"] == {"allow": [], "deny": []}
    finish(stack, accept(stack, ep, *PR)["id"])


def test_grafana_label_deny(stack: Stack):
    ep = stack.create_endpoint(
        "grafana-alert", filters={"deny": [{"field": "labels", "values": ["env=staging"]}]}
    )
    staging = accept(
        stack, ep, {}, p.grafana_alert(labels={"alertname": "HighCPU", "env": "staging"})
    )
    assert (staging["status"], staging["reason"]) == ("ignored", "filter_denied: labels")
    prod = accept(
        stack, ep, {}, p.grafana_alert(labels={"alertname": "HighCPU", "env": "production"})
    )
    assert prod["status"] == "new"
    finish(stack, prod["id"])


def test_preview_reports_filter_outcome(stack: Stack):
    rules = {"allow": [{"field": "author", "values": ["alice"]}], "deny": []}
    url = f"{stack.api}/templates/preview"
    ok = stack.http.post(url, json={"type": "github-pr", "example": PR[1], "filters": rules})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"prompt": None, "filters": {"passed": True, "reason": None}}

    denied = {"allow": [], "deny": [{"field": "title", "values": ["Fix it"]}]}
    no = stack.http.post(url, json={"type": "github-pr", "example": PR[1], "filters": denied})
    assert no.json()["filters"] == {"passed": False, "reason": "filter_denied: title"}

    miss = {"allow": [{"field": "author", "values": ["bob"]}], "deny": []}
    out = stack.http.post(url, json={"type": "github-pr", "example": PR[1], "filters": miss})
    assert out.json()["filters"] == {"passed": False, "reason": "filter_not_allowed: author"}


# --- templates ------------------------------------------------------------------------------

TEMPLATE = "Service {{ (payload.pull_request.body | from_json).service }}: {{ title }}"


def json_body_pr() -> dict:
    payload = copy.deepcopy(PR[1])
    payload["pull_request"]["body"] = '{"service": "billing"}'
    return payload


def test_template_preview_with_from_json(stack: Stack):
    r = stack.http.post(
        f"{stack.api}/templates/preview",
        json={"template": TEMPLATE, "type": "github-pr", "example": json_body_pr()},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"prompt": "Service billing: Fix it", "filters": None}

    bad = stack.http.post(
        f"{stack.api}/templates/preview",
        json={"template": "{{ title | nope }}", "type": "github-pr", "example": PR[1]},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_request"
    broken = stack.http.post(
        f"{stack.api}/templates/preview",
        json={"template": TEMPLATE, "type": "github-pr", "example": PR[1]},
    )
    assert broken.status_code == 422  # the example body is not JSON: from_json refuses it


def test_endpoint_template_prompt_is_the_first_cluster_message(stack: Stack):
    bad = stack.http.post(
        f"{stack.api}/endpoints",
        json={"type": "github-pr", "preset_id": stack.preset_id, "prompt_template": "{% nope %}"},
    )
    assert bad.status_code == 422

    ep = stack.create_endpoint("github-pr", prompt_template=TEMPLATE)
    assert ep["prompt_template"] == TEMPLATE
    row = stack.wait_status(
        accept(stack, ep, p.gh_headers("pull_request"), json_body_pr())["id"], "in-progress"
    )
    messages = stack.messages(stack.cluster(row["cluster_slug"])["id"])
    assert len(messages) == 1
    assert messages[0]["text"].startswith("Service billing: Fix it")
    assert str(row["id"]) in messages[0]["text"]  # the run marker follows the prompt


# --- dedup ----------------------------------------------------------------------------------


def test_duplicate_while_new_latest_wins_and_redelivery(make_stack):
    stack = make_stack(worker=False)
    ep = stack.create_endpoint("github-pr")
    first = stack.send(ep, p.gh_headers("pull_request", "d-1"), PR[1]).json()
    assert first["status"] == "new"
    newer = p.gh_pull_request("synchronize")
    second = stack.send(ep, p.gh_headers("pull_request", "d-2"), newer).json()
    assert (second["status"], second["reason"]) == ("ignored", "duplicate")

    rows = stack.events(endpoint_id=ep["id"])
    queued = [r for r in rows if r["status"] == "new"]
    assert len(queued) == 1 and queued[0]["action"] == "synchronize"
    dupes = [r for r in rows if r["reason"] == "duplicate"]
    assert dupes and all(r["duplicate_of"] is not None for r in dupes)

    again = stack.send(ep, p.gh_headers("pull_request", "d-2"), newer).json()
    assert (again["status"], again["reason"]) == ("ignored", "duplicate")
    assert [r["status"] for r in stack.events(endpoint_id=ep["id"]) if r["status"] == "new"] == [
        "new"
    ]


# --- agent reuse ----------------------------------------------------------------------------


def comment(stack: Stack, ep: dict, who: str = "bob") -> int:
    return accept(
        stack, ep, p.gh_headers("issue_comment"), p.gh_issue_comment(True, commenter=who)
    )["id"]


def test_reuse_on_delivers_to_the_running_cluster_then_starts_a_new_one(stack: Stack):
    ep = stack.create_endpoint("github-pr", preset_id=stack.preset(reuse_running_agent=True))
    first = stack.wait_status(accept(stack, ep, *PR)["id"], "in-progress")
    cluster = stack.cluster(first["cluster_slug"])

    second = stack.wait_status(comment(stack, ep), "in-progress")
    assert second["update_of"] == first["id"]
    assert second["cluster_id"] == cluster["id"]
    assert len(stack.messages(cluster["id"])) == 2
    assert len([c for c in stack.fake.clusters.values() if c["preset_id"] == ep["preset_id"]]) == 1
    stack.fake.mark_sent(int(second["message_id"]))
    stack.wait_status(second["id"], "handled")
    stack.fake.complete_turn(int(first["message_id"]), "done")
    stack.wait_status(first["id"], "handled")

    stack.fake.discard_cluster(cluster["id"])
    third = stack.wait_status(comment(stack, ep), "in-progress")
    assert third["cluster_slug"] == f"wh-{third['id']}"
    assert third["cluster_id"] != cluster["id"]
    assert len(stack.messages(third["cluster_id"])) == 1


def test_reuse_off_starts_its_own_cluster(stack: Stack):
    ep = stack.create_endpoint("github-pr", preset_id=stack.preset(reuse_running_agent=False))
    first = finish(stack, accept(stack, ep, *PR)["id"])
    second = stack.wait_status(comment(stack, ep), "in-progress")
    assert second["update_of"] is None
    assert second["cluster_slug"] == f"wh-{second['id']}"
    assert second["cluster_id"] != first["cluster_id"]


# --- ignore reasons -------------------------------------------------------------------------


def test_ignore_reasons_are_listed(stack: Stack, make_stack):
    ep = stack.create_endpoint("github-pr")
    iss = stack.create_endpoint(
        "github-issue", filters={"allow": [{"field": "author", "values": ["alice", "bob"]}]}
    )
    self_event = accept(stack, ep, PR[0], p.gh_pull_request(sender={"login": "botty"}))
    action = accept(stack, ep, PR[0], p.gh_pull_request("closed"))
    stranger = p.gh_issues()
    stranger["issue"]["user"] = {"login": "mallory"}
    author = accept(stack, iss, p.gh_headers("issues"), stranger)

    expected = {
        self_event["id"]: "self_event",
        action["id"]: "action_ignored",
        author["id"]: "filter_not_allowed: author",
    }
    for id, reason in expected.items():
        assert stack.event(id)["status"] == "ignored" and stack.event(id)["reason"] == reason
    listed = {r["id"]: r["reason"] for r in stack.events(status="ignored")}
    assert all(listed[id] == reason for id, reason in expected.items())

    full = make_stack(worker=False, max_queue="1")
    a, b = full.create_endpoint("github-pr"), full.create_endpoint("github-pr")
    assert accept(full, a, *PR)["status"] == "new"
    over = accept(full, b, *PR)
    assert (over["status"], over["reason"]) == ("ignored", "queue_full")
    assert full.event(over["id"])["reason"] == "queue_full"


# --- caps and failure -----------------------------------------------------------------------


def test_in_progress_cap_holds_the_second_event_until_the_first_is_handled(make_stack):
    stack = make_stack(max_in_progress="1")
    a, b = stack.create_endpoint("github-pr"), stack.create_endpoint("github-pr")
    first = stack.wait_status(accept(stack, a, *PR)["id"], "in-progress")
    second = accept(stack, b, *PR)["id"]
    time.sleep(1.5)  # several worker ticks: the cap keeps it queued
    assert stack.event(second)["status"] == "new"
    assert len([c for c in stack.fake.clusters.values()]) == 1

    stack.fake.complete_turn(int(first["message_id"]), "done")
    stack.wait_status(first["id"], "handled")
    stack.wait_status(second, "in-progress")


def test_failed_turn_is_failed_turn_failed(stack: Stack):
    ep = stack.create_endpoint("github-pr")
    row = stack.wait_status(accept(stack, ep, *PR)["id"], "in-progress")
    message = stack.fake.messages[int(row["message_id"])]
    message["status"] = "sent"
    message["turn"] = {
        "status": "failed",
        "started_at": None,
        "completed_at": None,
        "final_text": None,
    }
    failed = stack.wait_status(row["id"], "failed")
    assert failed["reason"] == "turn_failed"


# --- auth and endpoint operations -----------------------------------------------------------


def test_wrong_password_and_unknown_suffix_look_the_same(stack: Stack):
    ep = stack.create_endpoint("github-pr")
    wrong = stack.send(ep, *PR[:1], PR[1], password="not-the-password")
    ghost = stack.http.post(
        f"{stack.ingest}/webhook/no-such-{uuid.uuid4().hex}",
        headers={**basic("x", "y"), **PR[0]},
        json=PR[1],
    )
    assert wrong.status_code == ghost.status_code == 401
    assert wrong.json() == ghost.json()
    assert wrong.headers["www-authenticate"] == ghost.headers["www-authenticate"]


def test_eleventh_failed_authentication_is_429(make_stack):
    stack = make_stack(worker=False, auth_failures_per_minute="10")
    ep = stack.create_endpoint("github-pr")
    codes = [stack.send(ep, *PR[:1], PR[1], password="wrong").status_code for _ in range(10)]
    assert codes == [401] * 10
    blocked = stack.send(ep, *PR[:1], PR[1], password="wrong")
    assert blocked.status_code == 429 and "retry-after" in blocked.headers


def test_rotate_replaces_the_password(stack: Stack):
    ep = stack.create_endpoint("github-pr")
    r = stack.http.post(f"{stack.api}/endpoints/{ep['id']}/rotate")
    assert r.status_code == 200, r.text
    rotated = r.json()
    assert rotated["password"] != ep["password"]
    assert stack.send(ep, *PR[:1], PR[1]).status_code == 401
    assert stack.send({**ep, **rotated}, *PR[:1], PR[1]).status_code == 202


def test_oversized_body_is_413(stack: Stack):
    ep = stack.create_endpoint("github-pr")
    r = stack.http.post(
        ep["url"],
        headers={
            **basic(ep["username"], ep["password"]),
            **PR[0],
            "Content-Type": "application/json",
        },
        content=b" " * (6 * MIB),
    )
    assert r.status_code == 413


def test_delete_endpoint_stops_deliveries_and_removes_queued_events(make_stack):
    stack = make_stack(max_in_progress="1")
    busy, ep = stack.create_endpoint("github-pr"), stack.create_endpoint("github-pr")
    first = stack.wait_status(accept(stack, busy, *PR)["id"], "in-progress")
    queued = accept(stack, ep, *PR)["id"]
    assert stack.event(queued)["status"] == "new"

    assert stack.http.delete(f"{stack.api}/endpoints/{ep['id']}").status_code == 204
    assert stack.send(ep, *PR[:1], PR[1]).status_code == 401
    assert stack.http.delete(f"{stack.api}/endpoints/{ep['id']}").status_code == 404

    stack.fake.complete_turn(int(first["message_id"]), "done")  # frees the worker to claim
    removed = wait_for(
        lambda: (r := stack.event(queued))["status"] == "ignored" and r, "endpoint_removed"
    )
    assert removed["reason"] == "endpoint_removed"
