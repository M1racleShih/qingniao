# Architecture

Status: foundation stage of `import-claude-config`. This document
describes what is implemented in this repository today.

## Overview

Qingniao is a loopback gateway that lets several Claude Code processes share
one connection catalog while each keeps an independent, hot-switchable
routing selection. One foreground gateway process owns a state directory and
serves two HTTP planes on a single 127.0.0.1 listener:

- `/control/v1` — administration (configuration, instances, routes),
  authenticated with a per-run control token
- `/v1/messages`, `/v1/messages/count_tokens` — Anthropic-compatible
  forwarding, authenticated with per-instance bearer tokens

```
Claude/HTTP client --bearer(instance)--> gateway /v1/* --credential(env | private file)--> upstream provider
CLI (qing ...) ------bearer(control)---> gateway /control/v1/*
```

## State

Persisted inside the state directory:

- `config.json` — the shared configuration (0600, written atomically via
  tmp-file + rename + fsync) in explicit format version 1 with a
  monotonic `generation`. Files without `schema_version` are the legacy
  format and are read as-is — reading never rewrites them, and the first
  versioned write snapshots the old bytes as `config.backup.pre-v1.json`.
- `credentials/` — the private credential store (0700 directory, one
  0600 regular file per immutable `cred_<hex>` version created by an
  import). This is a plain local file store with restrictive
  permissions, **not** an encrypted vault: anyone who can read the
  user's files or backups can read the secrets. Rotation creates a new
  version; old versions are never implicitly deleted.
- `transactions/` — sanitized import records (0700): operation id,
  expected generation, created credential id, plan digest and the
  conclusion receipt. No secret material, ever.

A runtime discovery file (`gateway.json`, 0600) exists only while a
gateway runs and carries the port, PID and control token; it is removed
on shutdown and only by the process that owns it (PID-checked). An
advisory `flock` on `gateway.lock` guarantees a single gateway per state
directory; startup failures clean up everything they created.

Everything else is in memory and intentionally dies with the process:

- instances (IDs, token hashes, route snapshots, revisions, leases)
- the control token
- bounded request metadata (max 1000 distinct records, sanitized facts
  only), listed via `GET /control/v1/requests` and `qing requests`

Consequence: a gateway restart invalidates every instance token. Old
clients fail closed with `invalid_instance_token` and must register again;
the shared catalog and defaults survive in `config.json`.

## Shared configuration

Top-level `schema_version: 1`, a monotonic `generation`, an optional
`last_operation` (the import transaction that last committed the file) and
three sections (see `examples/qingniao-config.example.json`):

- `providers`: id -> `{base_url, credential_env | credential_id, auth}` —
  an API root (http/https, no userinfo/query/fragment; `/v1/...` paths are
  appended at forwarding time), **exactly one** credential source (an
  environment variable name read from the gateway process, or a private
  credential version id), and the injection style (`bearer` or
  `x-api-key`). No credential values are ever stored here.
- `models`: id -> `{provider, upstream_model}` — catalog destinations.
- `defaults`: `{model, aux_model, routes}` — the default main/aux request
  model strings and the default exact-match route table
  (request string -> catalog model ID). Both defaults must be keys in
  `routes` when set.

Unknown future versions are rejected without migration or overwrite.
`PUT /control/v1/config` (and `qing config apply`) validates the complete
object, persists it atomically — conditionally on `expected_generation`
when supplied — and only then swaps the in-memory catalog. Invalid input
changes nothing — not the file, not the running catalog, not any
instance.

## Instances and snapshots

An instance is one attached client identity, created via
`POST /control/v1/instances` with optional `label`, `model`, `aux_model`
and `routes` overrides (all validated wholly before registration; any
failure registers nothing).

- The display `id` (`i-...`) and the ≥256-bit bearer token are separate;
  only the SHA-256 of the token is kept. The token appears exactly once, in
  the registration response.
- At creation the instance copies the current defaults (plus overrides)
  into immutable `RouteSnapshot` values: provider ID, base URL, a typed
  credential reference (environment variable or private credential id),
  auth style, upstream model, and the revision it was set
  at. Later catalog edits or credential rotations never touch existing
  snapshots (status shows `catalog_present: false` once a snapshot
  diverges); only a new instance or an explicit route switch adopts a
  rotated credential version, and a revoked old credential fails without
  automatic fallback.
- Each instance has a monotonically increasing `revision`. `PUT .../routes`
  replaces exactly one request model's snapshot with a fresh resolution
  from the current catalog, guarded by compare-and-swap on
  `expected_revision`; the response is an honest `applied` acknowledgement.
- Leases: 30 s default, renewed via `POST .../renew` (the future launcher
  will renew every 10 s). Expiry and ending are evaluated against an
  injected clock; ended/expired instances reject new requests, renewals and
  route updates with explicit errors and no side effects.

## Request forwarding

For each `/v1/messages` (and `count_tokens`) request:

1. Authenticate the bearer against live instance tokens; ended/expired or
   unknown tokens fail closed before anything else.
2. Read the body and take its `model` string as-is; look it up in that
   instance's route table (exact match; unknown models fail with
   `unknown_model`, never falling back).
3. Capture the immutable route snapshot — from here on the request never
   re-reads defaults, the catalog or the instance's routes.
4. Resolve the credential just before sending: from the gateway process
   environment for `credential_env` references, or from the checked
   private store for `credential_id` references (symlinks, unsafe
   permissions, foreign owners and non-regular files are refused;
   `credential_missing`/`credential_unreadable` fail before any upstream
   I/O, never falling back to another source).
