# agents-webhook

Part of the agents-webhook-component project (python).

Webhook endpoints that turn provider events (GitHub, GitLab, Grafana) into prompts for agents run by agents-orchestrator. One image, four subcommands: `migrate`, `ingest` (public listener), `api` (UI API) and `worker` (dispatcher).

## Overview

agents-webhook turns provider events into prompts for agents run by agents-orchestrator. One image, four processes (subcommands):

- `ingest`: the public listener for `POST /webhook/{suffix}`. It authenticates, normalizes and records the event in Postgres and answers 202; it never talks to the orchestrator and holds no orchestrator token.
- `api`: the UI API under `/webhooks/api/v1`, behind SSO. It manages endpoints and reads events.
- `worker`: the single dispatcher (one advisory-lock holder). It claims queued events within the limits and creates or reuses agent sessions through the orchestrator.
- `migrate`: applies database migrations and exits.

The queue is the `events` table in the service's own Postgres: ingest writes `new` rows and sends `NOTIFY webhook_events`, the worker claims them with status-guarded updates. Services share no other state.

## Configuration

Every setting is an environment variable. The first block is read by all subcommands, the rest are shared base settings. `migrate` and `ingest` need no orchestrator URL or token; `api` and `worker` require `WEBHOOK_ORCHESTRATOR_URL` and a token (`WEBHOOK_ORCHESTRATOR_TOKEN` or `WEBHOOK_ORCHESTRATOR_TOKEN_FILE`).

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_PUBLIC_BASE_URL` | `https://lab.szymonrichert.pl` | base of the printed endpoint URLs (trailing slash removed) |
| `WEBHOOK_BOT_LOGINS` | empty | comma-separated logins whose own events are ignored (`self_event`) |
| `WEBHOOK_MAX_IN_PROGRESS` | 3 | events running at once, overall |
| `WEBHOOK_MAX_IN_PROGRESS_PER_ENDPOINT` | 1 | events running at once for one endpoint |
| `WEBHOOK_MAX_QUEUE` | 500 | `new` events kept before further ones are ignored as `queue_full` |
| `WEBHOOK_MAX_BODY_BYTES` | 5242880 (5 MiB) | largest accepted request body |
| `WEBHOOK_RUN_TIMEOUT_SECONDS` | 3600 | an in-progress event older than this fails `timeout` |
| `WEBHOOK_POLL_SECONDS` | 5 | worker tick when no NOTIFY arrives |
| `WEBHOOK_EVENTS_RETENTION_DAYS` | 30 | the worker deletes handled, ignored and failed events idle longer than this, hourly, in batches of 1000 |
| `WEBHOOK_AUTH_FAILURES_PER_MINUTE` | 10 | failed authentications per client per minute before 429 |
| `WEBHOOK_API_ALLOWED_CIDRS` | `127.0.0.1/32` | comma-separated CIDRs the api accepts as direct peer address (403 otherwise, `/healthz` and `/readyz` excepted; `X-Forwarded-For` is never read). The default admits only the oauth2-proxy sidecar |
| `WEBHOOK_TRUSTED_PROXIES` | empty | comma-separated CIDRs whose `X-Forwarded-For` is trusted |
| `WEBHOOK_MIN_PASSWORD_LENGTH` | 16 | shortest accepted caller-chosen password |
| `WEBHOOK_TEMPLATE_TIMEOUT_SECONDS` | 2.0 | per-render time limit for prompt templates |
| `WEBHOOK_TEMPLATE_MEMORY_MB` | 256 | memory a template render child may allocate beyond its baseline (`RLIMIT_AS`, enforced on Linux); over it the event fails `template_error`. Chart value `templateMemoryMb` |
| `WEBHOOK_DATABASE_URL` | required | Postgres URL |
| `WEBHOOK_HTTP_HOST` | `0.0.0.0` | listen address |
| `WEBHOOK_HTTP_PORT` | 8000 | listen port |
| `WEBHOOK_LOG_LEVEL` | `INFO` | log level |
| `WEBHOOK_ORCHESTRATOR_URL` | required for `api`, `worker` | the agents-orchestrator base URL |
| `WEBHOOK_ORCHESTRATOR_TOKEN` | empty | service token for the orchestrator (`api`, `worker`) |
| `WEBHOOK_ORCHESTRATOR_TOKEN_FILE` | empty | file holding the token; wins over the variable |
| `WEBHOOK_SHUTDOWN_GRACE_SECONDS` | 20 | graceful shutdown time |

