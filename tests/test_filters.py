import time
from dataclasses import replace

import pytest

from agents_webhook.filters import (
    DENIED,
    EMPTY,
    FIELDS,
    MATCHES,
    MAX_LIST_ITEMS,
    MAX_RULES,
    MAX_TEXT,
    MAX_TOTAL_VALUES,
    MAX_VALUE_LENGTH,
    MAX_VALUES,
    NOT_ALLOWED,
    evaluate,
    validate,
)
from agents_webhook.normalize import NormalizedEvent

PR = NormalizedEvent(
    type="github-pr",
    source="github",
    kind="mr",
    action="opened",
    repo="Acme/Widgets",
    number=7,
    title="Fix the thing",
    body="Long description",
    comment_body=None,
    url="https://github.com/Acme/Widgets/pull/7",
    author="Alice",
    actor="Alice",
    labels=["Bug", "infra"],
    delivery_id="d-1",
    dedup_key="k-1",
    priority=0,
    source_branch="feature/x",
    target_branch="main",
)

ALERT = NormalizedEvent(
    type="grafana-alert",
    source="grafana",
    kind="alert",
    action="firing",
    repo=None,
    number=None,
    title="HighLatency",
    body="p99 above 2s",
    comment_body=None,
    url=None,
    author=None,
    actor=None,
    labels={"env": "staging", "team": "core", "url": "a=b"},
    delivery_id=None,
    dedup_key="g-1",
    priority=0,
)


def _rule(field, *values, match=None):
    rule = {"field": field, "values": list(values)}
    if match is not None:
        rule["match"] = match
    return rule


def _run(event, allow=(), deny=()):
    return evaluate(event, validate(event.type, {"allow": list(allow), "deny": list(deny)}))


def test_fields_per_type():
    forge = ("author", "actor", "repo", "labels", "title", "body", "comment", "kind", "action")
    branches = ("source_branch", "target_branch")
    assert {name: tuple(fields) for name, fields in FIELDS.items()} == {
        "github-issue": forge,
        "gitlab-issue": forge,
        "github-pr": forge + branches,
        "gitlab-mr": forge + branches,
        "grafana-alert": ("labels", "title", "body"),
    }
    assert MATCHES == ("equals", "regex")
    assert EMPTY == {"allow": [], "deny": []}
    assert (DENIED, NOT_ALLOWED) == ("filter_denied", "filter_not_allowed")


def test_validate_canonical_form():
    assert validate(
        "grafana-alert", {"deny": [{"field": "labels", "values": ["env=staging"]}]}
    ) == {
        "allow": [],
        "deny": [{"field": "labels", "match": "equals", "values": ["env=staging"]}],
    }
    assert validate("github-pr", {}) == EMPTY
    assert validate("github-pr", {"allow": None, "deny": None}) == EMPTY
    data = {
        "allow": [
            _rule("author", "alice", "bob"),
            _rule("title", "^fix", match="regex"),
            {"field": "repo", "match": None, "values": ["acme/widgets"]},
        ]
    }
    assert validate("github-pr", data) == {
        "allow": [
            {"field": "author", "match": "equals", "values": ["alice", "bob"]},
            {"field": "title", "match": "regex", "values": ["^fix"]},
            {"field": "repo", "match": "equals", "values": ["acme/widgets"]},
        ],
        "deny": [],
    }
    # The canonical form is a fresh object: the input and EMPTY stay untouched.
    validate("github-pr", {})["allow"].append("x")
    assert EMPTY == {"allow": [], "deny": []}
    assert "match" not in data["allow"][0]


