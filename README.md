# agents-webhook

Part of the agents-webhook-component project (python).

Webhook endpoints that turn provider events (GitHub, GitLab, Grafana) into prompts for agents run by agent-orchestrator. One image, four subcommands: `migrate`, `ingest` (public listener), `api` (UI API) and `worker` (dispatcher).

## Ingest

`POST /webhook/{suffix}` is the only ingest path. It is served by the public `ingest` Deployment (no SSO). Each endpoint has a type (`grafana-alert`, `gitlab-issue`, `gitlab-mr`, `github-issue`, `github-pr`), a preset, an optional prompt template and HTTP basic-auth credentials.

| Status | Meaning |
|---|---|
| 202 | accepted or ignored (`{id, status, reason}`); a GitHub `ping` answers `{"status": "pong"}` |
| 400 | body is not JSON |
| 401 | unknown suffix or wrong credentials (identical answers, `WWW-Authenticate: Basic`) |
| 413 | body over `WEBHOOK_MAX_BODY_BYTES` (checked after authentication) |
| 429 | too many failed authentications from one client (`Retry-After`) |
| 503 | the database write failed, so the sender retries |

```
curl -u "$USERNAME:$PASSWORD" -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: issues' -d @event.json \
  https://lab.szymonrichert.pl/webhook/<suffix>
```

## Prompt templates

An endpoint's `prompt_template` is a Jinja2 template for the initial prompt. Without one the default prompt is used: a preamble plus an escaped `<event>` data block.

- Templates run in a sandboxed Jinja2 environment (`ImmutableSandboxedEnvironment`): no loader (no `include` or `import` of files), autoescape off, `StrictUndefined` (an undefined variable is an error, not an empty string), output capped at 64 KiB, template source at most 16 KiB, `range` limited to 10000 items, and repetition, product and power results bounded. Each render runs in a child process killed after `WEBHOOK_TEMPLATE_TIMEOUT_SECONDS` (default 2).
- The `from_json` filter parses a JSON string into a value: `{% set body = payload.message | from_json %}{{ body.field }}`. A value that is not valid JSON is a render error.
- Context variables for every type: `type`, `source`, `kind`, `action`, `repo`, `number`, `title`, `body`, `comment_body` (null unless the event is a comment), `url`, `author`, `actor`, `labels`, `delivery_id`, `dedup_key`, `priority`, `draft`, `changed`, plus `payload` (the raw request JSON, `{}` when it is not an object) and `event` (the default block, fully escaped). The other variables are raw: escape or frame untrusted text yourself.
- `labels` is a list of names for `github-issue`, `github-pr`, `gitlab-issue` and `gitlab-mr`, and a mapping (the alert's `commonLabels`) for `grafana-alert`.
- Creating an endpoint validates the template (syntax, unknown filters and tags): 422 `invalid_request` naming the line.
- A template that fails at delivery time (undefined variable, bad `from_json` input, timeout, size) fails the event with reason `template_error`.
- To check a template against an example JSON payload without storing anything, call `POST /webhooks/api/v1/templates/preview`.

## API

The UI API lives under `/webhooks/api/v1` in the `api` Deployment behind the SSO sidecar. Errors are `{"error": {"code": "...", "message": "..."}}`; codes: `invalid_request` (422), `unknown_preset` (422), `not_found` (404), `suffix_taken` (409), `orchestrator_unavailable` (503), `unavailable` (503). The committed schema is `openapi.json` (regenerate with `uv run python -m agents_webhook.api.openapi > openapi.json`; a test fails on drift).

Calling headless: through SSO send a Keycloak bearer token (`Authorization: Bearer $TOKEN`); the injected oauth2-proxy sidecar already accepts it (it sets `OAUTH2_PROXY_SKIP_JWT_BEARER_TOKENS=true` itself), so no per-deployment setting is needed. Without SSO, `kubectl -n agents port-forward deploy/agents-webhook-api 8000:8000` (the api container port) and use `http://localhost:8000`. The examples use:

```
API=https://lab.szymonrichert.pl/webhooks/api/v1
AUTH="Authorization: Bearer $TOKEN"
```

### `GET /webhooks/api/v1/endpoints`

Lists endpoints: 200, an array of `{id, suffix, url, type, preset_id, prompt_template, username, created_at, credentials_rotated_at}`. Never carries a password.

```
curl -H "$AUTH" $API/endpoints
```

### `POST /webhooks/api/v1/endpoints`

Body `{type, preset_id, suffix?, username?, password?, prompt_template?}`; a missing suffix, username or password is generated (password at least `WEBHOOK_MIN_PASSWORD_LENGTH` characters when given). Response 201: the endpoint view plus `password`, shown only here (`Cache-Control: no-store`). Errors: 422 `invalid_request` (bad type, suffix, credentials or template), 422 `unknown_preset`, 409 `suffix_taken`, 503 `orchestrator_unavailable`.

```
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"github-pr","preset_id":"<preset id>","prompt_template":"Review {{ repo }}#{{ number }}: {{ title }}"}' \
  $API/endpoints
```

### `POST /webhooks/api/v1/endpoints/{id}/rotate`

Issues new credentials and invalidates the old password. Optional body `{username?, password?}`. Response 200: the endpoint view plus `username` and `password` (`Cache-Control: no-store`). Errors: 404 `not_found`, 422 `invalid_request`.

```
curl -X POST -H "$AUTH" $API/endpoints/1/rotate
```

### `DELETE /webhooks/api/v1/endpoints/{id}`

Deletes the endpoint; its events remain. Response 204. Errors: 404 `not_found`.

```
curl -X DELETE -H "$AUTH" $API/endpoints/1
```

### `GET /webhooks/api/v1/events`

Lists events newest first. Query: `status` (`new`, `in-progress`, `handled`, `ignored`, `failed`), `endpoint_id`, `limit` (1 to 200, default 50), `before_id` (keyset paging). Response 200: an array of event views (no payload).

```
curl -H "$AUTH" "$API/events?status=failed&limit=20"
```

### `GET /webhooks/api/v1/events/{id}`

One event with its raw `payload` and `labels`. Response 200. Errors: 404 `not_found`.

```
curl -H "$AUTH" $API/events/42
```

### `POST /webhooks/api/v1/templates/preview`

Renders a template against an example JSON payload and returns the prompt; nothing is stored. Body `{template, type, example}`: `type` is an endpoint type and `example` the provider payload of that type (it is normalized exactly as at ingest, so the context variables are the real ones; `payload` is `example`). Response 200 `{prompt}`. Errors: 422 `invalid_request` when `type` is unknown, the example is not a payload of that type, or the template does not compile or render (the message names the cause).

```
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"github-issue","template":"{{ title }} by {{ author }}","example":{"action":"opened","issue":{"number":1,"title":"Hi","body":"x","user":{"login":"bob"},"labels":[]},"repository":{"full_name":"o/r"},"sender":{"login":"bob"}}}' \
  $API/templates/preview
```

### `GET /webhooks/api/v1/meta`

Response 200: `{public_base_url, ingest_path_prefix, types, limits}` where `limits` has `max_in_progress`, `max_in_progress_per_endpoint`, `max_queue`, `max_body_bytes`, `run_timeout_seconds` and `auth_failures_per_minute`.

```
curl -H "$AUTH" $API/meta
```