The limits are set from the Helm chart (`limits.maxInProgress`, `limits.maxInProgressPerEndpoint`, `limits.queueCap`, `limits.maxBodyBytes`).

## Endpoints

An endpoint is one URL, `https://lab.szymonrichert.pl/webhook/<suffix>`, with one type, one agent preset, an optional prompt template and one basic-auth username and password. Create it in the web UI (Webhooks menu) or with `POST /webhooks/api/v1/endpoints` (see API). The response shows the password once; store it then. Rotate (`POST /webhooks/api/v1/endpoints/{id}/rotate`) to replace it; the old password stops working at once.

| Type | Accepts |
|---|---|
| `github-issue` | GitHub `issues` and `issue_comment` events on issues |
| `github-pr` | GitHub `pull_request`, `pull_request_review`, `pull_request_review_comment` and `issue_comment` events on pull requests |
| `gitlab-issue` | GitLab Issue Hook and Note Hook on issues |
| `gitlab-mr` | GitLab Merge Request Hook and Note Hook on merge requests |
| `grafana-alert` | Grafana webhook contact point alerts (`firing` starts work, `resolved` is ignored) |

An event of another provider type on an endpoint is ignored with `type_mismatch`; an event that is no supported event at all with `unsupported_event`.

Configure the sender with the endpoint URL and its credentials:

- GitHub: Settings, Webhooks, Add webhook. Payload URL `https://<username>:<password>@lab.szymonrichert.pl/webhook/<suffix>`, content type `application/json`, choose the events (issues, issue comments, pull requests, reviews). Leave the secret empty. A `ping` is answered `pong`.
- GitLab: Settings, Webhooks. URL `https://<username>:<password>@lab.szymonrichert.pl/webhook/<suffix>`; tick issue, merge request and comment events as the type needs. Leave the secret token empty.
- Grafana: Alerting, Contact points, new contact point of type Webhook. URL `https://lab.szymonrichert.pl/webhook/<suffix>`, set the basic auth Username and Password fields.

## Filters

Each endpoint carries `filters = {"allow": [Rule], "deny": [Rule]}` with `Rule = {"field", "match", "values"}`; `match` is `equals` (the default) or `regex`. Empty lists accept every event.

An author allowlist is an endpoint `allow` rule on `author` (for example `{"field": "author", "values": ["alice", "bob"]}`); there is no global author setting. `WEBHOOK_BOT_LOGINS` stays global: it is the self-loop guard (`self_event`) and runs before any filter.

- Order: the first deny rule (in list order) that matches ignores the event with `filter_denied: <field>`; otherwise the first allow rule that does not match ignores it with `filter_not_allowed: <field>`. Allow rules combine with AND, the values within one rule with OR.
- Fields per type: `github-issue` and `gitlab-issue` offer `author`, `actor`, `repo`, `labels`, `title`, `body`, `comment`, `kind`, `action`; `github-pr` and `gitlab-mr` add `source_branch` and `target_branch`; `grafana-alert` offers `labels`, `title`, `body`. A field the event does not carry (for example `comment` on a non-comment event) never matches: it fails an allow rule and never trips a deny rule. A list field (forge `labels`) matches when any element matches.
- Not applicable, and why: `grafana-alert` has no `author`, `actor`, `repo` or `comment` (alerts carry none), no `kind` (always an alert) and no `action` (a non-firing alert is already `resolved`); issue types have no `source_branch` or `target_branch` (issues have no branch), and a `issue_comment` on a pull request carries none either, so they are absent there. Not offered for any type: `number`, `url`, `draft`, `changed` (the built-in rules already decide drafts and title-only edits), the delivery id, the dedup key, the priority and the raw payload.
- `equals` compares whole values: case-insensitively for `author`, `actor`, `repo` and forge `labels`, exactly for every other field.
- `regex` is an unanchored search; anchor with `^...$`, use `(?i)` for case-insensitivity. Patterns use RE2 syntax (the `google-re2` engine): matching takes time linear in the text, and backreferences and lookaround are refused when the filters are saved.
- Grafana `labels` values are `key=value` (split on the first `=`, non-empty key) and match the alert group's common labels: `equals` compares the label's value exactly, `regex` searches it with the part after `=`. A key the alert group does not carry never matches.
- Window: a regex reads at most 65536 characters of a field (of each list element, of each label value). A longer text fails closed instead of being cut: a regex rule on it never satisfies an allow rule and always trips a deny rule, so padding cannot push denied content past the window and an end anchor cannot match at a cut point. `equals` rules are unaffected.
- Bounds, checked on save: at most 20 rules per list, 1 to 50 values per rule, each value 1 to 256 characters, and at most 200 values in total across all allow and deny rules of an endpoint (`MAX_TOTAL_VALUES`; the error names the path of the rule that crosses it).
- List cap: a list field (forge `labels`) is read for at most 100 elements (`MAX_LIST_ITEMS`). A longer list fails closed like an oversized text: a `regex` or `equals` rule on it never satisfies an allow rule and always trips a deny rule.
- Evaluation (and the regex compile on a cache miss) runs in a worker thread in ingest and in the preview route, so a large rule set cannot stall the event loop.

