"""Deterministic OpenAPI export: `python -m agents_webhook.api.openapi > openapi.json`."""

import json

from agents_webhook.api.app import create_api_app
from agents_webhook.settings import WebhookSettings


def export() -> str:
    """The api app's OpenAPI document as JSON with sorted keys and a trailing newline.

    Built without a database or an orchestrator: routes only close over their dependencies.
    """
    settings = WebhookSettings(
        database_url="postgresql://openapi/export",
        orchestrator_url="http://openapi",
        orchestrator_token="openapi",
    )
    app = create_api_app(settings, None, None)  # type: ignore[arg-type]
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(), end="")
