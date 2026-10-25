"""WEBHOOK_* settings of agents-webhook."""

import ipaddress

from agents_common.settings import BaseServiceSettings, CsvList
from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import SettingsConfigDict


class WebhookSettings(BaseServiceSettings):
    """Environment-driven settings; limits are set from Helm values (limits.*)."""

    model_config = SettingsConfigDict(env_prefix="WEBHOOK_", extra="ignore")

    public_base_url: str = "https://lab.szymonrichert.pl"
    bot_logins: CsvList = []
    max_in_progress: int = Field(3, ge=1)
    max_in_progress_per_endpoint: int = Field(1, ge=1)
    max_queue: int = Field(500, ge=1)
    max_body_bytes: int = Field(5 * 1024 * 1024, ge=1)
    run_timeout_seconds: int = Field(3600, ge=1)
    poll_seconds: float = Field(5, gt=0)
    events_retention_days: int = Field(30, ge=1)
    auth_failures_per_minute: int = Field(10, ge=1)
    trusted_proxies: CsvList = []
    min_password_length: int = Field(16, ge=1)
    template_timeout_seconds: float = Field(2.0, gt=0)
    template_memory_mb: int = Field(256, ge=32)
    api_allowed_cidrs: CsvList = ["127.0.0.1/32"]

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("trusted_proxies", "api_allowed_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(
                    f"WEBHOOK_{str(info.field_name).upper()}::not a CIDR: {item!r}"
                ) from None
        return value

    def api_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.api_allowed_cidrs]


class LocalSettings(WebhookSettings):
    """Settings for the subcommands that never talk to the orchestrator (migrate, ingest)."""

    orchestrator_url: str = ""

    @model_validator(mode="after")
    def _token_from_file(self):  # overrides the base check: no orchestrator token is needed
        return self
