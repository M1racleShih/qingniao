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
| `operation_not_found` | 404 | unknown import operation ID |
| `revision_conflict` | 409 | CAS mismatch (`current_revision` included) |
| `generation_conflict` | 409 | conditional write with a stale generation (`current_generation` included) |
| `import_plan_mismatch` | 409 | operation id reused with a different plan |
| `request_too_large` | 413 | import plan body above the size limit |
| `entry_exists` | CLI 1 | an add targets a catalog id that already exists (no write) |
| `entry_not_found` | CLI 1 | a set/show/rm targets a catalog id that does not exist (no write) |
| `entry_in_use` | CLI 1 | a delete would break a reference; `references` lists every reference (no write) |
| `invalid_argument` | CLI 2 | an entry-level command received invalid arguments (bad auth mode, malformed URL, invalid env name, conflicting or missing credential sources, nothing to change); `errors[]` lists field problems |
| `cli_error` | CLI 1/2 | generic CLI or usage failure |
| `credential_missing` | 500 | credential source absent at request time — env var unset, private version not present (no upstream call) |
| `credential_invalid` / `credential_unreadable` | 500 | private credential store refused the access (malformed id, unsafe permissions, symlink, foreign owner, non-regular file); sanitized category only |
| `config_persist_failed` | 500 | configuration validated but could not be persisted (disk state and running config unchanged) |
| `transaction_damaged` | 500 | an open import record or its committed credential is unreadable; writes and startup refuse until it is resolved manually |
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

### Configuration format

Requests may carry the legacy format (no `schema_version`, environment
variable credentials only); responses and every persisted write use
format version 1:

```json
{
  "schema_version": 1,
  "generation": 3,
  "last_operation": "op_<hex> or null",
  "providers": {"p": {"base_url": "https://…", "credential_env": "NAME",
                      "auth": "bearer"}},
  "models": {"m": {"provider": "p", "upstream_model": "exact-upstream"}},
  "credentials": {"shared": {"source": "env", "env": "NAME"}},
  "defaults": {"model": "r", "aux_model": "r2", "routes": {"r": "m"}}
}
```

A provider carries **exactly one** credential source: `credential_env`
(read from the gateway process) or `credential_id` (a credential catalog
id or an immutable version in the private credential store under
`<state-dir>/credentials`, created by `qing config import-claude` or
`qing credential add --from-stdin`; the secret itself never appears in the
configuration, responses or logs). The optional `credentials` section
names catalog credentials: `{"source": "env", "env": NAME}` reads the
credential from the gateway process environment variable `NAME`, and
`{"source": "private"}` refers to an immutable private store version.
A provider may reference either an env-type catalog id (the gateway
resolves the environment variable at request time) or a direct
`cred_<hex>` id; `credential_id` values that are neither in the catalog
nor valid private ids are rejected. An absent `credentials` section is
valid and behaves as empty, and the section is written to disk **only
when it is non-empty** — a configuration without catalog credentials
never carries the key, so it stays readable by builds that predate this
feature. Configuration files that do carry the `credentials` section
require this build: older builds reject them as an unknown top-level
field (`credentials` in the `unknown top-level fields` error), so before
rolling back to an older program either remove the section from the
configuration or restore the pre-upgrade backup. Unknown future
`schema_version` values are rejected — never migrated or overwritten.
The first write that upgrades a legacy file snapshots the old bytes as
`config.backup.pre-v1.json`; to roll back, stop the gateway, restore
that backup and use the older program (imported private connections do
not carry back and their files are not auto-deleted).

`generation` increases with every persisted write and enables
conditional updates; `last_operation` identifies the import transaction
that last committed the file.

### `GET /control/v1/config`
→ `200 {"revision": 3, "generation": 3, "config": {...}}`

### `PUT /control/v1/config[?expected_generation=N]`
Body: the complete configuration object. Validates everything, persists
atomically, then swaps catalog and defaults. With `expected_generation`
the write is conditional: a mismatch answers
`409 generation_conflict` with `current_generation` and changes nothing;
without it the request behaves as unconditional last-write-wins
(pre-generation clients keep working).
→ `200 {"applied": true, "revision": 4, "generation": 4, "config": {...}}`,
`400 invalid_config` or `409 generation_conflict`.

### `POST /control/v1/imports`
Body: `{"operation_id": "op_<hex>", "plan": {…}}` — the complete
wire-encoded plan that `qing config import-claude --apply` builds (max
256 KiB). The plan may carry the secret for the private store; the body
is never logged and errors never echo it. Commits one transaction
through the same serialized path as every configuration write.
Repeating an operation id with the same plan returns the original
result (`"duplicate": true`); the same id with a different plan answers
`409 import_plan_mismatch`.
→ `200 {"status": "committed|unchanged|skipped", "applied": true,
"generation": 4, "provider_id": "…", "credential_id": "cred_<hex>", …}`.
`applied` is true only when the running gateway swapped the new
configuration in; a persist failure at the commit point answers with
`"status": "unconfirmed"` and recovery resolves it — never assume it
rolled back.

### `GET /control/v1/operations/{operation_id}`
→ `200 {"operation_id": "op_…", "status":
"in_progress|committed|aborted|unconfirmed", "generation": 4}` or `404
operation_not_found`. Id, commit status and generation only — no
credentials, no plan content.

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

`state` is `active`, `expired` (lease lapsed) or `ended`. Route
snapshots created after an import may carry `"credential_id":
"cred_<hex>"` instead of `credential_env`; the id is a reference, never
the secret. Snapshots are immutable: rotating a provider's credential
does not change existing instances or in-flight requests — only new
instances or an explicit route switch pick up the new version, and a
revoked old credential fails honestly without automatic fallback.

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

