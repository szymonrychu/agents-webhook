"""README documents every setting, status, reason and endpoint type."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents_webhook.filters import FIELDS
from agents_webhook.normalize import ENDPOINT_TYPES
from agents_webhook.settings import LocalSettings, WebhookSettings
from agents_webhook.worker import REUSE_FALLBACK

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

STATUSES = ("new", "in-progress", "handled", "ignored", "failed")
REASONS = (
    "self_event",
    "resolved",
    "filter_denied",
    "filter_not_allowed",
    "filter_error",
    "action_ignored",
    "type_mismatch",
    "unsupported_event",
    "duplicate",
    "queue_full",
    "endpoint_removed",
    "preset_not_found",
    "cluster_not_found",
    "template_error",
    "timeout",
    "turn_failed",
    "message_failed",
    "message_cancelled",
    REUSE_FALLBACK,
)


def section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


@pytest.mark.parametrize("name", sorted(WebhookSettings.model_fields))
def test_every_webhook_variable_is_documented(name):
    assert f"`WEBHOOK_{name.upper()}`" in section("Configuration")


@pytest.mark.parametrize("name", sorted(set(LocalSettings.model_fields)))
def test_local_settings_fields_are_known(name):
    assert name in WebhookSettings.model_fields


@pytest.mark.parametrize("word", [*STATUSES, *REASONS])
def test_every_status_and_reason_is_documented(word):
    assert f"`{word}`" in section("Statuses and reasons")


@pytest.mark.parametrize("kind", ENDPOINT_TYPES)
def test_every_endpoint_type_is_documented(kind):
    assert f"`{kind}`" in section("Endpoints")


@pytest.mark.parametrize(
    "title",
    [
        "Overview",
        "Configuration",
        "Endpoints",
        "Filters",
        "Agent reuse",
        "Statuses and reasons",
        "Security",
        "Commands",
        "Helm chart",
        "Development",
        "API",
    ],
)
def test_sections_exist(title):
    assert section(title).strip()


@pytest.mark.parametrize(
    ("endpoint_type", "field"), [(t, f) for t, fs in FIELDS.items() for f in fs]
)
def test_filters_section_lists_every_field(endpoint_type, field):
    text = section("Filters")
    assert f"`{field}`" in text
    assert f"`{endpoint_type}`" in text


def test_retired_author_allowlist_not_documented():
    assert "WEBHOOK_ALLOWED_AUTHORS" not in README
    assert "author_not_allowed" not in README