@pytest.mark.parametrize(
    ("endpoint_type", "data", "prefix"),
    [
        ("github-pr", ["allow"], "filters: "),
        ("github-pr", None, "filters: "),
        ("github-pr", "allow", "filters: "),
        ("unknown-type", {}, "filters: "),
        ("github-pr", {"allow": [], "block": []}, "filters.block: "),
        ("github-pr", {"allow": "author"}, "filters.allow: "),
        ("github-pr", {"allow": ["author"]}, "filters.allow[0]: "),
        ("github-pr", {"allow": [{**_rule("author", "a"), "op": "x"}]}, "filters.allow[0].op: "),
        ("github-pr", {"allow": [{"values": ["a"]}]}, "filters.allow[0].field: "),
        ("github-pr", {"deny": [_rule("number", "1")]}, "filters.deny[0].field: "),
        ("github-pr", {"deny": [_rule("draft", "true")]}, "filters.deny[0].field: "),
        ("github-pr", {"deny": [_rule("author", "a", match="glob")]}, "filters.deny[0].match: "),
        ("github-pr", {"allow": [{"field": "author"}]}, "filters.allow[0].values: "),
        ("github-pr", {"allow": [_rule("author")]}, "filters.allow[0].values: "),
        (
            "github-pr",
            {"allow": [{"field": "author", "values": "alice"}]},
            "filters.allow[0].values: ",
        ),
        ("github-pr", {"allow": [_rule("author", "a", 5)]}, "filters.allow[0].values[1]: "),
        ("github-pr", {"allow": [_rule("author", "a", None)]}, "filters.allow[0].values[1]: "),
        ("github-pr", {"allow": [_rule("author", "")]}, "filters.allow[0].values[0]: "),
        ("grafana-alert", {"deny": [_rule("labels", "env")]}, "filters.deny[0].values[0]: "),
        ("grafana-alert", {"deny": [_rule("labels", "=x")]}, "filters.deny[0].values[0]: "),
        (
            "grafana-alert",
            {"deny": [_rule("labels", "env=(", match="regex")]},
            "filters.deny[0].values[0]: not a valid RE2 pattern (",
        ),
        (
            "github-pr",
            {"allow": [_rule("author", "a"), _rule("title", "x", "y", "(", match="regex")]},
            "filters.allow[1].values[2]: not a valid RE2 pattern (",
        ),
    ],
)
def test_validate_rejects_with_path(endpoint_type, data, prefix):
    with pytest.raises(ValueError) as caught:
        validate(endpoint_type, data)
    assert str(caught.value).startswith(prefix)


def test_validate_rejects_field_for_type():
    with pytest.raises(ValueError) as caught:
        validate("grafana-alert", {"deny": [_rule("repo", "acme/widgets")]})
    assert str(caught.value) == "filters.deny[0].field: 'repo' does not apply to grafana-alert"
    for endpoint_type in ("github-issue", "gitlab-issue"):
        with pytest.raises(ValueError) as caught:
            validate(endpoint_type, {"allow": [_rule("source_branch", "main")]})
        assert str(caught.value) == (
            f"filters.allow[0].field: 'source_branch' does not apply to {endpoint_type}"
        )
    for field in ("author", "actor", "comment", "kind", "action", "target_branch"):
        with pytest.raises(ValueError, match=r"^filters\.allow\[0\]\.field: "):
            validate("grafana-alert", {"allow": [_rule(field, "x")]})
    for endpoint_type in ("github-pr", "gitlab-mr"):
        assert validate(endpoint_type, {"allow": [_rule("source_branch", "main")]})["allow"]


def test_validate_rejects_non_re2_patterns():
    for pattern in (r"(a)\1", r"foo(?=bar)", r"foo(?!bar)", r"(?<=a)b", r"(?<!a)b"):
        with pytest.raises(ValueError) as caught:
            validate("github-pr", {"deny": [_rule("title", "ok", pattern, match="regex")]})
        assert str(caught.value).startswith("filters.deny[0].values[1]: not a valid RE2 pattern (")
    with pytest.raises(ValueError, match=r"^filters\.allow\[0\]\.values\[0\]: not a valid RE2"):
        validate("grafana-alert", {"allow": [_rule("labels", r"env=(a)\1", match="regex")]})
    # The same strings are plain literals under equals.
    assert validate("github-pr", {"deny": [_rule("title", r"(a)\1")]})["deny"]