## Agent reuse

Each agent preset has a boolean `reuse_running_agent`, off by default. When it is on for an endpoint's preset, events about the same item (the same repository and issue or merge request number, or the same alert group, which is the event's dedup key on that endpoint) go to the agent already working on that item:

- A queued event whose item has an earlier run is delivered as an update message to that run's session instead of creating a new one. A session counts as running while it exists and its desired state is `active` (its first turn may still be running or already complete). The event is handled once the message is sent; it bypasses the in-progress limits.
- Ordering: events of one item are processed in order. An update waits while an earlier event of the same item is still without a session or has an update in flight. With reuse off, any in-progress event of the same item blocks the next one.
- Fallback: when the session is gone, refuses the message, or cancels or fails the message before it is sent, the event falls back once to a new session `wh-<id>` (when the limits allow, else it returns to the queue in its place). If a fallback event then fails, its reason is `reuse_fallback` (for example `reuse_fallback: turn_failed`).
- With reuse off every event starts a new session. The service never deletes sessions.

## Statuses and reasons

An event has one of five statuses: `new` (queued), `in-progress` (claimed, running), `handled` (turn completed or update sent), `ignored` (nothing to do), `failed`. A queued `new` event of an item is replaced by a newer delivery for the same item (latest wins) and the older one becomes `duplicate`.

| Reason | Status | Meaning |
|---|---|---|
| `self_event` | `ignored` | the actor is one of `WEBHOOK_BOT_LOGINS` |
| `resolved` | `ignored` | a Grafana alert that is not firing |
| `action_ignored` | `ignored` | an action that starts no work (closed, labeled, draft merge request, title-only edit) |
| `type_mismatch` | `ignored` | a provider event of another endpoint type |
| `unsupported_event` | `ignored` | not a supported provider event |
| `filter_denied` | `ignored` | a deny rule of the endpoint matched; written `filter_denied: <field>` |
| `filter_not_allowed` | `ignored` | an allow rule of the endpoint did not match; written `filter_not_allowed: <field>` |
| `filter_error` | `ignored` | the endpoint's filters could not be evaluated (fails closed; never expected after saving) |
| `duplicate` | `ignored` | the same delivery id again, or a queued event replaced by a newer one |
| `queue_full` | `ignored` | `WEBHOOK_MAX_QUEUE` queued events reached |
| `endpoint_removed` | `ignored` | the endpoint was deleted while the event was queued |
| `preset_not_found` | `failed` | the orchestrator no longer has the preset |
| `session_not_found` | `failed` | the run's session disappeared |
| `template_error` | `failed` | the prompt template failed to render |
| `timeout` | `failed` | older than `WEBHOOK_RUN_TIMEOUT_SECONDS` without finishing |
| `turn_failed` | `failed` | the agent turn failed |
| `message_failed` | `failed` | the orchestrator failed the prompt message |
| `message_cancelled` | `failed` | the prompt message was cancelled |
| `command_<error>` | `failed` | the prompt is a slash command and it failed; `<error>` is the orchestrator's command error (`command_failed` when it gave none) |
| `command_cancelled` | `failed` | the prompt is a slash command and it was cancelled |
| `reuse_fallback` | `failed` | a fallback after a failed agent reuse failed; carried as a prefix |

