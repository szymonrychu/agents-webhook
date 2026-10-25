"""Minimal realistic provider payloads and headers (GitHub, GitLab, Grafana docs shapes)."""

from typing import Any


def gh_headers(event: str, delivery: str = "d-1") -> dict[str, str]:
    return {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}


def _gh_repo() -> dict[str, Any]:
    return {"full_name": "octo/repo"}


def _gh_item(number: int = 7, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Fix it",
        "body": "Please fix",
        "html_url": f"https://github.com/octo/repo/pull/{number}",
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}, {"name": "p1"}],
        **extra,
    }


def _gh_pr_item(**extra: Any) -> dict[str, Any]:
    return _gh_item(
        head={"ref": "feature/x", "sha": "abc"}, base={"ref": "main", "sha": "def"}, **extra
    )


def gh_pull_request(action: str = "opened", draft: bool = False, **top: Any) -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": _gh_pr_item(draft=draft),
        "repository": _gh_repo(),
        "sender": {"login": "alice"},
        **top,
    }


def gh_issues(action: str = "opened", **top: Any) -> dict[str, Any]:
    return {
        "action": action,
        "issue": _gh_item(3, html_url="https://github.com/octo/repo/issues/3"),
        "repository": _gh_repo(),
        "sender": {"login": "alice"},
        **top,
    }


def gh_issue_comment(
    on_pr: bool, action: str = "created", commenter: str = "bob"
) -> dict[str, Any]:
    issue = _gh_item(7 if on_pr else 3)
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/octo/repo/pulls/7"}
    return {
        "action": action,
        "issue": issue,
        "comment": {
            "body": "looks odd",
            "html_url": "https://github.com/octo/repo/issues/3#c1",
            "user": {"login": commenter},
        },
        "repository": _gh_repo(),
        "sender": {"login": commenter},
    }


def gh_review_comment(action: str = "created") -> dict[str, Any]:
    return {
        "action": action,
        "comment": {
            "body": "nit here",
            "html_url": "https://github.com/octo/repo/pull/7#r1",
            "user": {"login": "bob"},
        },
        "pull_request": _gh_pr_item(),
        "repository": _gh_repo(),
        "sender": {"login": "bob"},
    }


def gh_review(action: str = "submitted") -> dict[str, Any]:
    return {
        "action": action,
        "review": {
            "body": "needs work",
            "html_url": "https://github.com/octo/repo/pull/7#rv1",
            "user": {"login": "bob"},
        },
        "pull_request": _gh_pr_item(),
        "repository": _gh_repo(),
        "sender": {"login": "bob"},
    }


def gl_headers(event: str, uuid: str = "u-1") -> dict[str, str]:
    return {"X-Gitlab-Event": event, "X-Gitlab-Event-UUID": uuid}


def gl_merge_request(
    action: str = "open", changes: dict[str, Any] | None = None, draft: bool = False
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object_kind": "merge_request",
        "user": {"username": "alice"},
        "project": {"path_with_namespace": "grp/sub/proj"},
        "object_attributes": {
            "iid": 12,
            "title": "Add thing",
            "description": "Adds thing",
            "action": action,
            "draft": draft,
            "source_branch": "feature/x",
            "target_branch": "main",
            "url": "https://gitlab.com/grp/sub/proj/-/merge_requests/12",
        },
        "labels": [{"title": "backend"}],
    }
    if changes is not None:
        body["changes"] = changes
    return body


def gl_issue(action: str = "open", changes: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object_kind": "issue",
        "user": {"username": "alice"},
        "project": {"path_with_namespace": "grp/sub/proj"},
        "object_attributes": {
            "iid": 5,
            "title": "Broken",
            "description": "It is broken",
            "action": action,
            "url": "https://gitlab.com/grp/sub/proj/-/issues/5",
        },
        "labels": [{"title": "bug"}],
    }
    if changes is not None:
        body["changes"] = changes
    return body


def gl_note(on_mr: bool, user: str = "bob") -> dict[str, Any]:
    body: dict[str, Any] = {
        "object_kind": "note",
        "user": {"username": user},
        "project": {"path_with_namespace": "grp/sub/proj"},
        "object_attributes": {
            "note": "see this",
            "noteable_type": "MergeRequest" if on_mr else "Issue",
            "url": "https://gitlab.com/grp/sub/proj/-/merge_requests/12#note_1",
        },
    }
    if on_mr:
        body["merge_request"] = {
            "iid": 12,
            "title": "Add thing",
            "description": "Adds thing",
            "source_branch": "feature/x",
            "target_branch": "main",
        }
    else:
        body["issue"] = {"iid": 5, "title": "Broken", "description": "It is broken"}
    return body


def gl_note_commit() -> dict[str, Any]:
    body = gl_note(True)
    body["object_attributes"]["noteable_type"] = "Commit"
    return body


def grafana_alert(status: str = "firing", labels: dict[str, str] | None = None) -> dict[str, Any]:
    common = (
        labels
        if labels is not None
        else {"alertname": "HighCPU", "severity": "critical", "namespace": "prod"}
    )
    return {
        "receiver": "tatara",
        "status": status,
        "alerts": [
            {"status": status, "labels": common, "generatorURL": "https://grafana/alerting/1"}
        ],
        "commonLabels": common,
        "commonAnnotations": {"summary": "CPU is high", "description": "Above 90% for 5m"},
        "externalURL": "https://grafana.example/",
    }