def test_validate_bounds():
    assert (MAX_RULES, MAX_VALUES, MAX_VALUE_LENGTH, MAX_TEXT) == (20, 50, 256, 65536)
    rules = [_rule("author", f"user{i}") for i in range(MAX_RULES)]
    assert len(validate("github-pr", {"allow": rules, "deny": rules})["deny"]) == MAX_RULES
    with pytest.raises(ValueError, match=r"^filters\.deny: "):
        validate("github-pr", {"deny": [*rules, _rule("author", "one-more")]})

    values = [f"user{i}" for i in range(MAX_VALUES)]
    assert validate("github-pr", {"allow": [_rule("author", *values)]})["allow"][0]["values"] == (
        values
    )
    with pytest.raises(ValueError, match=r"^filters\.allow\[0\]\.values: "):
        validate("github-pr", {"allow": [_rule("author", *values, "one-more")]})

    longest = "a" * MAX_VALUE_LENGTH
    assert validate("github-pr", {"allow": [_rule("title", longest, match="regex")]})["allow"]
    with pytest.raises(ValueError, match=r"^filters\.allow\[0\]\.values\[1\]: "):
        validate("github-pr", {"allow": [_rule("title", "ok", longest + "a")]})


def test_deny_first_match_wins():
    deny = [
        _rule("repo", "other/repo"),
        _rule("title", "thing", match="regex"),
        _rule("author", "alice"),
    ]
    # The second deny rule is the first that matches; the failing allow rule is never reached.
    assert _run(PR, allow=[_rule("author", "bob")], deny=deny) == "filter_denied: title"
    assert _run(PR, deny=deny[:1]) is None
    assert _run(PR, deny=[deny[2], deny[1]]) == f"{DENIED}: author"


def test_allow_rules_and_values_or():
    assert _run(PR, allow=[_rule("author", "bob", "alice")]) is None
    assert _run(PR, allow=[_rule("author", "alice"), _rule("repo", "acme/widgets")]) is None
    mallory = replace(PR, author="mallory")
    assert _run(mallory, allow=[_rule("author", "alice")]) == "filter_not_allowed: author"
    assert (
        _run(PR, allow=[_rule("author", "alice"), _rule("repo", "acme/other")])
        == f"{NOT_ALLOWED}: repo"
    )
    assert (
        _run(PR, allow=[_rule("target_branch", "dev"), _rule("repo", "acme/other")])
        == "filter_not_allowed: target_branch"
    )
    assert _run(PR) is None
    assert evaluate(PR, EMPTY) is None


def test_absent_field_fails_allow_not_deny():
    any_text = _rule("comment", ".*", match="regex")
    assert _run(PR, allow=[any_text]) == "filter_not_allowed: comment"
    assert _run(PR, deny=[any_text]) is None
    commented = replace(PR, kind="comment", comment_body="LGTM", source_branch=None)
    assert _run(commented, allow=[any_text]) is None
    assert _run(commented, deny=[any_text]) == "filter_denied: comment"
    assert _run(commented, allow=[_rule("source_branch", "feature/x")]) == (
        "filter_not_allowed: source_branch"
    )
    assert _run(commented, deny=[_rule("source_branch", ".*", match="regex")]) is None
    anonymous = replace(PR, author=None)
    assert _run(anonymous, allow=[_rule("author", "alice")]) == "filter_not_allowed: author"
    assert _run(anonymous, deny=[_rule("author", "alice")]) is None


def test_equals_case_rules():
    # Logins, repo paths and forge label names compare case-insensitively.
    assert _run(PR, allow=[_rule("author", "ALICE")]) is None
    assert _run(PR, allow=[_rule("actor", "alice")]) is None
    assert _run(PR, allow=[_rule("repo", "acme/widgets")]) is None
    assert _run(PR, deny=[_rule("labels", "bug")]) == "filter_denied: labels"
    assert _run(PR, deny=[_rule("labels", "INFRA")]) == "filter_denied: labels"
    # Every other field compares exactly.
    assert _run(PR, allow=[_rule("target_branch", "Main")]) == "filter_not_allowed: target_branch"
    assert _run(PR, allow=[_rule("target_branch", "main")]) is None
    assert _run(PR, allow=[_rule("source_branch", "Feature/X")]) == (
        "filter_not_allowed: source_branch"
    )
    assert _run(PR, allow=[_rule("title", "fix the thing")]) == "filter_not_allowed: title"
    assert _run(PR, allow=[_rule("title", "Fix the thing")]) is None
    assert _run(PR, allow=[_rule("kind", "MR")]) == "filter_not_allowed: kind"
    assert _run(PR, allow=[_rule("action", "opened")]) is None


