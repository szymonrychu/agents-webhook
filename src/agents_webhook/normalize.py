"""Provider payloads to one event shape per endpoint type, plus ignore classification (no I/O).

An endpoint has exactly one type; `normalize` parses a payload for that type, raises
`TypeMismatch` when the payload is a valid provider event of another type and `Unsupported`
when it is no supported event at all. `classify` returns the ignore reason or None.
"""

import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

ENDPOINT_TYPES = ("grafana-alert", "gitlab-issue", "gitlab-mr", "github-issue", "github-pr")

# Actions that start or continue work; anything else is `action_ignored`.
_HANDLED_ACTIONS = {
    "mr": {"opened", "reopened", "synchronize", "ready_for_review"},
    "issue": {"opened", "reopened", "edited"},
    "comment": {"created", "submitted"},
}
_GITLAB_ACTIONS = {"open": "opened", "reopen": "reopened", "close": "closed", "merge": "merged"}


class TypeMismatch(Exception):
    """A valid provider event that belongs to another endpoint type."""


class Unsupported(Exception):
    """Not a supported provider event."""


@dataclass(frozen=True)
class NormalizedEvent:
    type: str
    source: str
    kind: str
    action: str
    repo: str | None
    number: int | None
    title: str
    body: str
    comment_body: str | None
    url: str | None
    author: str | None
    actor: str | None
    labels: dict[str, str] | list[str]
    delivery_id: str | None
    dedup_key: str
    priority: int
    draft: bool = False
    changed: tuple[str, ...] = ()
    source_branch: str | None = None
    target_branch: str | None = None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _login(user: Any) -> str | None:
    return user.get("login") if isinstance(user, dict) else None


