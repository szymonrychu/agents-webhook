import hashlib
import inspect
import json

import pytest

from agents_webhook.normalize import (
    ENDPOINT_TYPES,
    TypeMismatch,
    Unsupported,
    classify,
    normalize,
)

from . import payloads as p


def _classify(event, bots=()):
    return classify(event, bot_logins=bots)


def test_endpoint_types():
    assert set(ENDPOINT_TYPES) == {
        "grafana-alert",
        "gitlab-issue",
        "gitlab-mr",
        "github-issue",
        "github-pr",
    }


@pytest.mark.parametrize(
    ("name", "payload", "kind", "action"),
    [
        ("pull_request", p.gh_pull_request(), "mr", "opened"),
        ("pull_request_review_comment", p.gh_review_comment(), "comment", "created"),
        ("pull_request_review", p.gh_review(), "comment", "submitted"),
        ("issue_comment", p.gh_issue_comment(on_pr=True), "comment", "created"),
    ],
)
def test_github_pr_events(name, payload, kind, action):
    ev = normalize("github-pr", p.gh_headers(name, "abc"), payload)
    assert (ev.kind, ev.action, ev.source, ev.priority) == (kind, action, "github", 1)
    assert ev.dedup_key == "github/octo/repo/mr/7"
    assert (ev.repo, ev.number, ev.delivery_id, ev.labels) == ("octo/repo", 7, "abc", ["bug", "p1"])
    assert ev.title == "Fix it"
    assert ev.body == "Please fix"


def test_github_pr_comment_fields():
    ev = normalize("github-pr", p.gh_headers("issue_comment"), p.gh_issue_comment(on_pr=True))
    assert (ev.comment_body, ev.author, ev.actor) == ("looks odd", "bob", "bob")
    assert ev.url.endswith("#c1")


def test_github_issue_events():
    ev = normalize("github-issue", p.gh_headers("issues"), p.gh_issues())
    assert (ev.kind, ev.action, ev.number, ev.author, ev.actor) == (
        "issue",
        "opened",
        3,
        "alice",
        "alice",
    )
    assert ev.dedup_key == "github/octo/repo/issue/3"
    ev = normalize("github-issue", p.gh_headers("issue_comment"), p.gh_issue_comment(on_pr=False))
    assert (ev.kind, ev.dedup_key, ev.comment_body) == (
        "comment",
        "github/octo/repo/issue/3",
        "looks odd",
    )


def test_github_mismatch_and_unsupported():
    with pytest.raises(TypeMismatch):
        normalize("github-issue", p.gh_headers("pull_request"), p.gh_pull_request())
    with pytest.raises(TypeMismatch):
        normalize("github-issue", p.gh_headers("issue_comment"), p.gh_issue_comment(on_pr=True))
    with pytest.raises(TypeMismatch):
        normalize("github-pr", p.gh_headers("issues"), p.gh_issues())
    with pytest.raises(TypeMismatch):
        normalize("github-pr", p.gh_headers("issue_comment"), p.gh_issue_comment(on_pr=False))


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("open", "opened"),
        ("reopen", "reopened"),
        ("update", "synchronize"),
        ("close", "closed"),
        ("merge", "merged"),
    ],
)
def test_gitlab_mr_actions(action, expected):
    ev = normalize(
        "gitlab-mr", p.gl_headers("Merge Request Hook", "u9"), p.gl_merge_request(action)
    )
    assert (ev.kind, ev.action, ev.delivery_id) == ("mr", expected, "u9")
    assert ev.dedup_key == "gitlab/grp/sub/proj/mr/12"
    assert (ev.repo, ev.number, ev.labels, ev.author, ev.actor) == (
        "grp/sub/proj",
        12,
        ["backend"],
        "alice",
        "alice",
    )