def test_regex_search_semantics():
    regex = "regex"
    assert _run(PR, deny=[_rule("title", "the", match=regex)]) == "filter_denied: title"
    assert _run(PR, deny=[_rule("title", "^the", match=regex)]) is None
    assert _run(PR, deny=[_rule("title", "^Fix the thing$", match=regex)]) == "filter_denied: title"
    assert _run(PR, deny=[_rule("title", "FIX", match=regex)]) is None
    assert _run(PR, deny=[_rule("title", "(?i)FIX", match=regex)]) == "filter_denied: title"
    # Forge labels: any element; regex is case-sensitive unless (?i).
    assert _run(PR, deny=[_rule("labels", "^inf", match=regex)]) == "filter_denied: labels"
    assert _run(PR, deny=[_rule("labels", "^bug$", match=regex)]) is None
    assert _run(PR, deny=[_rule("author", "^Ali", match=regex)]) == "filter_denied: author"

    # A text of exactly MAX_TEXT characters is searched whole.
    inside = replace(PR, body="x" * (MAX_TEXT - len("needle")) + "needle")
    needle = _rule("body", "needle", match=regex)
    assert _run(inside, deny=[needle]) == "filter_denied: body"
    assert _run(inside, allow=[_rule("body", "needle$", match=regex)]) is None
    assert _run(inside, deny=[_rule("body", "^x*$", match=regex)]) is None


def test_oversized_text_fails_closed():
    regex = "regex"
    oversized = replace(PR, body="x" * (MAX_TEXT + 1), title="t" * (MAX_TEXT + 1))
    # An allow regex never passes a text longer than the window, even one it would match.
    assert _run(oversized, allow=[_rule("body", "x", match=regex)]) == "filter_not_allowed: body"
    # A deny regex always trips on it, even one it would not match.
    assert _run(oversized, deny=[_rule("body", "needle", match=regex)]) == "filter_denied: body"
    # Padding a denied word past the window does not get it through.
    padded = replace(PR, body="x" * MAX_TEXT + "forbidden")
    assert _run(padded, deny=[_rule("body", "forbidden", match=regex)]) == "filter_denied: body"
    # An end-anchored allow regex cannot match at the cut point.
    cut = replace(PR, body="a" * (MAX_TEXT - 1) + "x" + "tail")
    assert _run(cut, allow=[_rule("body", "x$", match=regex)]) == "filter_not_allowed: body"
    # Equals compares whole values, so an oversized text neither equals nor trips anything.
    assert _run(oversized, deny=[_rule("title", "t" * MAX_VALUE_LENGTH)]) is None
    assert _run(oversized, allow=[_rule("title", "t" * MAX_VALUE_LENGTH)]) == (
        "filter_not_allowed: title"
    )
    # List and label elements fail closed per element.
    long_label = "l" * (MAX_TEXT + 1)
    labelled = replace(PR, labels=["Bug", long_label])
    assert _run(labelled, deny=[_rule("labels", "^nothing$", match=regex)]) == (
        "filter_denied: labels"
    )
    assert _run(labelled, allow=[_rule("labels", "^Bug$", match=regex)]) is None
    assert _run(replace(PR, labels=[long_label]), allow=[_rule("labels", "l", match=regex)]) == (
        "filter_not_allowed: labels"
    )
    alert = replace(ALERT, labels={"env": long_label})
    assert _run(alert, deny=[_rule("labels", "env=^prod$", match=regex)]) == (
        "filter_denied: labels"
    )
    assert _run(alert, allow=[_rule("labels", "env=l", match=regex)]) == (
        "filter_not_allowed: labels"
    )
    assert _run(alert, deny=[_rule("labels", "team=.*", match=regex)]) is None


