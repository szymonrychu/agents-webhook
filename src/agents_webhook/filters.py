"""Per-endpoint allow and deny rules over normalized event fields (design D2, D3, D4; no I/O).

`validate` turns user input into the canonical form `{"allow": [Rule], "deny": [Rule]}` with
`Rule = {"field", "match", "values"}`, or raises `ValueError` whose message starts with the
rule path. `evaluate` returns the ignore reason for an event or None.

User regexes compile with RE2 (the `google-re2` package, module `re2`): matching is linear in
the input and backreferences and lookaround are refused at compile time, so no pattern can
backtrack catastrophically on the public ingest path. stdlib `re` never sees a user pattern.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import re2

from agents_webhook.normalize import NormalizedEvent

_FORGE = ("author", "actor", "repo", "labels", "title", "body", "comment", "kind", "action")
_BRANCHES = ("source_branch", "target_branch")

FIELDS: dict[str, tuple[str, ...]] = {
    "github-issue": _FORGE,
    "github-pr": _FORGE + _BRANCHES,
    "gitlab-issue": _FORGE,
    "gitlab-mr": _FORGE + _BRANCHES,
    "grafana-alert": ("labels", "title", "body"),
}
MATCHES = ("equals", "regex")
EMPTY = {"allow": [], "deny": []}
DENIED = "filter_denied"
NOT_ALLOWED = "filter_not_allowed"

MAX_RULES = 20
MAX_VALUES = 50
MAX_VALUE_LENGTH = 256
MAX_TEXT = 65536

_LISTS = ("allow", "deny")
_RULE_KEYS = ("field", "match", "values")
# Field name to NormalizedEvent attribute where they differ.
_ATTRIBUTES = {"comment": "comment_body"}
# Forges treat logins, repo paths and label names case-insensitively (D2).
_CASEFOLDED = frozenset({"author", "actor", "repo", "labels"})
_GRAFANA = "grafana-alert"
_CACHE_SIZE = 256


def _compile(pattern: str) -> Any:
    options = re2.Options()
    options.log_errors = False  # a refused pattern must not reach stderr
    return re2.compile(pattern, options=options)


def _read(text: str) -> str:
    return text[:MAX_TEXT]


def _grafana_label(value: str) -> tuple[str, str] | None:
    key, sep, rest = value.partition("=")
    return (key, rest) if sep and key else None


def _validate_value(endpoint_type: str, field: str, match: str, value: Any, path: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{path}: must be a string")
    if not value:
        raise ValueError(f"{path}: must not be empty")
    if len(value) > MAX_VALUE_LENGTH:
        raise ValueError(f"{path}: longer than {MAX_VALUE_LENGTH} characters")
    pattern = value
    if endpoint_type == _GRAFANA and field == "labels":
        label = _grafana_label(value)
        if label is None:
            raise ValueError(f"{path}: must be key=value with a non-empty key")
        pattern = label[1]
    if match == "regex":
        try:
            _compile(pattern)
        except re2.error as exc:
            detail = exc.args[0] if exc.args else exc
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise ValueError(f"{path}: not a valid RE2 pattern ({detail})") from None


def _validate_rule(endpoint_type: str, rule: Any, path: str) -> dict[str, Any]:
    if not isinstance(rule, Mapping):
        raise ValueError(f"{path}: must be an object")
    for key in rule:
        if key not in _RULE_KEYS:
            raise ValueError(f"{path}.{key}: unknown key")
    if "field" not in rule:
        raise ValueError(f"{path}.field: required")
    field = rule["field"]
    if field not in FIELDS[endpoint_type]:
        raise ValueError(f"{path}.field: {field!r} does not apply to {endpoint_type}")
    match = rule.get("match")
    if match is None:
        match = "equals"
    if match not in MATCHES:
        raise ValueError(f"{path}.match: must be one of {', '.join(MATCHES)}")
    values = rule.get("values")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
        raise ValueError(f"{path}.values: must be a list of 1 to {MAX_VALUES} strings")
    for index, value in enumerate(values):
        _validate_value(endpoint_type, field, match, value, f"{path}.values[{index}]")
    return {"field": field, "match": match, "values": list(values)}


def validate(endpoint_type: str, data: Any) -> dict:
    """The canonical form of `data` for an endpoint of `endpoint_type`, or ValueError."""
    if endpoint_type not in FIELDS:
        raise ValueError(f"filters: unknown endpoint type {endpoint_type!r}")
    if not isinstance(data, Mapping):
        raise ValueError("filters: must be an object")
    for key in data:
        if key not in _LISTS:
            raise ValueError(f"filters.{key}: unknown key")
    canonical: dict[str, list[dict[str, Any]]] = {}
    for name in _LISTS:
        rules = data.get(name)
        if rules is None:
            rules = []
        if not isinstance(rules, list):
            raise ValueError(f"filters.{name}: must be a list")
        if len(rules) > MAX_RULES:
            raise ValueError(f"filters.{name}: at most {MAX_RULES} rules")
        canonical[name] = [
            _validate_rule(endpoint_type, rule, f"filters.{name}[{index}]")
            for index, rule in enumerate(rules)
        ]
    return canonical


@dataclass(frozen=True)
class _Rule:
    field: str
    attribute: str
    matches: Callable[[Any], bool]


def _text_matcher(field: str, match: str, values: list[str]) -> Callable[[str], bool]:
    if match == "regex":
        patterns = [_compile(value) for value in values]
        return lambda text: any(p.search(_read(text)) is not None for p in patterns)
    if field in _CASEFOLDED:
        folded = frozenset(value.casefold() for value in values)
        return lambda text: _read(text).casefold() in folded
    exact = frozenset(values)
    return lambda text: _read(text) in exact


def _grafana_labels_matcher(match: str, values: list[str]) -> Callable[[Any], bool]:
    tests: list[tuple[str, Callable[[str], bool]]] = []
    for value in values:
        key, _, expected = value.partition("=")
        if match == "regex":
            pattern = _compile(expected)
            tests.append((key, lambda text, p=pattern: p.search(_read(text)) is not None))
        else:
            tests.append((key, lambda text, e=expected: _read(text) == e))

    def matches(labels: Any) -> bool:
        if not isinstance(labels, Mapping):
            return False
        return any(isinstance(labels.get(key), str) and test(labels[key]) for key, test in tests)

    return matches


def _field_matcher(text_matches: Callable[[str], bool]) -> Callable[[Any], bool]:
    def matches(value: Any) -> bool:
        if isinstance(value, str):
            return text_matches(value)
        if isinstance(value, list | tuple):  # forge labels: any element
            return any(isinstance(item, str) and text_matches(item) for item in value)
        return False  # absent (None) never matches

    return matches


def _build_rule(endpoint_type: str, rule: Mapping[str, Any]) -> _Rule:
    field, match, values = rule["field"], rule["match"], rule["values"]
    if endpoint_type == _GRAFANA and field == "labels":
        matches = _grafana_labels_matcher(match, values)
    else:
        matches = _field_matcher(_text_matcher(field, match, values))
    return _Rule(field, _ATTRIBUTES.get(field, field), matches)


@lru_cache(maxsize=_CACHE_SIZE)
def _rule_set(endpoint_type: str, key: str) -> tuple[tuple[_Rule, ...], tuple[_Rule, ...]]:
    canonical = validate(endpoint_type, json.loads(key))
    allow = tuple(_build_rule(endpoint_type, rule) for rule in canonical["allow"])
    deny = tuple(_build_rule(endpoint_type, rule) for rule in canonical["deny"])
    return allow, deny


def evaluate(event: NormalizedEvent, filters: Mapping[str, Any]) -> str | None:
    """`filter_denied: <field>`, `filter_not_allowed: <field>` or None (event passes)."""
    if not filters.get("allow") and not filters.get("deny"):
        return None
    allow, deny = _rule_set(event.type, json.dumps(filters, sort_keys=True, default=dict))
    for rule in deny:
        if rule.matches(getattr(event, rule.attribute)):
            return f"{DENIED}: {rule.field}"
    for rule in allow:
        if not rule.matches(getattr(event, rule.attribute)):
            return f"{NOT_ALLOWED}: {rule.field}"
    return None