A 4xx from the orchestrator when creating a session or posting fails the event with the error code it returned.

## Security

- The `ingest` Deployment is public and outside SSO: `/webhook` is its own Ingress. Only the `api` Deployment carries SSO annotations. The ingest pod never receives the orchestrator token.
- Endpoint passwords are stored only as scrypt hashes; the plaintext is shown once at creation or rotation (`Cache-Control: no-store`).
- Credentials are compared in constant time, also for suffixes that do not exist. Unknown suffix and wrong credentials give the same 401 with `WWW-Authenticate: Basic`.
- Failed authentications are limited per client (`WEBHOOK_AUTH_FAILURES_PER_MINUTE`, then 429 with `Retry-After`). The client is the peer address; `X-Forwarded-For` is honoured only from `WEBHOOK_TRUSTED_PROXIES`.
- Bodies over `WEBHOOK_MAX_BODY_BYTES` (5 MiB) get 413, after authentication; stalled body reads are cut off with 408 `{"error": {"code": "request_timeout", ...}}`.
- Prompt framing: the default prompt wraps the event in an escaped `<event>` data block so untrusted text is data, and custom templates run in a sandbox (see Prompt templates).
- `api` and `worker` call the orchestrator with a service token (`WEBHOOK_ORCHESTRATOR_TOKEN` or `_FILE`); it is never logged or returned.

## Commands

`agents-webhook <subcommand>`:

| Subcommand | Does | Needs |
|---|---|---|
| `migrate` | applies migrations, exits | database URL |
| `ingest` | public listener | database URL |
| `api` | UI API | database URL, orchestrator URL and token |
| `worker` | queue dispatcher (singleton by advisory lock) | database URL, orchestrator URL and token |

Invalid settings exit with code 78, naming every bad variable.

## Helm chart

