# HTTP API

Base: `http://127.0.0.1:<port>` (loopback only; the port is in the state
directory's `gateway.json` discovery file). The machine-readable contract is
`docs/openapi.json`; this page is the human view. Only the endpoints below
exist.

## Authentication

Two disjoint bearer realms:

- **Control token** — random per gateway run, delivered only through the
  0600 discovery file; required by everything under `/control/v1`.
- **Instance token** — random per registered instance, delivered only in
  the registration response; required by `/v1/*`.

Instance tokens are rejected on `/control/v1` with
`403 instance_token_not_admin`; anything else is
`401 invalid_admin_token`. On `/v1/*` a missing/unknown token (including
the control token) is `401 invalid_instance_token`; ended or lease-expired
instances are `403 instance_ended` / `403 instance_expired`.

## Error shape

Every **gateway-generated** error is JSON with a stable `code`:

```json
{"error": {"code": "unknown_model", "message": "...", "request_model": "x"}}
```

| Code | Status | Meaning |
| --- | --- | --- |
| `invalid_json` / `invalid_request` | 400 | malformed body |
| `invalid_config` | 400 | shared config validation failed (`errors_[]` lists field paths) |
| `invalid_selection` / `no_default_model` | 400 | instance creation input invalid |
| `invalid_pagination` | 400 | bad `offset`/`limit` |
| `unknown_model` | 400 | request model not routed for the instance |
| `invalid_admin_token` | 401 | missing/wrong control token |
| `invalid_instance_token` | 401 | missing/wrong instance token (also after gateway restart) |
| `instance_token_not_admin` | 403 | instance token used on `/control/v1` |
| `instance_ended` / `instance_expired` | 403 | lifecycle rejection |
| `instance_not_found` | 404 | unknown instance ID |
| `revision_conflict` | 409 | CAS mismatch (`current_revision` included) |
| `credential_missing` | 500 | provider env var absent at request time (no upstream call) |
| `config_persist_failed` | 500 | configuration validated but could not be persisted (disk state and running config unchanged) |
| `upstream_error` | 502 | upstream connection failure |
| `gateway_locked` | CLI exit 2 | another gateway owns the state directory |

**Relayed upstream responses are not gateway errors.** The proxy endpoints
forward upstream status codes, bodies and content types verbatim — any media
type (JSON, `text/plain`, `text/html`, `application/problem+json`, ...). A
relayed upstream 400/401/403/5xx uses the same status numbers as several
gateway errors above but carries whatever the upstream sent — an arbitrary
body and content type with no stable `code`, and an upstream body may even
coincidentally match the Error shape. Gateway-generated errors always use
the Error shape (JSON with `error.code`), but shape alone cannot prove a
response's origin; the OpenAPI contract models each proxy status as either
the `Error` schema or a verbatim `UpstreamRelay` body under any media type.

## Control endpoints

### `GET /control/v1/config`
→ `200 {"revision": 3, "config": {...}}`

### `PUT /control/v1/config`
Body: the complete configuration object. Validates everything, persists
atomically, then swaps catalog and defaults.
→ `200 {"applied": true, "revision": 4, "config": {...}}` or `400 invalid_config`.

### `POST /control/v1/instances`
Body (all optional): `{"label"?, "model"?, "aux_model"?, "routes"?}` where
`routes` maps exact request models to catalog model IDs and overrides
defaults. Validated wholly before registration.
→ `201 {"instance": {...status...}, "token": "<bearer, shown once>"}`.

Instance status shape:

```json
{
  "id": "i-hfbfunte85wg", "label": "a", "state": "active",
  "model": "req-main", "aux_model": "req-aux", "revision": 2,
  "created_at": 1788835000.0, "lease_expires_in": 27.4,
  "routes": {"req-main": {"catalog_model": "model-p", "catalog_present": true,
                          "provider": "prov-p", "base_url": "...",
                          "upstream_model": "vendor/p", "auth": "bearer",
                          "credential_env": "QING_P", "route_revision": 2}}
}
```

`state` is `active`, `expired` (lease lapsed) or `ended`.

### `GET /control/v1/instances?offset=0&limit=50`
Bounded pagination: `limit` defaults to 50 and is clamped to 100;
`next_offset` is `null` on the last page.
→ `200 {"instances": [...], "offset", "limit", "total", "next_offset"}`.

### `GET /control/v1/instances/{id}` → `200 {"instance": {...}}` or 404

### `POST /control/v1/instances/{id}/renew`
Extends the lease by 30 s. → `200 {"instance": {...}}`; rejected for
ended/expired instances.

### `DELETE /control/v1/instances/{id}`
Ends the instance (idempotent). New requests fail; in-flight requests keep
their snapshot. → `200 {"ended": true, "instance": {...}}`.

### `PUT /control/v1/instances/{id}/routes`
Body: `{"request_model": "req-main", "model": "model-q",
"expected_revision": 3}`. Resolves the destination from the current
catalog, atomically replaces that one route snapshot and bumps the
revision. The success response is the applied acknowledgement.
→ `200 {"applied": true, "revision": 4, "route": {...}, "instance": {...}}`;
`409 revision_conflict` includes `current_revision`.

## Proxy endpoints

### `POST /v1/messages` (query string preserved, e.g. `?beta=true`)
Body: Anthropic messages request JSON. The gateway performs no Anthropic
payload validation beyond requiring a non-empty `model` string; the `model`
string must match the instance's route table exactly. The gateway replaces
only the `model` field with the snapshot's upstream model, strips the
client's auth headers, injects the provider credential
(`Authorization: Bearer ...` or `x-api-key`), forwards
`anthropic-version` / `anthropic-beta` / `accept`, and relays the upstream
status and body unchanged — including arbitrary error statuses, bodies and
content types (see "Relayed upstream responses are not gateway errors"
above). No retries, no redirects, no model fallback.

With `stream: true`, SSE responses are relayed chunk by chunk. Content
encodings (e.g. gzip) are decoded by the gateway and never forwarded, so
downstream always receives identity-encoded SSE with intact event
semantics. Downstream disconnects (also while awaiting upstream response
headers or buffered bodies) always release the upstream connection; a
stream that ends before `message_stop` is recorded as incomplete, an SSE
`error` event as failed — never as success.

### Request metadata

The gateway keeps at most 1000 sanitized request records (no bodies, tool
arguments, credentials, raw exceptions or upstream error payloads). Usage
follows the Anthropic cumulative contract: `message_start`/`message_delta`
values replace each other and are never summed; unknown values are `null`
and real zeros are preserved. `GET /control/v1/requests` (control token;
`offset` defaults to 0, `limit` defaults to 50 and is clamped to 100,
`next_offset`; optional exact `instance_id` filter) lists them with outcomes
`in_progress`, `success`, `failed`, `cancelled` or `incomplete`. The
`qing requests --json` command provides machine output; terminal layout
acceptance is deferred.

### `POST /v1/messages/count_tokens`
Same authentication and routing; forwarded to the upstream's
`/v1/messages/count_tokens`.

## `qing run` output contract

`qing run` passes Claude's stdout through untouched (native
`--output-format` semantics are never modified, and no wrapper text is
inserted). Launcher diagnostics go to stderr: human-readable lines by
default, or — with `qing run --json` — one NDJSON event per line with a
stable `event` name (`registered`, `child_started`, `note`,
`first_request_observed`, `warning`, `instance_lost`, `error`), a
machine-usable `code` on errors (`gateway_unreachable`,
`registration_failed`, `conflicting_native_arg`, `invalid_selection`,
`client_start_failed`, `launcher_setup_failed`, `cli_error`), and
`instance_id` whenever known. Claude's own stderr may interleave, so the
combined stderr is not a pure JSON document; wrapper events are
individually parseable. Exit codes: the child's code on normal/signal
exit, 2 for usage/registration failures, 3 when the instance was lost.

## CLI equivalents

`qing serve`, `qing run`, `qing config show|apply`, `qing defaults set`,
`qing instances`, `qing instance end <id>`, `qing requests`, `qing route
set <request-model> <catalog-model> --instance <id|label>` (labels must resolve uniquely;
ambiguous labels error with the candidate IDs). Every command accepts
`--json` and `--state-dir`.

Route changes are only reported as applied on a well-formed acknowledgement
matching the intended instance, request model, destination and expected
next revision; timeouts report the effect as unconfirmed and point at
reading the instance status back.