def test_gitlab_issue_and_notes():
    ev = normalize("gitlab-issue", p.gl_headers("Issue Hook"), p.gl_issue())
    assert (ev.kind, ev.action, ev.dedup_key) == ("issue", "opened", "gitlab/grp/sub/proj/issue/5")
    ev = normalize("gitlab-issue", p.gl_headers("Note Hook"), p.gl_note(on_mr=False))
    assert (ev.kind, ev.comment_body, ev.dedup_key, ev.author) == (
        "comment",
        "see this",
        "gitlab/grp/sub/proj/issue/5",
        "bob",
    )
    ev = normalize("gitlab-mr", p.gl_headers("Note Hook"), p.gl_note(on_mr=True))
    assert (ev.kind, ev.dedup_key, ev.title) == (
        "comment",
        "gitlab/grp/sub/proj/mr/12",
        "Add thing",
    )


def test_gitlab_mismatch_and_unsupported():
    with pytest.raises(TypeMismatch):
        normalize("gitlab-issue", p.gl_headers("Merge Request Hook"), p.gl_merge_request())
    with pytest.raises(TypeMismatch):
        normalize("gitlab-mr", p.gl_headers("Issue Hook"), p.gl_issue())
    with pytest.raises(TypeMismatch):
        normalize("gitlab-mr", p.gl_headers("Note Hook"), p.gl_note(on_mr=False))
    with pytest.raises(TypeMismatch):
        normalize("gitlab-issue", p.gl_headers("Note Hook"), p.gl_note(on_mr=True))
    with pytest.raises(Unsupported):
        normalize("gitlab-mr", p.gl_headers("Note Hook"), p.gl_note_commit())
    with pytest.raises(Unsupported):
        normalize("gitlab-mr", p.gl_headers("Push Hook"), {"object_kind": "push"})


def test_mismatch_across_providers():
    with pytest.raises(TypeMismatch):
        normalize("github-pr", p.gl_headers("Merge Request Hook"), p.gl_merge_request())
    with pytest.raises(TypeMismatch):
        normalize("gitlab-mr", p.gh_headers("pull_request"), p.gh_pull_request())
    with pytest.raises(TypeMismatch):
        normalize("grafana-alert", p.gh_headers("pull_request"), p.gh_pull_request())
    with pytest.raises(TypeMismatch):
        normalize("grafana-alert", p.gl_headers("Issue Hook"), p.gl_issue())


def test_unknown_event_unsupported_mismatch():
    with pytest.raises(Unsupported):
        normalize("github-pr", p.gh_headers("push"), {"ref": "x"})
    with pytest.raises(Unsupported):
        normalize("github-pr", p.gh_headers("ping"), {"zen": "x"})
    with pytest.raises(Unsupported):
        normalize("github-pr", {}, {"anything": 1})
    with pytest.raises(Unsupported):
        normalize("grafana-alert", {}, {"hello": "world"})
    with pytest.raises(Unsupported):
        normalize("github-pr", {}, [1])


def test_headers_case_insensitive():
    ev = normalize(
        "github-pr",
        {"x-github-event": "pull_request", "x-github-delivery": "z"},
        p.gh_pull_request(),
    )
    assert ev.delivery_id == "z"


