"""Request and response models of the UI API (`/webhooks/api/v1`)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FilterRule(BaseModel):
    field: str
    match: Literal["equals", "regex"] = "equals"
    values: list[str]


class Filters(BaseModel):
    allow: list[FilterRule] = []
    deny: list[FilterRule] = []


class FilterResult(BaseModel):
    passed: bool
    reason: str | None


class EndpointIn(BaseModel):
    type: str
    preset_id: str
    suffix: str | None = None
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    prompt_template: str | None = None
    filters: Filters | None = None

    @field_validator("preset_id")
    @classmethod
    def _preset_is_uuid(cls, value: str) -> str:
        """The orchestrator serves presets by UUID only; anything else is an unknown preset."""
        try:
            uuid.UUID(value)
        except ValueError:
            raise ValueError(f"unknown preset {value!r}") from None
        return value


class RotateIn(BaseModel):
    username: str | None = None
    password: str | None = Field(default=None, repr=False)


class EndpointView(BaseModel):
    id: int
    suffix: str
    url: str
    type: str
    preset_id: str
    prompt_template: str | None
    filters: Filters
    username: str
    created_at: datetime
    credentials_rotated_at: datetime | None


class EndpointCreated(EndpointView):
    """The only view that carries a password (create and rotate responses)."""

    password: str = Field(repr=False)


class EventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    received_at: datetime
    updated_at: datetime
    endpoint_id: int | None
    endpoint_suffix: str
    type: str
    source: str
    kind: str
    action: str | None
    repo: str | None
    number: int | None
    title: str | None
    url: str | None
    author: str | None
    status: str
    reason: str | None
    dedup_key: str
    deliveries: int
    preset_id: str | None
    cluster_id: str | None
    cluster_slug: str | None
    message_id: str | None
    update_of: int | None
    duplicate_of: int | None
    started_at: datetime | None
    finished_at: datetime | None


class EventDetail(EventView):
    payload: Any
    labels: Any


class PreviewIn(BaseModel):
    template: str | None = None
    type: str
    example: Any
    filters: Filters | None = None


class PreviewOut(BaseModel):
    prompt: str | None
    filters: FilterResult | None


class Limits(BaseModel):
    max_in_progress: int
    max_in_progress_per_endpoint: int
    max_queue: int
    max_body_bytes: int
    run_timeout_seconds: int
    auth_failures_per_minute: int


class Meta(BaseModel):
    public_base_url: str
    ingest_path_prefix: str
    types: list[str]
    filter_fields: dict[str, list[str]]
    limits: Limits
