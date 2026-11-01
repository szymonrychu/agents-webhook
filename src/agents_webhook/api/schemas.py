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


AuthMode = Literal["basic", "header", "bearer", "hmac"]


def _preset_is_uuid(value: str) -> str:
    """The orchestrator serves presets by UUID only; anything else is an unknown preset."""
    try:
        uuid.UUID(value)
    except ValueError:
        raise ValueError(f"unknown preset {value!r}") from None
    return value


class EndpointIn(BaseModel):
    """`username` and `password` are for basic mode; the other modes get a generated secret."""

    type: str
    preset_id: str
    suffix: str | None = None
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    prompt_template: str | None = None
    filters: Filters | None = None
    auth_mode: AuthMode = "basic"
    auth_header: str | None = None
    hmac_algorithm: Literal["sha256"] | None = None
    hmac_prefix: str | None = None
    hmac_timestamp_header: str | None = None

    _preset = field_validator("preset_id")(_preset_is_uuid)


class EndpointPatch(BaseModel):
    """Every editable field; only the fields sent are changed, null clears a nullable one.

    There is no secret, password or type here (extra fields are refused): a secret is replaced
    only by rotate, or issued anew by a change of `auth_mode`.
    """

    model_config = ConfigDict(extra="forbid")

    preset_id: str | None = None
    prompt_template: str | None = None
    suffix: str | None = None
    filters: Filters | None = None
    username: str | None = None
    auth_mode: AuthMode | None = None
    auth_header: str | None = None
    hmac_algorithm: Literal["sha256"] | None = None
    hmac_prefix: str | None = None
    hmac_timestamp_header: str | None = None

    @field_validator("preset_id")
    @classmethod
    def _preset(cls, value: str | None) -> str | None:
        return None if value is None else _preset_is_uuid(value)


class CloneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suffix: str | None = None
    preset_id: str | None = None

    @field_validator("preset_id")
    @classmethod
    def _preset(cls, value: str | None) -> str | None:
        return None if value is None else _preset_is_uuid(value)


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
    username: str | None
    created_at: datetime
    credentials_rotated_at: datetime | None
    auth_mode: AuthMode
    auth_header: str | None
    hmac_algorithm: str | None
    hmac_prefix: str | None
    hmac_timestamp_header: str | None
    updated_at: datetime
    has_secret: bool


class EndpointCreated(EndpointView):
    """The only view that carries credentials, shown once: `password` (basic mode) or
    `secret` (header, bearer, hmac). Create, rotate, clone and a patch that changes the
    auth mode return it; a patch without a mode change returns both as null."""

    password: str | None = Field(default=None, repr=False)
    secret: str | None = Field(default=None, repr=False)


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
    session_id: str | None
    session_slug: str | None
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