def test_grafana_label_rules():
    labels = "labels"
    assert _run(ALERT, deny=[_rule(labels, "env=staging")]) == "filter_denied: labels"
    assert _run(ALERT, deny=[_rule(labels, "env=prod", "team=core")]) == "filter_denied: labels"
    assert _run(ALERT, deny=[_rule(labels, "env=stag")]) is None
    assert _run(ALERT, deny=[_rule(labels, "env=Staging")]) is None
    assert _run(ALERT, deny=[_rule(labels, "url=a=b")]) == "filter_denied: labels"
    regex = "regex"
    assert _run(ALERT, deny=[_rule(labels, "env=^stag", match=regex)]) == "filter_denied: labels"
    assert _run(ALERT, deny=[_rule(labels, "env=^prod", match=regex)]) is None
    assert _run(ALERT, deny=[_rule(labels, "env=", match=regex)]) == "filter_denied: labels"
    # A key the alert group does not carry never matches.
    assert _run(ALERT, deny=[_rule(labels, "region=eu")]) is None
    assert _run(ALERT, deny=[_rule(labels, "region=.*", match=regex)]) is None
    assert _run(ALERT, allow=[_rule(labels, "region=.*", match=regex)]) == (
        "filter_not_allowed: labels"
    )
    assert _run(
        ALERT, allow=[_rule(labels, "team=core"), _rule("title", "^High", match=regex)]
    ) is (None)
    assert _run(replace(ALERT, labels={}), deny=[_rule(labels, "env=staging")]) is None


def test_catastrophic_pattern_is_linear():
    catastrophic = validate("github-pr", {"deny": [_rule("body", "(a+)+$", match="regex")]})
    literal = replace(PR, body="a" * 100_000 + "!")
    # The failing '!' inside the window: a backtracking engine never returns on this one.
    in_window = replace(PR, body="a" * (MAX_TEXT - 1) + "!")
    started = time.monotonic()
    literal_result = evaluate(literal, catastrophic)
    in_window_result = evaluate(in_window, catastrophic)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    # The literal input is longer than MAX_TEXT, so the deny regex fails closed.
    assert literal_result == "filter_denied: body"
    assert in_window_result is None


def test_total_values_budget():
    assert MAX_TOTAL_VALUES == 200
    full = [_rule("author", *[f"u{r}-{i}" for i in range(MAX_VALUES)]) for r in range(4)]
    assert len(validate("github-pr", {"allow": full[:2], "deny": full[2:]})["deny"]) == 2
    with pytest.raises(ValueError, match=r"^filters\.deny\[1\]: .*200"):
        validate("github-pr", {"allow": full[:3], "deny": [full[3], _rule("author", "extra")]})
    with pytest.raises(ValueError, match=r"^filters\.allow\[4\]: .*200"):
        validate("github-pr", {"allow": [*full, _rule("author", "extra")]})


def test_oversized_list_fails_closed():
    assert MAX_LIST_ITEMS == 100
    long = replace(PR, labels=[f"l{i}" for i in range(MAX_LIST_ITEMS + 1)])
    ok = replace(PR, labels=[f"l{i}" for i in range(MAX_LIST_ITEMS)])
    regex = "regex"
    assert _run(ok, deny=[_rule("labels", "^l99$", match=regex)]) == "filter_denied: labels"
    assert _run(ok, allow=[_rule("labels", "^l99$", match=regex)]) is None
    # Past the cap even a matching element cannot satisfy allow, and any rule trips deny.
    assert _run(long, allow=[_rule("labels", "^l0$", match=regex)]) == "filter_not_allowed: labels"
    assert _run(long, deny=[_rule("labels", "^nomatch$", match=regex)]) == "filter_denied: labels"
    assert _run(long, allow=[_rule("labels", "l0")]) == "filter_not_allowed: labels"
    assert _run(long, deny=[_rule("labels", "nomatch")]) == "filter_denied: labels"