def test_grafana_event_and_key():
    ev = normalize("grafana-alert", {}, p.grafana_alert())
    labels = {"alertname": "HighCPU", "severity": "critical", "namespace": "prod"}
    digest = hashlib.sha256(
        json.dumps(dict(sorted(labels.items())), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    assert ev.dedup_key == f"grafana/{digest}"
    assert (ev.kind, ev.priority, ev.title, ev.delivery_id, ev.labels) == (
        "alert",
        0,
        "HighCPU",
        None,
        labels,
    )
    assert ev.body == "CPU is high\n\nAbove 90% for 5m"
    assert ev.url == "https://grafana.example/"


def test_grafana_key_ignores_label_order():
    a = normalize(
        "grafana-alert", {}, p.grafana_alert(labels={"alertname": "A", "z": "1", "b": "2"})
    )
    b = normalize(
        "grafana-alert", {}, p.grafana_alert(labels={"b": "2", "alertname": "A", "z": "1"})
    )
    c = normalize(
        "grafana-alert", {}, p.grafana_alert(labels={"alertname": "A", "z": "1", "b": "3"})
    )
    assert a.dedup_key == b.dedup_key != c.dedup_key


def test_grafana_resolved_classified():
    firing = normalize("grafana-alert", {}, p.grafana_alert())
    resolved = normalize("grafana-alert", {}, p.grafana_alert("resolved"))
    assert _classify(firing) is None
    assert _classify(resolved) == "resolved"


def test_classify_self_event():
    ev = normalize("github-pr", p.gh_headers("pull_request"), p.gh_pull_request())
    assert _classify(ev, bots={"alice"}) == "self_event"
    assert _classify(ev, bots={"other"}) is None


def test_classify_has_no_author_allowlist():
    assert "allowed_authors" not in inspect.signature(classify).parameters
    issue = normalize("github-issue", p.gh_headers("issues"), p.gh_issues())
    comment = normalize("github-pr", p.gh_headers("issue_comment"), p.gh_issue_comment(on_pr=True))
    assert _classify(issue) is None
    assert _classify(comment) is None


@pytest.mark.parametrize("action", ["closed", "labeled", "unlabeled", "converted_to_draft"])
def test_classify_action_ignored_github(action):
    ev = normalize("github-pr", p.gh_headers("pull_request"), p.gh_pull_request(action))
    assert _classify(ev) == "action_ignored"


def test_classify_handled_pr_actions():
    for action in ("opened", "reopened", "synchronize", "ready_for_review"):
        ev = normalize("github-pr", p.gh_headers("pull_request"), p.gh_pull_request(action))
        assert _classify(ev) is None, action


def test_classify_draft_pr_and_review_dismissal():
    draft = normalize(
        "github-pr", p.gh_headers("pull_request"), p.gh_pull_request("synchronize", draft=True)
    )
    assert _classify(draft) == "action_ignored"
    ready = normalize(
        "github-pr", p.gh_headers("pull_request"), p.gh_pull_request("ready_for_review")
    )
    assert _classify(ready) is None
    dismissed = normalize(
        "github-pr", p.gh_headers("pull_request_review"), p.gh_review("dismissed")
    )
    assert _classify(dismissed) == "action_ignored"
    edited = normalize(
        "github-pr", p.gh_headers("issue_comment"), p.gh_issue_comment(on_pr=True, action="deleted")
    )
    assert _classify(edited) == "action_ignored"


def test_classify_gitlab_actions():
    for action in ("close", "merge"):
        ev = normalize("gitlab-mr", p.gl_headers("Merge Request Hook"), p.gl_merge_request(action))
        assert _classify(ev) == "action_ignored"
    ev = normalize("gitlab-mr", p.gl_headers("Merge Request Hook"), p.gl_merge_request(draft=True))
    assert _classify(ev) == "action_ignored"
    ev = normalize(
        "gitlab-mr",
        p.gl_headers("Merge Request Hook"),
        p.gl_merge_request("update", {"labels": {}}),
    )
    assert _classify(ev) == "action_ignored"
    ev = normalize(
        "gitlab-mr", p.gl_headers("Merge Request Hook"), p.gl_merge_request("update", {"title": {}})
    )
    assert _classify(ev) == "action_ignored"
    ev = normalize(
        "gitlab-mr",
        p.gl_headers("Merge Request Hook"),
        p.gl_merge_request("update", {"last_commit": {}}),
    )
    assert _classify(ev) is None


def test_classify_title_only_edit_ignored_body_edit_accepted():
    title = normalize(
        "github-issue",
        p.gh_headers("issues"),
        p.gh_issues("edited", changes={"title": {"from": "x"}}),
    )
    body = normalize(
        "github-issue",
        p.gh_headers("issues"),
        p.gh_issues("edited", changes={"body": {"from": "x"}}),
    )
    both = normalize(
        "github-issue",
        p.gh_headers("issues"),
        p.gh_issues("edited", changes={"body": {}, "title": {}}),
    )
    assert _classify(title) == "action_ignored"
    assert _classify(body) is None
    assert _classify(both) is None
    gl_title = normalize(
        "gitlab-issue", p.gl_headers("Issue Hook"), p.gl_issue("update", {"title": {}})
    )
    gl_body = normalize(
        "gitlab-issue", p.gl_headers("Issue Hook"), p.gl_issue("update", {"description": {}})
    )
    assert _classify(gl_title) == "action_ignored"
    assert _classify(gl_body) is None


def test_classify_github_pr_edit_ignored():
    ev = normalize(
        "github-pr",
        p.gh_headers("pull_request"),
        p.gh_pull_request("edited", changes={"body": {"from": "x"}}),
    )
    assert _classify(ev) == "action_ignored"


@pytest.mark.parametrize("key", ["draft", "work_in_progress"])
def test_gitlab_draft_to_ready_is_ready_for_review(key):
    changes = {
        key: {"previous": True, "current": False},
        "title": {"previous": "Draft: Add thing", "current": "Add thing"},
    }
    ev = normalize(
        "gitlab-mr", p.gl_headers("Merge Request Hook"), p.gl_merge_request("update", changes)
    )
    assert ev.action == "ready_for_review"
    assert _classify(ev) is None


def test_gitlab_ready_to_draft_is_ignored():
    changes = {
        "draft": {"previous": False, "current": True},
        "title": {"previous": "x", "current": "Draft: x"},
    }
    ev = normalize(
        "gitlab-mr",
        p.gl_headers("Merge Request Hook"),
        p.gl_merge_request("update", changes, draft=True),
    )
    assert _classify(ev) == "action_ignored"


def _branches(ev):
    return ev.source_branch, ev.target_branch


def test_github_pr_branches():
    ev = normalize("github-pr", p.gh_headers("pull_request"), p.gh_pull_request())
    assert _branches(ev) == ("feature/x", "main")


def test_github_branches_per_event():
    for name, payload in [
        ("pull_request_review", p.gh_review()),
        ("pull_request_review_comment", p.gh_review_comment()),
    ]:
        ev = normalize("github-pr", p.gh_headers(name), payload)
        assert _branches(ev) == ("feature/x", "main")
    ev = normalize("github-pr", p.gh_headers("issue_comment"), p.gh_issue_comment(on_pr=True))
    assert _branches(ev) == (None, None)
    ev = normalize("github-issue", p.gh_headers("issues"), p.gh_issues())
    assert _branches(ev) == (None, None)


def test_gitlab_mr_branches():
    ev = normalize("gitlab-mr", p.gl_headers("Merge Request Hook"), p.gl_merge_request())
    assert _branches(ev) == ("feature/x", "main")
    ev = normalize("gitlab-mr", p.gl_headers("Note Hook"), p.gl_note(on_mr=True))
    assert _branches(ev) == ("feature/x", "main")
    ev = normalize("gitlab-issue", p.gl_headers("Issue Hook"), p.gl_issue())
    assert _branches(ev) == (None, None)
    ev = normalize("gitlab-issue", p.gl_headers("Note Hook"), p.gl_note(on_mr=False))
    assert _branches(ev) == (None, None)


def test_branches_absent_are_none():
    ev = normalize("grafana-alert", {}, p.grafana_alert())
    assert _branches(ev) == (None, None)
    gh = p.gh_pull_request()
    gh["pull_request"]["head"] = {"ref": 5}
    gh["pull_request"]["base"] = "main"
    ev = normalize("github-pr", p.gh_headers("pull_request"), gh)
    assert _branches(ev) == (None, None)
    del gh["pull_request"]["head"], gh["pull_request"]["base"]
    assert _branches(normalize("github-pr", p.gh_headers("pull_request"), gh)) == (None, None)
    gl = p.gl_merge_request()
    gl["object_attributes"]["source_branch"] = None
    del gl["object_attributes"]["target_branch"]
    assert _branches(normalize("gitlab-mr", p.gl_headers("Merge Request Hook"), gl)) == (
        None,
        None,
    )
    note = p.gl_note(on_mr=True)
    note["merge_request"]["source_branch"] = ""
    note["merge_request"]["target_branch"] = ["x"]
    assert _branches(normalize("gitlab-mr", p.gl_headers("Note Hook"), note)) == (None, None)
