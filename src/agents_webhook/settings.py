"""WEBHOOK_* settings of agents-webhook."""

import ipaddress

from agents_common.settings import BaseServiceSettings, CsvList
from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict


class WebhookSettings(BaseServiceSettings):
    """Environment-driven settings; limits are set from Helm values (limits.*)."""

    model_config = SettingsConfigDict(env_prefix="WEBHOOK_", extra="ignore")

    public_base_url: str = "https://lab.szymonrichert.pl"
    bot_logins: CsvList = []
    allowed_authors: CsvList = []
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

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("trusted_proxies")
    @classmethod
    def _cidrs(cls, value: list[str]) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"WEBHOOK_TRUSTED_PROXIES::not a CIDR: {item!r}") from None
        return value