`chart/agents-webhook` deploys the `ingest` (2 replicas), `api` and `worker` Deployments from one image, the settings ConfigMap, a Secret and a Service. Required values: `image.tag`, `orchestrator.url` with `orchestrator.token` or `orchestrator.tokenSecret`, and exactly one of `postgres.url` and `postgres.existingSecret`. `limits.*` render the limit variables above; `publicBaseUrl` and `trustedProxies` render `WEBHOOK_PUBLIC_BASE_URL` and `WEBHOOK_TRUSTED_PROXIES`; `api.allowedCidrs` renders `WEBHOOK_API_ALLOWED_CIDRS` on the api pod only (default `127.0.0.1/32`, so the SSO sidecar is the only caller; the sidecar's `http-insecure` Service port is otherwise open to every in-cluster pod); `env` adds extra `WEBHOOK_*` settings. With `ingress.enabled` there are two Ingresses on one host: `/webhooks/api` (put SSO annotations in `ingress.annotations`) and `/webhook` (public; `ingress.ingestAnnotations`, body limit `ingress.proxyBodySize`, keep it at or above `limits.maxBodyBytes`). Each pod runs `migrate` as an init container.

## Release

Every green push to `main` ends with the `release` job in `.github/workflows/ci.yaml`, which needs every other job of the run, so only a fully green build is released:

- `scripts/next_version.py` computes the version from the conventional commits since the last `vX.Y.Z` git tag (every commit, branch commits behind merge commits included): a `type!:` subject or a `BREAKING CHANGE:` footer bumps major (also on 0.x), `feat` bumps minor, anything else bumps patch. With no `vX.Y.Z` tag yet the first release is `0.1.0`.
- The job copies the run's image `harbor.szymonrichert.pl/containers/agents-webhook:<sha7>` to the tags `X.Y.Z`, `X.Y` and `X` with `regctl image copy` (manifest only, no layer upload), then pushes git tag `vX.Y.Z` with the job token.
- When `HEAD` is already in a `vX.Y.Z` tag (a re-run, or a newer commit already released) the job skips everything after the version step. Release jobs run one at a time (concurrency group `release`); retry a failed one with "Re-run failed jobs".
- agents-helmfile must pin `X.Y.Z`, never `<sha7>`: Harbor retention prunes `<sha7>` tags within hours, semver tags survive it.

The chart is published by the `chart-publish` job on every push to `main`, after `lint`, `test`, `chart`, `e2e` and `images` pass: `scripts/chart-version.sh` gives `<version>-main.<sha7>` from `chart/agents-webhook/Chart.yaml`, with `appVersion` set to `<sha7>`, pushed to `oci://harbor.szymonrichert.pl/charts`. Any other ref, tags included, publishes no chart.

## Development

```
mise run install     # uv sync
mise run test        # unit tests
mise run lint        # ruff check and format check
mise run fmt         # format and autofix
mise run test:chart  # chart tests
mise run lint:chart  # helm lint
mise run test:e2e    # end-to-end tests
```

## Ingest

`POST /webhook/{suffix}` is the only ingest path. It is served by the public `ingest` Deployment (no SSO). After authentication, the body cap, JSON parsing, normalize and the built-in rules (`classify`), the endpoint's filters (see Filters) run; a built-in reason wins over a filter. A delivery that fails a filter is accepted (202) and stored `ignored` with the reason, never queued. Each endpoint has a type (`grafana-alert`, `gitlab-issue`, `gitlab-mr`, `github-issue`, `github-pr`), a preset, an optional prompt template and HTTP basic-auth credentials.

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

- Templates run in a sandboxed Jinja2 environment (`ImmutableSandboxedEnvironment`): no loader (no `include` or `import` of files), autoescape off, `StrictUndefined` (an undefined variable is an error, not an empty string), output capped at 64 KiB, template source at most 16 KiB, `range` limited to 10000 items, and repetition, product and power results bounded. Each render runs in a child process killed after `WEBHOOK_TEMPLATE_TIMEOUT_SECONDS` (default 2) and capped at `WEBHOOK_TEMPLATE_MEMORY_MB` (default 256) of address space, so filters such as `center(10**9)` fail with `template_error` instead of exhausting the pod. The chart sets default `resources` for ingest (limit 256Mi), api and worker (limit 640Mi, above the cap plus the pod's own usage); override under `components.<name>.resources`.
- The `from_json` filter parses a JSON string into a value: `{% set body = payload.message | from_json %}{{ body.field }}`. A value that is not valid JSON is a render error.
- Context variables for every type: `type`, `source`, `kind`, `action`, `repo`, `number`, `title`, `body`, `comment_body` (null unless the event is a comment), `url`, `author`, `actor`, `labels`, `delivery_id`, `dedup_key`, `priority`, `draft`, `changed` (both stored with the event, so a delivered prompt matches the preview), plus `payload` (the raw request JSON, `{}` when it is not an object) and `event` (the default block, fully escaped). The other variables are raw: escape or frame untrusted text yourself.
- `labels` is a list of names for `github-issue`, `github-pr`, `gitlab-issue` and `gitlab-mr`, and a mapping (the alert's `commonLabels`) for `grafana-alert`.
- Creating an endpoint validates the template (syntax, unknown filters and tags): 422 `invalid_request` naming the line.
- A template that fails at delivery time (undefined variable, bad `from_json` input, timeout, size) fails the event with reason `template_error`.
- To check a template against an example JSON payload without storing anything, call `POST /webhooks/api/v1/templates/preview`.

## API

The UI API lives under `/webhooks/api/v1` in the `api` Deployment behind the SSO sidecar. Errors are `{"error": {"code": "...", "message": "..."}}`; codes: `invalid_request` (422), `unknown_preset` (422, also for a preset_id that is not a UUID), `not_found` (404), `suffix_taken` (409), `orchestrator_unavailable` (503), `orchestrator_auth` (502, the orchestrator rejected the service token), `unavailable` (503). The committed schema is `openapi.json` (regenerate with `uv run python -m agents_webhook.api.openapi > openapi.json`; a test fails on drift).

Calling headless: through SSO send a Keycloak bearer token (`Authorization: Bearer $TOKEN`); the injected oauth2-proxy sidecar already accepts it (it sets `OAUTH2_PROXY_SKIP_JWT_BEARER_TOKENS=true` itself), so no per-deployment setting is needed. Without SSO, `kubectl -n agents port-forward deploy/agents-webhook-api 8000:8000` (the api container port) and use `http://localhost:8000`. The examples use:

```
API=https://lab.szymonrichert.pl/webhooks/api/v1
AUTH="Authorization: Bearer $TOKEN"
```

### `GET /webhooks/api/v1/endpoints`

Lists endpoints: 200, an array of `{id, suffix, url, type, preset_id, prompt_template, filters, username, created_at, credentials_rotated_at}`. Never carries a password.

```
curl -H "$AUTH" $API/endpoints
```

### `POST /webhooks/api/v1/endpoints`

Body `{type, preset_id, suffix?, username?, password?, prompt_template?, filters?}` where `filters` is `{allow: [rule], deny: [rule]}` and a rule is `{field, match: "equals" | "regex" (default equals), values: [string]}` (see Filters; the view always carries `filters`, empty lists when unset); a missing suffix, username or password is generated (password at least `WEBHOOK_MIN_PASSWORD_LENGTH` characters when given). Response 201: the endpoint view plus `password`, shown only here (`Cache-Control: no-store`). Errors: 422 `invalid_request` (bad type, suffix, credentials, template or filter rule; the message names the rule path such as `filters.deny[0]`), 422 `unknown_preset` (also a `preset_id` that is not a UUID), 409 `suffix_taken`, 502 `orchestrator_auth`, 503 `orchestrator_unavailable`.

```
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"github-pr","preset_id":"<preset id>","prompt_template":"Review {{ repo }}#{{ number }}: {{ title }}"}' \
  $API/endpoints
```

### `PUT /webhooks/api/v1/endpoints/{id}/filters`

Replaces the endpoint's filters. Body `{allow?: [rule], deny?: [rule]}` (a missing list is empty). Response 200: the endpoint view. Errors: 404 `not_found`, 422 `invalid_request` (the message names the rule path, for example a field that does not apply to the endpoint type).

```
curl -X PUT -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"deny":[{"field":"labels","values":["env=staging"]}]}' \
  $API/endpoints/3/filters
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

An event view carries `id`, `received_at`, `updated_at`, `endpoint_id`, `endpoint_suffix`, `type`, `source`, `kind`, `action`, `repo`, `number`, `title`, `url`, `author`, `status`, `reason`, `dedup_key`, `deliveries`, `preset_id`, `session_id` and `session_slug` (the agent session the run started or was delivered to, null until then), `message_id`, `update_of`, `duplicate_of`, `started_at` and `finished_at`.

```
curl -H "$AUTH" "$API/events?status=failed&limit=20"
```

### `GET /webhooks/api/v1/events/{id}`

One event with its raw `payload` and `labels`. Response 200. Errors: 404 `not_found`.

```
curl -H "$AUTH" $API/events/42
```

### `POST /webhooks/api/v1/templates/preview`

Renders a template and/or evaluates filters against an example JSON payload; nothing is stored. Body `{template?, type, example, filters?}`, at least one of `template` and `filters` required: `type` is an endpoint type and `example` the provider payload of that type (it is normalized exactly as at ingest, so the context variables are the real ones; `payload` is `example`). Response 200 `{prompt, filters}`: `prompt` is the rendered text, or null without a `template`; `filters` is `{passed, reason}` (the same verdict as ingest: a built-in reason such as `self_event` or `action_ignored` wins, then `filter_denied: <field>`, `filter_not_allowed: <field>` or `filter_error`; `reason` is null when it passes), or null without `filters`. Errors: 422 `invalid_request` when neither is given (`template or filters: required`), a filter rule is invalid for `type` (the message names the rule path), `type` is unknown, the example is not a payload of that type, or the template does not compile or render (the message names the cause).

```
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"github-issue","template":"{{ title }} by {{ author }}","example":{"action":"opened","issue":{"number":1,"title":"Hi","body":"x","user":{"login":"bob"},"labels":[]},"repository":{"full_name":"o/r"},"sender":{"login":"bob"}}}' \
  $API/templates/preview
```

### `GET /webhooks/api/v1/meta`

Response 200: `{public_base_url, ingest_path_prefix, types, filter_fields, limits}` where `filter_fields` maps each endpoint type to the fields a filter rule may use and `limits` has `max_in_progress`, `max_in_progress_per_endpoint`, `max_queue`, `max_body_bytes`, `run_timeout_seconds` and `auth_failures_per_minute`.

```
curl -H "$AUTH" $API/meta
```