def _ref(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _ref_of(parent: Any, key: str) -> str | None:
    return (
        _ref(parent.get(key, {}).get("ref"))
        if isinstance(parent, dict) and isinstance(parent.get(key), dict)
        else None
    )


def _event(
    endpoint_type: str, source: str, kind: str, key_kind: str, **fields: Any
) -> NormalizedEvent:
    repo, number = fields["repo"], fields["number"]
    return NormalizedEvent(
        type=endpoint_type,
        source=source,
        kind=kind,
        dedup_key=f"{source}/{repo}/{key_kind}/{number}",
        priority=1,
        **fields,
    )


def _github(
    endpoint_type: str, headers: Mapping[str, str], payload: dict[str, Any]
) -> NormalizedEvent:
    want_pr = endpoint_type == "github-pr"
    name = _header(headers, "X-GitHub-Event")
    if name not in {
        "pull_request",
        "pull_request_review_comment",
        "pull_request_review",
        "issues",
        "issue_comment",
    }:
        raise Unsupported(f"github event {name!r}")
    comment_body: str | None = None
    if name == "issues":
        item, kind, is_pr = payload.get("issue"), "issue", False
    elif name == "issue_comment":
        item = payload.get("issue")
        kind, is_pr = "comment", bool(isinstance(item, dict) and item.get("pull_request"))
        comment_body = (payload.get("comment") or {}).get("body")
        author, url = (
            _login((payload.get("comment") or {}).get("user")),
            (payload.get("comment") or {}).get("html_url"),
        )
    elif name == "pull_request":
        item, kind, is_pr = payload.get("pull_request"), "mr", True
    else:
        item, kind, is_pr = payload.get("pull_request"), "comment", True
        sub = payload.get("review") if name == "pull_request_review" else payload.get("comment")
        sub = sub or {}
        comment_body = sub.get("body")
        author, url = _login(sub.get("user")), sub.get("html_url")
    if not isinstance(item, dict) or not isinstance(payload.get("repository"), dict):
        raise Unsupported(f"github {name} payload without item or repository")
    if is_pr != want_pr:
        raise TypeMismatch(f"github {name} belongs to {'github-pr' if is_pr else 'github-issue'}")
    if kind != "comment":
        author, url = _login(item.get("user")), item.get("html_url")
    changes = payload.get("changes")
    return _event(
        endpoint_type,
        "github",
        kind,
        "mr" if is_pr else "issue",
        action=str(payload.get("action", "")),
        repo=payload["repository"].get("full_name"),
        number=item.get("number"),
        title=item.get("title") or "",
        body=item.get("body") or "",
        comment_body=comment_body,
        url=url,
        author=author,
        actor=_login(payload.get("sender")),
        labels=[lb.get("name", "") for lb in item.get("labels") or []],
        delivery_id=_header(headers, "X-GitHub-Delivery"),
        draft=bool(item.get("draft")),
        changed=tuple(sorted(changes)) if isinstance(changes, dict) else (),
        source_branch=_ref_of(item, "head") if name != "issue_comment" else None,
        target_branch=_ref_of(item, "base") if name != "issue_comment" else None,
    )


def _became_ready(change: Any) -> bool:
    return isinstance(change, dict) and bool(change.get("previous")) and not change.get("current")


def _gitlab(
    endpoint_type: str, headers: Mapping[str, str], payload: dict[str, Any]
) -> NormalizedEvent:
    want_mr = endpoint_type == "gitlab-mr"
    name = _header(headers, "X-Gitlab-Event")
    attrs = payload.get("object_attributes")
    project = payload.get("project")
    if name not in {"Merge Request Hook", "Issue Hook", "Note Hook"} or not isinstance(attrs, dict):
        raise Unsupported(f"gitlab event {name!r}")
    if not isinstance(project, dict):
        raise Unsupported(f"gitlab {name} payload without project")
    comment_body: str | None = None
    if name == "Note Hook":
        noteable = attrs.get("noteable_type")
        if noteable not in {"MergeRequest", "Issue"}:
            raise Unsupported(f"gitlab note on {noteable!r}")
        is_mr, kind = noteable == "MergeRequest", "comment"
        item = payload.get("merge_request" if is_mr else "issue") or {}
        comment_body, action = attrs.get("note"), "created"
        author, url = (payload.get("user") or {}).get("username"), attrs.get("url")
        title, body, labels = item.get("title"), item.get("description"), item.get("labels")
    else:
        is_mr, kind = (
            name == "Merge Request Hook",
            "mr" if name == "Merge Request Hook" else "issue",
        )
        item, action, url = attrs, str(attrs.get("action", "")), attrs.get("url")
        author = None
        title, body, labels = attrs.get("title"), attrs.get("description"), payload.get("labels")
    if is_mr != want_mr:
        raise TypeMismatch(f"gitlab {name} belongs to {'gitlab-mr' if is_mr else 'gitlab-issue'}")
    changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
    changed = tuple(sorted(changes))
    if kind != "comment":
        action = _GITLAB_ACTIONS.get(action, action)
        if action == "update":
            if is_mr and any(_became_ready(changes.get(k)) for k in ("draft", "work_in_progress")):
                action = "ready_for_review"
            elif changed == ("title",):
                action = "edited"
            elif changed == ("labels",):
                action = "labeled"
            else:
                action = "synchronize" if is_mr else "edited"
        # the hook carries only an author id; the hook user is the best available author
        author = (payload.get("user") or {}).get("username")
    return _event(
        endpoint_type,
        "gitlab",
        kind,
        "mr" if is_mr else "issue",
        action=action,
        repo=project.get("path_with_namespace"),
        number=item.get("iid"),
        title=title or "",
        body=body or "",
        comment_body=comment_body,
        url=url,
        author=author,
        actor=(payload.get("user") or {}).get("username"),
        labels=[lb.get("title", "") for lb in labels or [] if isinstance(lb, dict)],
        delivery_id=_header(headers, "X-Gitlab-Event-UUID"),
        draft=bool(item.get("draft") or item.get("work_in_progress")),
        changed=changed,
        source_branch=_ref(item.get("source_branch")) if is_mr else None,
        target_branch=_ref(item.get("target_branch")) if is_mr else None,
    )


def _grafana(headers: Mapping[str, str], payload: dict[str, Any]) -> NormalizedEvent:
    if _header(headers, "X-GitHub-Event") or _header(headers, "X-Gitlab-Event"):
        raise TypeMismatch("a forge event on a grafana-alert endpoint")
    labels = payload.get("commonLabels")
    if not isinstance(payload.get("alerts"), list) or not isinstance(labels, dict):
        raise Unsupported("not a grafana alert webhook body")
    annotations = payload.get("commonAnnotations") or {}
    canonical = json.dumps(dict(sorted(labels.items())), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    first = payload["alerts"][0] if payload["alerts"] else {}
    body = "\n\n".join(p for p in (annotations.get("summary"), annotations.get("description")) if p)
    return NormalizedEvent(
        type="grafana-alert",
        source="grafana",
        kind="alert",
        action=str(payload.get("status", "")),
        repo=None,
        number=None,
        title=labels.get("alertname") or payload.get("title") or "",
        body=body,
        comment_body=None,
        url=payload.get("externalURL") or first.get("generatorURL"),
        author=None,
        actor=None,
        labels=labels,
        delivery_id=None,
        dedup_key=f"grafana/{digest}",
        priority=0,
    )


def normalize(endpoint_type: str, headers: Mapping[str, str], payload: Any) -> NormalizedEvent:
    if endpoint_type not in ENDPOINT_TYPES:
        raise ValueError(f"unknown endpoint type {endpoint_type!r}")
    if not isinstance(payload, dict):
        raise Unsupported("payload is not a JSON object")
    provider = endpoint_type.split("-")[0]
    if provider == "grafana":
        return _grafana(headers, payload)
    other = "X-Gitlab-Event" if provider == "github" else "X-GitHub-Event"
    own = "X-GitHub-Event" if provider == "github" else "X-Gitlab-Event"
    if not _header(headers, own) and _header(headers, other):
        found = "gitlab" if provider == "github" else "github"
        raise TypeMismatch(f"a {found} event on a {endpoint_type} endpoint")
    return (_github if provider == "github" else _gitlab)(endpoint_type, headers, payload)


def classify(
    event: NormalizedEvent, *, bot_logins: Collection[str], allowed_authors: Collection[str]
) -> str | None:
    """The ignore reason for a normalised event, or None when it should run."""
    if event.actor and event.actor in bot_logins:
        return "self_event"
    if (
        event.kind in {"issue", "comment"}
        and allowed_authors
        and event.author not in allowed_authors
    ):
        return "author_not_allowed"
    if event.kind == "alert":
        return None if event.action == "firing" else "resolved"
    if event.action not in _HANDLED_ACTIONS[event.kind]:
        return "action_ignored"
    if event.action == "edited" and event.changed == ("title",):
        return "action_ignored"
    if event.kind == "mr" and event.draft and event.action != "ready_for_review":
        return "action_ignored"
    return None