5. Forward with HTTPX (async, `follow_redirects=False`, no retries):
   the upstream URL is `snapshot.base_url` + the original path and query
   (e.g. `/v1/messages?beta=true`), the body is the client's JSON with only
   the `model` field replaced by `snapshot.upstream_model`, and the headers
   carry the client's `anthropic-version`/`anthropic-beta`/`accept` plus the
   injected provider credential. The client's own
   `Authorization`/`x-api-key` are never forwarded.
6. Relay the upstream status and body verbatim; record sanitized metadata
   (instance, request model, actual provider/upstream model, revision,
   status, usage when present). Streaming responses are relayed chunk by
   chunk through a bounded incremental SSE parser: the wire bytes are
   forwarded unchanged (content encodings such as gzip are decoded by
   HTTPX and not re-encoded downstream), while usage is observed in
   passing — `message_start`/`message_delta` usage values are cumulative
   and replace each other, never summed. A stream that ends without
   `message_stop` is marked incomplete (retaining known usage); an SSE
   `error` event is marked failed; HTTP error statuses are marked failed;
   neither is ever recorded as success.
7. Release the upstream connection in every outcome: downstream
   cancellation (observed explicitly while awaiting upstream response
   headers and buffered bodies, and via the streaming response's
   disconnect listener mid-stream), upstream errors, truncation and
   normal completion. Cancellation before admission never touches the
   upstream; after admission the request keeps its full snapshot until it
   completes or is cancelled.

Request metadata (bounded to 1000 records, sanitized facts only) is exposed
via `GET /control/v1/requests` and `qing requests`; in-progress records
show the usage observed so far.

## Routing semantics

Route updates are per-request-boundary: once the gateway acknowledges a
route change (revision bump), new requests resolve against the new
snapshot, while requests already accepted keep the old snapshot until they
complete. Other instances are unaffected — demonstrated by the ASGI-level
dual-instance test with two upstreams.

## Imports (`qing config import-claude`)

Importing turns one Claude settings file into gateway configuration
without touching the source file:

1. **Parse** (bytes already read): only the allowed fields — the base URL
   (rejecting userinfo/query/fragment), Bearer/x-api-key candidates, the
   primary model sources, the three tier defaults and the subagent model.
   `apiKeyHelper`, OAuth and cloud switches are reported as unsupported
   and never executed; everything else is listed by name as not migrated.
2. **Plan** against the current directory: exact model mapping (upstream
   strings kept verbatim; no capability guessing), explicit primary/aux
   decisions (aux equal to primary requires confirmation), content-based
   dedup (normalized endpoint + auth + in-memory secret equality; the two
   credential source types never compare equal), and skip/update/new for
   same-name collisions with different content. Self-reference is
   refused: gateway-owned `qn_` tokens are never stored as upstream
   credentials, and the source address is compared with the gateway's own
   loopback-equivalent endpoint when discovery is available (reverse
   proxy aliases cannot be proven absent offline — a documented
   limitation). Previews and error messages never contain key material.
3. **Commit** as one transaction (below).

Offline applies hold the state directory's exclusive lock; if a gateway
owns it, the plan is submitted to the running gateway over the
authenticated control channel instead. Saved, applied and duplicate are
distinct states and none of them means *verified*: importing never
contacts the provider.

### One logical commit

The import writes credential, configuration and receipt as one
transaction judged by the active configuration's `last_operation`:

1. lock (or the gateway's serialized write path) and recovery of any open
   transaction
2. re-verify the source snapshot and the expected generation
3. stage the private credential and — on the first versioned write — the
   legacy backup, fsynced, tracked by a sanitized transaction record
4. atomically replace `config.json` with the new generation and operation
   id — the commit point
5. persist the receipt; a failure at step 4 is reported *unconfirmed*,
   never as rolled back or succeeded

Recovery runs at gateway startup (before accepting anything), at every
offline write after the lock, and at every running-gateway write entry:
committed transactions keep all products (a missing committed credential
refuses startup rather than guessing), uncommitted ones have their staged
credential and backup removed, and credential files no committed record
or configuration ever owned are cleaned as staging orphans. Rotated
versions referenced by committed records are kept.

## The launcher (`qing run`)

`qing run` registers a fresh instance from the current cwd (explicit
`--model`/`--aux-model`/`--route`/`--label` selections are validated wholly
before registration; `--resume` and other non-conflicting native arguments
pass through), then writes a per-run temporary 0600 JSON `--settings` file
carrying the gateway transport, the instance bearer and the selected
request models. The file outranks project settings env, and it additionally
neutralizes conflicting credentials and provider switches (API keys, OAuth
tokens, Bedrock/Vertex/Foundry/Mantle/AWS/GCP/gateway flags), so neither an
inherited environment nor a project's `settings.json` can bypass the
gateway. The bearer never appears in argv text, logs, status output,
records or the shared configuration; the file is removed on every exit
path, and a registration or spawn failure ends the instance and never
starts the client.

The child keeps the user's Claude config dir, setting sources, session
history, tools, hooks and unrelated environment. A lease keeper renews the
30 s lease every 10 s; one failed renewal is a warning, while explicit
loss (not-found, ended, control-identity change, expiry) or failures
outlasting the lease print reconnect guidance, end the child with bounded
SIGTERM/SIGKILL escalation and exit with a distinct code — the keeper never
re-registers after a gateway restart. SIGINT/SIGTERM are forwarded and
owned from before the child exists, so no setup window can orphan it.
Diagnostics distinguish *registered*, *child started* and *first
gateway-observed request*; stdout belongs to Claude (native `--output-format`
untouched), launcher messages go to stderr, and `--json` switches them to
one NDJSON event per line.

## Not implemented yet

- real-provider verification of the import path (the local end-to-end
  matrix uses synthetic fixtures only); clean distribution installs