`qing serve`, `qing run [--preview]`, `qing config
show|apply|import-claude|operation`, `qing defaults set`, `qing
instances`, `qing instance end <id>`, `qing requests`, `qing route
set <request-model> <catalog-model> --instance <id|label>` (labels must
resolve uniquely; ambiguous labels error with the candidate IDs). Every
command accepts `--json` and `--state-dir`.

`qing config import-claude [--source PATH]` previews a desensitized
import plan by default — nothing is written and no provider is
contacted. `--apply` commits it: offline (holding the state directory
lock) when the gateway is stopped — the result is reported as *saved* —
or through `POST /control/v1/imports` when it is running, reported as
*applied*. Both wordings state that the connection is **not verified**:
importing never contacts the provider. Decision flags make the flow
non-interactive: `--auth bearer|x-api-key` (both tokens present),
`--primary-model settings|env` (conflicting main model sources),
`--aux-same-as-primary` (explicitly reuse the main model), and
`--conflict skip|update|new [--new-provider-id ID]` for same-name
collisions with different content; missing decisions fail with zero
writes. The source file is never modified, and a preview is invalidated
when the file changes before the apply. `qing config operation <id>`
queries a submitted operation.

## Entry-level catalog commands

`qing provider` , `qing credential` and `qing model` manage catalog
entries without rewriting the whole configuration. The three groups are
designed for humans and agents alike:

- **Non-interactive.** No command prompts or asks for confirmation; a
  missing required option is a usage error (exit code 2) that lists what
  is missing. Destructive operations rely on `--dry-run` and reference
  protection instead of a y/n prompt.
- **Machine-readable `--json`.** Every command accepts `--json`. Success
  is `{"ok": true, ...}` with entries in the same shape as the control
  API configuration (a provider is `{"id", "base_url", "auth",
  "credential_env" | "credential_id"}`, a model is
  `{"id", "provider", "upstream_model"}`); lists are
  `{"ok": true, "providers": [...]}` / `{"ok": true, "models":
  [...]}` / `{"ok": true, "credentials": [...]}`. Failure (JSON mode)
  is `{"ok": false, "error": {"code", "message", ...details}}` on
  stdout; human mode prints one `error: ...` line on stderr. Output is
  free of ANSI escape sequences when `NO_COLOR` is set or stdout is
  redirected, and values are soft-wrapped but never truncated at widths
  40/80/120.
- **Exit codes.** 0 on success, 1 for expected failures (stable machine
  code in the error payload), 2 for usage errors. Usage errors in the
  entry-level commands (invalid values, mutually exclusive or missing
  credential sources, nothing-to-change sets) exit 2 in **both** human
  and `--json` mode, and in `--json` mode carry the stable
  `invalid_argument` code with an `errors[]` detail (field paths and
  messages); parser-level missing required options also exit 2 with the
  generic `cli_error` code. Expected failures exit 1 with their own
  code: `entry_exists`, `entry_not_found`, `entry_in_use`,
  `generation_conflict`, `gateway_unreachable`, ... (see the error
  table above).
- **Read-modify-write with generation checks.** Every mutating command
  reads the current configuration, changes exactly one entry and
  commits a conditional `PUT /config?expected_generation=N`. A
  concurrently-changed generation fails with `generation_conflict` and
  changes nothing; a stopped gateway fails with `gateway_unreachable`
  and reports the change as unconfirmed. Saving or applying is never
  described as a connection verification.
- **`--dry-run`.** Every mutating command supports `--dry-run`: it
  describes the minimal change (JSON `{"ok": true, "dry_run": true,
  "changes": {...}}`) without writing files or calling a change
  endpoint. A dry run of `credential add` reads no secret.
- **Referential integrity.** `provider rm`, `model rm` and
  `credential rm` fail with `entry_in_use` (listing every reference —
  model ids, `defaults.model`/`defaults.aux_model`/`defaults.routes.<k>`
  locations, or provider ids) when a delete would break a reference.
  There is no cascade delete and no silent rewrite; running instances
  keep their immutable snapshots per the instance-routing rules.

```
qing provider add  <id> --base-url URL --auth bearer|x-api-key (--credential-env NAME | --credential-id ID)
qing provider list
qing provider show <id>
qing provider set  <id> [--base-url URL] [--auth ...] [--credential-env NAME | --credential-id ID]
qing provider rm   <id>

qing credential list
qing credential show <id>
qing credential add <id> --env NAME              # env-variable source
qing credential add --from-stdin | --from-file PATH   # private store
qing credential rm  <id>

qing model add  <id> --provider PID --upstream-model NAME
qing model list
qing model show <id>
qing model set  <id> [--provider PID] [--upstream-model NAME]
qing model rm   <id>
```

**Secrets.** Credential values enter only through `--from-stdin` or
`--from-file` (or `--env`, which names an environment variable the
gateway process already holds); a value is never accepted as a
command-line argument, and `credential show`/`credential list` return
metadata (id, source type, env name or `private`, referencing providers)
only — never the value or a reversible mask. The value, argv, stdout,
stderr, JSON output, errors and logs all stay free of secret material;
the private store commits the value only into `<state-dir>/credentials`
as an immutable 0600 version, and its catalog entry is registered in
the same single transaction.

Route changes are only reported as applied on a well-formed acknowledgement
matching the intended instance, request model, destination and expected
next revision; timeouts report the effect as unconfirmed and point at
reading the instance status back. Import submits follow the same rule:
a timeout queries the operation once and otherwise stays unconfirmed —
never falling back to an offline write under a running gateway.
